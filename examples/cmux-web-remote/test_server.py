#!/usr/bin/env python3
"""End-to-end tests for server.py against a fake cmux socket.

Runs anywhere (no cmux app needed):

    python3 examples/cmux-web-remote/test_server.py
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

WS_ID = "11111111-1111-1111-1111-111111111111"
SURF_ID = "22222222-2222-2222-2222-222222222222"
WIN_ID = "33333333-3333-3333-3333-333333333333"
NOTIF_ID = "44444444-4444-4444-4444-444444444444"


class FakeCmux:
    """Speaks the subset of the cmux v2 socket protocol the server uses."""

    def __init__(self, path: str, password: str = ""):
        self.path = path
        self.password = password
        self.calls: List[Dict[str, Any]] = []
        self.auth_lines: List[str] = []
        self.stream_conns: List[socket.socket] = []
        self.text = "$ echo hi\nhi\n"
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(16)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        f = conn.makefile("rwb")
        try:
            for raw in f:
                line = raw.decode().strip()
                if line.startswith("auth "):
                    self.auth_lines.append(line)
                    ok = line == "auth " + self.password
                    f.write(b"OK\n" if ok else b"ERROR: bad password\n")
                    f.flush()
                    continue
                req = json.loads(line)
                self.calls.append(req)
                if req["method"] == "events.stream":
                    f.write(json.dumps({"type": "ack", "boot_id": "boot", "resume": {"gap": False}}).encode() + b"\n")
                    f.flush()
                    self.stream_conns.append(conn)
                    threading.Event().wait()  # keep the stream open
                result = self._result(req["method"], req.get("params") or {})
                f.write(json.dumps({"id": req["id"], "ok": True, "result": result}).encode() + b"\n")
                f.flush()
        except (OSError, ValueError):
            pass
        finally:
            f.close()
            conn.close()

    def _result(self, method: str, params: Dict[str, Any]) -> Any:
        if method == "system.ping":
            return {"pong": True}
        if method == "window.list":
            return {"windows": [{"id": WIN_ID, "ref": "window:1"}]}
        if method == "workspace.list":
            return {"workspaces": [{"id": WS_ID, "ref": "workspace:1", "title": "api", "selected": True}]}
        if method == "surface.list":
            return {"surfaces": [{"id": SURF_ID, "ref": "surface:1", "type": "terminal", "title": "zsh", "focused": True}]}
        if method == "surface.wait_text_change":
            changed = params.get("after_revision") != "rev-1"
            return {"text": self.text, "revision": "rev-1", "changed": changed}
        if method == "notification.list":
            return {"notifications": [{"id": NOTIF_ID, "workspace_id": WS_ID, "surface_id": SURF_ID,
                                       "title": "Claude is waiting", "body": "Approve Bash?", "is_read": False}]}
        return {}

    def push_event(self, frame: Dict[str, Any]) -> None:
        for conn in self.stream_conns:
            conn.sendall(json.dumps(frame).encode() + b"\n")

    def methods(self) -> List[str]:
        return [c["method"] for c in self.calls]

    def close(self) -> None:
        self.sock.close()


class ServerTest(unittest.TestCase):
    PASSWORD = "sock-pass"

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.fake = FakeCmux(os.path.join(self.tmp, "cmux.sock"), self.PASSWORD)
        client = server.CmuxClient(self.fake.path, self.PASSWORD)
        self.hub = server.EventHub(client)
        self.hub.start()
        self.token = "test-token"
        self.httpd = server.make_server("127.0.0.1", 0, client, self.hub, self.token)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self._wait(lambda: self.hub.connected)

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.fake.close()

    def _wait(self, cond, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return
            time.sleep(0.02)
        self.fail("condition not met in time")

    def request(self, method: str, path: str, body: Any = None, headers: Dict[str, str] = None,
                auth: bool = True) -> http.client.HTTPResponse:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        if auth:
            h["Cookie"] = f"{server.COOKIE_NAME}={self.token}"
        data = None
        if body is not None:
            data = json.dumps(body)
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=h)
        return conn.getresponse()

    def test_api_requires_token(self) -> None:
        self.assertEqual(self.request("GET", "/api/tree", auth=False).status, 401)
        self.assertEqual(self.request("POST", "/api/send", {"surface_id": SURF_ID, "text": "x"}, auth=False).status, 401)
        bad = self.request("GET", "/api/tree", auth=False, headers={"Cookie": f"{server.COOKIE_NAME}=nope"})
        self.assertEqual(bad.status, 401)
        self.assertNotIn("surface.send_text", self.fake.methods())

    def test_login_sets_strict_http_only_cookie(self) -> None:
        resp = self.request("GET", f"/login?token={self.token}", auth=False)
        self.assertEqual(resp.status, 303)
        cookie = resp.getheader("Set-Cookie")
        self.assertIn(f"{server.COOKIE_NAME}={self.token}", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(self.request("GET", "/login?token=wrong", auth=False).status, 401)

    def test_bearer_token_works_for_scripts(self) -> None:
        resp = self.request("GET", "/api/tree", auth=False, headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(resp.status, 200)

    def test_socket_password_is_sent(self) -> None:
        self.request("GET", "/api/tree").read()
        self.assertIn("auth " + self.PASSWORD, self.fake.auth_lines)

    def test_tree_lists_windows_workspaces_and_surfaces(self) -> None:
        tree = json.loads(self.request("GET", "/api/tree").read())
        ws = tree["windows"][0]["workspaces"][0]
        self.assertEqual(ws["id"], WS_ID)
        self.assertEqual(ws["surfaces"][0]["id"], SURF_ID)

    def test_send_text_then_enter(self) -> None:
        resp = self.request("POST", "/api/send", {"surface_id": SURF_ID, "text": "ls", "enter": True})
        self.assertEqual(resp.status, 200)
        sends = [c for c in self.fake.calls if c["method"].startswith("surface.send")]
        self.assertEqual(sends[0], {**sends[0], "method": "surface.send_text",
                                    "params": {"surface_id": SURF_ID, "text": "ls"}})
        self.assertEqual(sends[1]["params"], {"surface_id": SURF_ID, "key": "enter"})

    def test_cross_origin_post_is_rejected(self) -> None:
        resp = self.request("POST", "/api/key", {"surface_id": SURF_ID, "key": "enter"},
                            headers={"Origin": "https://evil.example"})
        self.assertEqual(resp.status, 403)
        self.assertNotIn("surface.send_key", self.fake.methods())

    def test_post_requires_json_content_type(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/key", body="surface_id=x&key=enter",
                     headers={"Cookie": f"{server.COOKIE_NAME}={self.token}",
                              "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(conn.getresponse().status, 400)

    def test_screen_long_polls_with_revision(self) -> None:
        first = json.loads(self.request("GET", f"/api/screen?surface_id={SURF_ID}").read())
        self.assertEqual(first["text"], self.fake.text)
        again = json.loads(self.request("GET", f"/api/screen?surface_id={SURF_ID}&after_revision=rev-1").read())
        self.assertFalse(again["changed"])
        waits = [c["params"] for c in self.fake.calls if c["method"] == "surface.wait_text_change"]
        self.assertNotIn("after_revision", waits[0])
        self.assertEqual(waits[1]["after_revision"], "rev-1")
        self.assertEqual(waits[1]["timeout_ms"], server.WAIT_TIMEOUT_MS)

    def test_sse_delivers_unredacted_notification(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/events", headers={"Cookie": f"{server.COOKIE_NAME}={self.token}"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)

        def next_message() -> Dict[str, Any]:
            while True:
                line = resp.fp.readline().decode()
                if line.startswith("data: "):
                    return json.loads(line[len("data: "):])

        self.assertEqual(next_message()["type"], "status")
        self._wait(lambda: len(self.hub.subscribers) == 1)
        self.fake.push_event({"type": "event", "seq": 5, "name": "surface.input_sent", "category": "surface"})
        self.fake.push_event({
            "type": "event", "seq": 6, "name": "notification.created", "category": "notification",
            "workspace_id": WS_ID, "surface_id": SURF_ID,
            "payload": {"notification_id": NOTIF_ID, "title": None, "body": None,
                        "redacted_fields": ["title", "subtitle", "body"]},
        })
        msg = next_message()
        self.assertEqual(msg["name"], "notification.created")  # input_sent was skipped
        self.assertEqual(msg["notification"]["title"], "Claude is waiting")
        self.assertEqual(self.hub.last_seq, 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
