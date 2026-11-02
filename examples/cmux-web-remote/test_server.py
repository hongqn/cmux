#!/usr/bin/env python3
"""End-to-end tests for server.py against a fake cmux socket.

Runs anywhere (no cmux app needed):

    python3 examples/cmux-web-remote/test_server.py
"""

from __future__ import annotations

import contextlib
import gzip
import io
import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agentchat  # noqa: E402
import panels  # noqa: E402
import qrterm  # noqa: E402
import server  # noqa: E402

WS_ID = "11111111-1111-1111-1111-111111111111"
SURF_ID = "22222222-2222-2222-2222-222222222222"
WIN_ID = "33333333-3333-3333-3333-333333333333"
NOTIF_ID = "44444444-4444-4444-4444-444444444444"
SURF2_ID = "55555555-5555-5555-5555-555555555555"
BROWSER_ID = "66666666-6666-6666-6666-666666666666"
PANE1_ID = "77777777-7777-7777-7777-777777777777"
PANE2_ID = "88888888-8888-8888-8888-888888888888"

# A render grid in cmux.render-grid.v1 shape (trimmed to the fields used).
NOTIF2_ID = "99999999-9999-9999-9999-999999999999"
HOME = os.path.expanduser("~")

# The v1 `sidebar_state` text cmux 0.65 prints (format from
# ControlCommandCoordinator+SidebarReportsV1.swift).
SIDEBAR_STATE = [
    f"tab={WS_ID}",
    "color=#C0392B",
    f"cwd={HOME}/src/api",
    "focused_cwd=unknown",
    "focused_panel=unknown",
    "git_branch=main dirty",
    "pr=none",
    "pr_label=none",
    "ports=8787",
    "progress=0.40 Indexing",
    "status_count=3",
    "  claude_code=Running icon=bolt.fill color=#4C8DFF work=running",
    "  program_status=claude-code · Working icon=bolt.fill priority=2",
    "  docs=[spec](https://example.com/spec) format=markdown url=https://example.com/spec",
    "meta_block_count=0",
    "log_count=7",
    "  [info] first",
    "  [info] second",
    "  [warning] disk almost full",
    "  [info] third",
    "  [success] 合并。 latest wins",
]

RENDER_GRID = {
    "format": "cmux.render-grid.v1",
    "columns": 20, "rows": 2, "scrollback_rows": 1,
    "terminal_foreground": "#FFFFFF", "terminal_background": "#1E1E1E",
    "cursor": {"row": 1, "column": 4, "visible": True, "style": "block"},
    "styles": [
        {"id": 0, "foreground": "#FFFFFF", "foreground_source": "default",
         "background": "#1E1E1E", "background_source": "default"},
        {"id": 1, "foreground": "#CC372E", "foreground_source": "palette",
         "background": "#1E1E1E", "background_source": "default", "bold": True},
        {"id": 2, "foreground": "#FFFFFF", "foreground_source": "default",
         "background": "#1E1E1E", "background_source": "default", "inverse": True},
    ],
    "scrollback_spans": [{"row": 0, "column": 0, "cell_width": 3, "text": "old", "style_id": 0}],
    "row_spans": [
        {"row": 0, "column": 0, "cell_width": 5, "text": "error", "style_id": 1},
        {"row": 0, "column": 8, "cell_width": 2, "text": "\u4e2d", "style_id": 0},
        {"row": 1, "column": 0, "cell_width": 2, "text": "$ ", "style_id": 2},
    ],
}


class FakeCmux:
    """Speaks the subset of the cmux v2 socket protocol the server uses."""

    def __init__(self, path: str, password: str = ""):
        self.path = path
        self.password = password
        self.calls: List[Dict[str, Any]] = []
        self.auth_lines: List[str] = []
        self.stream_conns: List[socket.socket] = []
        self.text = "$ echo hi\nhi\n"
        self.text2 = "server listening\n"
        # v1 `sidebar_state --tab=<id>` replies, by workspace id.
        self.sidebar_states: Dict[str, List[str]] = {WS_ID: list(SIDEBAR_STATE)}
        self.notifications: List[Dict[str, Any]] = [
            {"id": NOTIF_ID, "workspace_id": WS_ID, "surface_id": SURF_ID,
             "title": "Claude is waiting", "body": "Approve Bash?", "is_read": False},
            {"id": NOTIF2_ID, "workspace_id": WS_ID, "surface_id": SURF2_ID,
             "title": "Build done", "body": "", "is_read": True},
        ]
        # chat.sessions.dump records; mobile.chat.send answers not_found when chat_send_lost.
        self.chat_sessions: List[Dict[str, Any]] = []
        self.chat_send_lost = False
        self.screenshot = "iVBORw0KGgo="
        # Mimic cmuxOnly refusing a process outside cmux's process tree.
        self.deny = False
        # Mimic cmux builds (0.65.x) without surface.wait_text_change.
        self.no_wait_method = False
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
                if self.deny:
                    f.write(b"ERROR: Access denied - only processes started inside cmux can connect\n")
                    f.flush()
                    return
                if line.startswith("sidebar_state"):
                    self.calls.append({"method": "v1:" + line, "params": {}})
                    tab = line.partition("--tab=")[2].split()[0] if "--tab=" in line else ""
                    lines = self.sidebar_states.get(tab, ["ERROR: Tab not found"])
                    f.write(("\n".join(lines) + "\n").encode())
                    f.flush()
                    continue
                req = json.loads(line)
                self.calls.append(req)
                if req["method"] == "events.stream":
                    f.write(json.dumps({"type": "ack", "boot_id": "boot", "resume": {"gap": False}}).encode() + b"\n")
                    f.flush()
                    self.stream_conns.append(conn)
                    threading.Event().wait()  # keep the stream open
                if self.no_wait_method and req["method"] == "surface.wait_text_change":
                    err = {"code": "method_not_found", "message": "Unknown method"}
                    f.write(json.dumps({"id": req["id"], "ok": False, "error": err}).encode() + b"\n")
                    f.flush()
                    continue
                result = self._result(req["method"], req.get("params") or {})
                if result is None:
                    err = {"code": "not_found", "message": "Terminal surface not found"}
                    f.write(json.dumps({"id": req["id"], "ok": False, "error": err}).encode() + b"\n")
                    f.flush()
                    continue
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
        if method == "extension.sidebar.snapshot":
            return {"window_id": WIN_ID, "workspaces": [{
                "id": WS_ID, "title": "api", "unread_count": 1,
                "latest_notification_text": "Claude is waiting: Approve Bash?",
                "description": "Ship the **API**", "custom_color": "#C0392B", "pinned": True,
                "current_directory": HOME + "/src/api", "branch_summary": "main",
                "git_branches": [{"branch": "main", "dirty": True}],
                "pull_request_urls": ["https://github.com/o/r/pull/7"], "listening_ports": [8787],
                "latest_conversation_message": "ship it",
            }]}
        if method == "notification.dismiss":
            self.notifications = [n for n in self.notifications if n["id"] != params.get("id")]
            return {"dismissed": 1}
        if method == "notification.mark_read":
            return {"marked_read": 1}
        if method == "workspace.list":
            return {"workspaces": [{"id": WS_ID, "ref": "workspace:1", "title": "api", "selected": True,
                                    "current_directory": "/src/api"}]}
        if method == "system.tree":
            def surf(sid, pane, kind, title, selected=True):
                return {"id": sid, "ref": "surface:" + sid[:1], "type": kind, "title": title,
                        "pane_id": pane, "selected_in_pane": selected, "url": None}
            return {"windows": [{"id": WIN_ID, "ref": "window:1", "workspaces": [{
                "id": WS_ID, "ref": "workspace:1", "title": "api", "selected": True,
                "layout": {"direction": "horizontal", "split": 0.4, "children": [
                    {"pane": {"id": PANE1_ID}}, {"pane": {"id": PANE2_ID}}]},
                "panes": [
                    {"id": PANE1_ID, "ref": "pane:1", "focused": True, "selected_surface_id": SURF_ID,
                     "surfaces": [surf(SURF_ID, PANE1_ID, "terminal", "zsh")]},
                    {"id": PANE2_ID, "ref": "pane:2", "focused": False, "selected_surface_id": SURF2_ID,
                     "surfaces": [surf(SURF2_ID, PANE2_ID, "terminal", "server"),
                                  surf(BROWSER_ID, PANE2_ID, "browser", "docs", selected=False)]},
                ],
            }]}]}
        if method == "terminal.replay":
            if params.get("surface_id") != SURF_ID:
                return None  # _serve answers not_found
            return {"surface_id": SURF_ID, "render_grid": RENDER_GRID}
        if method == "surface.list":
            return {"surfaces": [{"id": SURF_ID, "ref": "surface:1", "type": "terminal", "title": "zsh", "focused": True}]}
        if method == "surface.read_text":
            text = self.text if params.get("surface_id") == SURF_ID else self.text2
            return {"text": text, "surface_id": params.get("surface_id")}
        if method == "surface.wait_text_change":
            changed = params.get("after_revision") != "rev-1"
            text = self.text if params.get("surface_id") == SURF_ID else self.text2
            return {"text": text, "revision": "rev-1", "changed": changed}
        if method == "notification.list":
            return {"notifications": [dict(n) for n in self.notifications]}
        if method == "browser.screenshot":
            return {"png_base64": self.screenshot, "surface_id": params.get("surface_id")}
        if method == "browser.url.get":
            return {"url": "https://example.com/"}
        if method == "browser.eval":
            return {"value": "a"}
        if method == "workspace.create":
            return {"workspace_id": "new-ws", "workspace_ref": "workspace:9"}
        if method == "chat.sessions.dump":
            return {"sessions": [dict(x) for x in self.chat_sessions]}
        if method in ("mobile.chat.send", "mobile.chat.interrupt") and self.chat_send_lost:
            return None  # _serve answers not_found
        return {}

    def push_event(self, frame: Dict[str, Any]) -> None:
        for conn in self.stream_conns:
            conn.sendall(json.dumps(frame).encode() + b"\n")

    def methods(self) -> List[str]:
        return [c["method"] for c in self.calls]

    def close(self) -> None:
        self.sock.close()


class ServerHarness(unittest.TestCase):
    """Runs server.py on a free port against a FakeCmux."""

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

    def open_events(self):
        """Subscribe to /api/events; returns a function that reads the next message."""
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
        return next_message

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


class ServerTest(ServerHarness):
    def test_api_requires_token(self) -> None:
        self.assertEqual(self.request("GET", "/api/tree", auth=False).status, 401)
        self.assertEqual(self.request("POST", "/api/send", {"surface_id": SURF_ID, "text": "x"}, auth=False).status, 401)
        bad = self.request("GET", "/api/tree", auth=False, headers={"Cookie": f"{server.COOKIE_NAME}=nope"})
        self.assertEqual(bad.status, 401)
        self.assertNotIn("surface.send_text", self.fake.methods())

    def test_login_sets_strict_http_only_cookie(self) -> None:
        resp = self.request("GET", f"/login?token={self.token}", auth=False)
        self.assertEqual(resp.status, 200)
        cookie = resp.getheader("Set-Cookie")
        self.assertIn(f"{server.COOKIE_NAME}={self.token}", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(self.request("GET", "/login?token=wrong", auth=False).status, 401)

    def test_login_continues_with_a_same_site_navigation(self) -> None:
        # Opening the link from the iPhone camera is a cross-site navigation, and
        # an HTTP redirect stays part of it, so Safari would not send the new
        # SameSite=Strict cookie to "/" and show the login page again. The page
        # itself has to navigate to "/".
        resp = self.request("GET", f"/login?token={self.token}", auth=False)
        body = resp.read().decode()
        self.assertEqual(resp.status, 200)
        self.assertIsNone(resp.getheader("Location"))
        self.assertIn('http-equiv="refresh" content="0;url=/"', body)
        self.assertIn('location.replace("/")', body)
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(resp.getheader("Referrer-Policy"), "no-referrer")

    def test_home_screen_manifest_and_icons_need_no_login(self) -> None:
        resp = self.request("GET", "/manifest.webmanifest", auth=False)
        self.assertEqual(resp.status, 200)
        manifest = json.loads(resp.read())
        self.assertEqual(manifest["display"], "standalone")
        for icon in manifest["icons"] + [{"src": "/apple-touch-icon.png", "sizes": "180x180"}]:
            resp = self.request("GET", icon["src"], auth=False)
            body = resp.read()
            self.assertEqual(resp.status, 200)
            self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n")
            side = int(icon["sizes"].split("x")[0])
            self.assertEqual((int.from_bytes(body[16:20], "big"), int.from_bytes(body[20:24], "big")), (side, side))

    def test_successful_requests_are_not_logged(self) -> None:
        # The server usually runs in a cmux terminal that the browser is
        # watching: a log line per poll changes that terminal, which answers
        # the next poll at once, which logs again, forever.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.request("POST", "/api/screens", {"surfaces": {SURF_ID: ""}}).read()
            self.request("GET", "/api/tree").read()
            self.request("GET", "/api/tree", auth=False).read()  # 401 is still logged
        self.assertNotIn("/api/screens", err.getvalue())
        self.assertNotIn('"GET /api/tree HTTP/1.1" 200', err.getvalue())
        self.assertIn('"GET /api/tree HTTP/1.1" 401', err.getvalue())

    def test_access_log_flag_logs_everything(self) -> None:
        self.httpd.RequestHandlerClass.access_log = True
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.request("GET", "/api/tree").read()
        self.assertIn('"GET /api/tree HTTP/1.1" 200', err.getvalue())

    def test_bearer_token_works_for_scripts(self) -> None:
        resp = self.request("GET", "/api/tree", auth=False, headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(resp.status, 200)

    def test_socket_password_is_sent(self) -> None:
        self.request("GET", "/api/tree").read()
        self.assertIn("auth " + self.PASSWORD, self.fake.auth_lines)

    def test_tree_has_layout_panes_and_tabs(self) -> None:
        tree = json.loads(self.request("GET", "/api/tree").read())
        ws = tree["windows"][0]["workspaces"][0]
        self.assertEqual(ws["id"], WS_ID)
        self.assertEqual(ws["directory"], "~/src/api")  # merged from extension.sidebar.snapshot
        self.assertEqual(ws["layout"]["direction"], "horizontal")
        self.assertEqual(ws["layout"]["split"], 0.4)
        self.assertEqual([p["id"] for p in ws["panes"]], [PANE1_ID, PANE2_ID])
        self.assertEqual([s["type"] for s in ws["panes"][1]["surfaces"]], ["terminal", "browser"])
        self.assertEqual(ws["panes"][1]["selected_surface_id"], SURF2_ID)
        # One snapshot call, not one surface.list per workspace.
        self.assertNotIn("surface.list", self.fake.methods())

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

    def screens(self, revisions: Dict[str, str], headers: Dict[str, str] = None) -> Dict[str, Any]:
        resp = self.request("POST", "/api/screens", {"surfaces": revisions}, headers=headers)
        self.assertEqual(resp.status, 200)
        return json.loads(resp.read())["screens"]

    def test_single_screen_long_polls_with_native_wait(self) -> None:
        first = self.screens({SURF_ID: ""})[SURF_ID]
        self.assertEqual(first["text"], self.fake.text)
        again = self.screens({SURF_ID: "rev-1"})[SURF_ID]
        self.assertFalse(again["changed"])
        self.assertNotIn("grid", again)  # unchanged: nothing to redraw
        waits = [c["params"] for c in self.fake.calls if c["method"] == "surface.wait_text_change"]
        self.assertNotIn("after_revision", waits[0])
        self.assertEqual(waits[1]["after_revision"], "rev-1")
        self.assertEqual(waits[1]["timeout_ms"], server.WAIT_TIMEOUT_MS)

    def test_screen_carries_colored_grid(self) -> None:
        grid = self.screens({SURF_ID: ""})[SURF_ID]["grid"]
        self.assertEqual((grid["columns"], grid["rows"]), (20, 2))
        # scrollback line first, then the viewport rows.
        self.assertEqual(len(grid["lines"]), 3)
        self.assertEqual(grid["lines"][0], [[0, 3, "old", 0]])
        self.assertEqual(grid["lines"][1], [[0, 5, "error", 1], [8, 2, "\u4e2d", 0]])
        self.assertIn("color:#CC372E", grid["styles"]["1"])
        self.assertIn("font-weight:bold", grid["styles"]["1"])
        self.assertEqual(grid["styles"]["0"], "")  # default style adds nothing
        # inverse swaps the terminal colors.
        self.assertIn("color:#1E1E1E", grid["styles"]["2"])
        self.assertIn("background:#FFFFFF", grid["styles"]["2"])
        self.assertEqual(grid["cursor"], {"line": 2, "column": 4, "visible": True})
        self.assertEqual((grid["foreground"], grid["background"]), ("#FFFFFF", "#1E1E1E"))

    def test_screen_without_grid_falls_back_to_text(self) -> None:
        screen = self.screens({SURF2_ID: ""})[SURF2_ID]
        self.assertEqual(screen["text"], "server listening\n")
        self.assertNotIn("grid", screen)

    def test_screens_answers_several_surfaces(self) -> None:
        result = self.screens({SURF_ID: "", SURF2_ID: ""})
        self.assertEqual(set(result), {SURF_ID, SURF2_ID})
        self.assertTrue(all(r["changed"] for r in result.values()))

    def test_screens_gzip(self) -> None:
        self.fake.text = "x" * 4096  # above the 1 KiB compression threshold
        resp = self.request("POST", "/api/screens", {"surfaces": {SURF_ID: ""}},
                            headers={"Accept-Encoding": "gzip"})
        self.assertEqual(resp.getheader("Content-Encoding"), "gzip")
        body = json.loads(gzip.decompress(resp.read()))
        self.assertIn(SURF_ID, body["screens"])

    def test_screens_rejects_bad_input(self) -> None:
        self.assertEqual(self.request("POST", "/api/screens", {"surfaces": []}).status, 400)
        many = {f"{i:08d}-0000-0000-0000-000000000000": "" for i in range(server.MAX_SCREENS + 1)}
        self.assertEqual(self.request("POST", "/api/screens", {"surfaces": many}).status, 400)

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


class SidebarParityTest(ServerHarness):
    """The web sidebar shows what cmux's own sidebar row shows."""

    def test_parse_sidebar_state(self) -> None:
        state = server.parse_sidebar_state(SIDEBAR_STATE)
        # Highest priority first; ties keep cmux's order.
        self.assertEqual([e["key"] for e in state["statuses"]], ["program_status", "claude_code", "docs"])
        working = state["statuses"][0]
        self.assertEqual(working["value"], "claude-code · Working")
        self.assertEqual((working["icon"], working["priority"]), ("bolt.fill", 2))
        running = state["statuses"][1]
        self.assertEqual((running["color"], running["work"]), ("#4C8DFF", "running"))
        docs = state["statuses"][2]
        self.assertEqual((docs["format"], docs["url"]), ("markdown", "https://example.com/spec"))
        self.assertEqual(state["log"], {"level": "success", "message": "合并。 latest wins"})
        self.assertEqual(state["progress"], {"value": 0.4, "label": "Indexing"})
        self.assertTrue(state["working"])
        self.assertTrue(state["git_dirty"])

    def test_parse_empty_sidebar_state(self) -> None:
        state = server.parse_sidebar_state(["tab=x", "progress=none", "status_count=0", "log_count=0"])
        self.assertEqual((state["statuses"], state["log"], state["progress"], state["working"]), ([], None, None, False))

    def test_v1_reply_reads_to_the_end_of_the_log(self) -> None:
        started = time.time()
        lines = self.hub.sidebar.client_v1_lines(f"sidebar_state --tab={WS_ID}")
        self.assertEqual(lines, SIDEBAR_STATE)
        # Ends on the log section, not on an idle timeout.
        self.assertLess(time.time() - started, server.V1_IDLE_SECONDS)

    def test_tree_rows_match_the_cmux_sidebar(self) -> None:
        self.hub.sidebar.refresh([WS_ID])
        ws = json.loads(self.request("GET", "/api/tree").read())["windows"][0]["workspaces"][0]
        self.assertEqual(ws["unread_count"], 1)
        self.assertEqual(ws["latest_notification_text"], "Claude is waiting: Approve Bash?")
        self.assertEqual(ws["description"], "Ship the **API**")
        self.assertEqual(ws["custom_color"], "#C0392B")
        self.assertTrue(ws["pinned"])
        self.assertEqual(ws["directory"], "~/src/api")  # home abbreviated, as cmux shows it
        self.assertEqual(ws["branch"], "main")
        self.assertTrue(ws["git_dirty"])
        self.assertEqual(ws["pull_requests"], ["https://github.com/o/r/pull/7"])
        self.assertEqual(ws["ports"], [8787])
        self.assertEqual(ws["sidebar"]["statuses"][0]["value"], "claude-code · Working")
        self.assertEqual(ws["sidebar"]["log"]["message"], "合并。 latest wins")
        self.assertIn("extension.sidebar.snapshot", self.fake.methods())

    def test_tree_fetches_missing_sidebar_state_in_background(self) -> None:
        ws = json.loads(self.request("GET", "/api/tree").read())["windows"][0]["workspaces"][0]
        self.assertIsNone(ws["sidebar"])  # not fetched yet: the tree does not wait for it
        self._wait(lambda: self.hub.sidebar.get(WS_ID) is not None)

    def test_sidebar_event_refreshes_row_and_notifies_browser(self) -> None:
        self.hub.sidebar.refresh([WS_ID])
        next_message = self.open_events()
        self.fake.sidebar_states[WS_ID] = [
            f"tab={WS_ID}", "progress=none", "status_count=1",
            "  claude_code=Idle icon=pause.circle.fill color=#8E8E93", "log_count=0",
        ]
        self.fake.push_event({"type": "event", "seq": 9, "name": "program_status.changed",
                              "category": "terminal", "workspace_id": WS_ID})
        msg = next_message()
        self.assertEqual(msg["type"], "sidebar")
        self.assertEqual(msg["workspace_id"], WS_ID)
        self.assertEqual(msg["sidebar"]["statuses"][0]["value"], "Idle")
        self.assertFalse(msg["sidebar"]["working"])
        self.assertEqual(self.hub.sidebar.get(WS_ID)["statuses"][0]["value"], "Idle")

    def test_events_subscribe_to_sidebar_and_terminal(self) -> None:
        stream = [c for c in self.fake.calls if c["method"] == "events.stream"][0]
        self.assertTrue({"sidebar", "terminal"} <= set(stream["params"]["categories"]))

    def test_clear_all_dismisses_every_notification(self) -> None:
        resp = self.request("POST", "/api/notifications/clear_all", {})
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.read())["dismissed"], 2)
        dismissed = [c["params"]["id"] for c in self.fake.calls if c["method"] == "notification.dismiss"]
        self.assertEqual(sorted(dismissed), sorted([NOTIF_ID, NOTIF2_ID]))

    def test_mark_read_for_a_pane(self) -> None:
        resp = self.request("POST", "/api/notifications/read", {"workspace_id": WS_ID, "surface_id": SURF_ID})
        self.assertEqual(resp.status, 200)
        call = [c for c in self.fake.calls if c["method"] == "notification.mark_read"][-1]
        self.assertEqual(call["params"], {"workspace_id": WS_ID, "surface_id": SURF_ID})


class ReadTextFallbackTest(ServerHarness):
    """Older cmux has no surface.wait_text_change: poll surface.read_text."""

    def setUp(self) -> None:
        super().setUp()
        self.fake.no_wait_method = True
        self._saved = (server.WAIT_TIMEOUT_MS, server.FALLBACK_POLL_SECONDS)
        server.WAIT_TIMEOUT_MS, server.FALLBACK_POLL_SECONDS = 1500, 0.05

    def tearDown(self) -> None:
        server.WAIT_TIMEOUT_MS, server.FALLBACK_POLL_SECONDS = self._saved
        super().tearDown()

    def screen(self, revision: str = "") -> Dict[str, Any]:
        resp = self.request("POST", "/api/screens", {"surfaces": {SURF_ID: revision}})
        return json.loads(resp.read())["screens"][SURF_ID]

    def test_first_read_returns_text_and_revision(self) -> None:
        first = self.screen()
        self.assertEqual(first["text"], self.fake.text)
        self.assertTrue(first["revision"])
        self.assertIn("surface.read_text", self.fake.methods())

    def test_wait_returns_when_text_changes(self) -> None:
        first = self.screen()
        threading.Timer(0.3, lambda: setattr(self.fake, "text", "new output\n")).start()
        started = time.time()
        second = self.screen(first["revision"])
        self.assertTrue(second["changed"])
        self.assertEqual(second["text"], "new output\n")
        self.assertNotEqual(second["revision"], first["revision"])
        self.assertLess(time.time() - started, 1.4)

    def test_multi_wait_returns_only_changed_surface(self) -> None:
        resp = self.request("POST", "/api/screens", {"surfaces": {SURF_ID: "", SURF2_ID: ""}})
        first = json.loads(resp.read())["screens"]
        revs = {sid: first[sid]["revision"] for sid in first}
        threading.Timer(0.3, lambda: setattr(self.fake, "text2", "GET /health 200\n")).start()
        resp = self.request("POST", "/api/screens", {"surfaces": revs})
        second = json.loads(resp.read())["screens"]
        self.assertFalse(second[SURF_ID]["changed"])
        self.assertTrue(second[SURF2_ID]["changed"])
        self.assertEqual(second[SURF2_ID]["text"], "GET /health 200\n")

    def test_wait_times_out_unchanged(self) -> None:
        first = self.screen()
        second = self.screen(first["revision"])
        self.assertFalse(second["changed"])
        self.assertEqual(second["revision"], first["revision"])


class AccessDeniedTest(unittest.TestCase):
    """cmux answers a plain-text ERROR line when its socket mode refuses us."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.fake = FakeCmux(os.path.join(self.tmp, "cmux.sock"))
        self.fake.deny = True
        self.client = server.CmuxClient(self.fake.path, None)

    def tearDown(self) -> None:
        self.fake.close()

    def test_call_raises_access_denied(self) -> None:
        with self.assertRaises(server.CmuxError) as ctx:
            self.client.call("system.ping")
        self.assertEqual(ctx.exception.code, "access_denied")
        self.assertIn("inside cmux", ctx.exception.message)

    def test_api_reports_access_denied(self) -> None:
        httpd = server.make_server("127.0.0.1", 0, self.client, server.EventHub(self.client), "t")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            conn.request("GET", "/api/tree", headers={"Authorization": "Bearer t"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 502)
            self.assertEqual(json.loads(resp.read())["error"], "access_denied")
        finally:
            httpd.shutdown()
            httpd.server_close()


def jl(obj: Dict[str, Any]) -> bytes:
    return json.dumps(obj).encode() + b"\n"


TRANSCRIPT = b"".join([
    b'{"type":"api-request-blob","payload":"' + b"x" * 5000 + b'"}\n',
    jl({"parentUuid": None, "isSidechain": False, "message": {"role": "user", "content": "fix the tests"},
        "type": "user", "uuid": "u1", "timestamp": "2026-11-02T08:00:00Z"}),
    jl({"isSidechain": False, "message": {"role": "user", "content": "<command-name>/clear</command-name>\n<command-args></command-args>"},
        "type": "user", "uuid": "u2"}),
    jl({"isSidechain": False, "isMeta": True, "message": {"role": "user", "content": "meta"}, "type": "user", "uuid": "u3"}),
    jl({"isSidechain": False, "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": ""},
        {"type": "text", "text": "Running **them** now."},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q", "description": "Run tests"}},
    ]}, "type": "assistant", "uuid": "a1"}),
    jl({"isSidechain": True, "message": {"role": "assistant", "content": [{"type": "text", "text": "subagent"}]},
        "type": "assistant", "uuid": "s1"}),
    jl({"isSidechain": False, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "2 failed", "is_error": True}]},
        "type": "user", "uuid": "u4"}),
    jl({"isSidechain": False, "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t2", "name": "Edit",
         "input": {"file_path": "/src/a.py", "old_string": "a = 1\nb = 2", "new_string": "a = 1\nb = 3\nc = 4"}},
        {"type": "tool_use", "id": "t3", "name": "AskUserQuestion", "input": {"questions": [
            {"question": "Which fix?", "header": "Fix", "options": [
                {"label": "Patch", "description": "small"}, {"label": "Rewrite", "description": "big"}]}]}},
    ]}, "type": "assistant", "uuid": "a2"}),
])


class AgentChatParseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.jsonl")
        with open(self.path, "wb") as fh:
            fh.write(TRANSCRIPT)

    def test_transcript_becomes_chat_items(self) -> None:
        page = agentchat.read_tail(self.path)
        kinds = [(i["kind"], i.get("text") or i.get("tool") or i.get("prompt")) for i in page["items"]]
        self.assertEqual(kinds, [
            ("user", "fix the tests"), ("user", "/clear"), ("assistant", "Running **them** now."),
            ("tool", "Bash"), ("result", "2 failed"), ("tool", "Edit"), ("question", "Which fix?"),
        ])
        bash, result, edit, question = page["items"][3], page["items"][4], page["items"][5], page["items"][6]
        self.assertEqual((bash["id"], bash["summary"], bash["note"]), ("t1", "pytest -q", "Run tests"))
        self.assertEqual((result["call_id"], result["is_error"]), ("t1", True))
        self.assertEqual((edit["path"], edit["additions"], edit["deletions"]), ("/src/a.py", 2, 1))
        self.assertEqual([o["label"] for o in question["options"]], ["Patch", "Rewrite"])
        self.assertEqual(question["call_id"], "t3")
        self.assertEqual(page["end"], len(TRANSCRIPT))

    def test_pages_backwards_and_reads_forward(self) -> None:
        last = agentchat.read_tail(self.path, want=1, chunk=300)
        self.assertGreater(last["start"], 0)
        earlier = agentchat.read_tail(self.path, before=last["start"], chunk=300)
        self.assertEqual(earlier["start"], 0)
        ids = [i["id"] for i in earlier["items"] + last["items"]]
        self.assertEqual(ids, [i["id"] for i in agentchat.read_tail(self.path)["items"]])
        line = jl({"message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
                   "type": "assistant", "uuid": "a3"})
        with open(self.path, "ab") as fh:
            fh.write(line + b'{"type":"assistant","partial')  # a line still being written
        update = agentchat.read_since(self.path, len(TRANSCRIPT))
        self.assertEqual([i["text"] for i in update["items"]], ["done"])
        self.assertEqual(update["end"], len(TRANSCRIPT) + len(line))

    def test_long_final_line_is_kept(self) -> None:
        big = jl({"message": {"role": "assistant", "content": [{"type": "text", "text": "y" * 2000}]},
                  "type": "assistant", "uuid": "big"})
        with open(self.path, "ab") as fh:
            fh.write(big)
        page = agentchat.read_tail(self.path, chunk=300)
        self.assertEqual(page["items"][-1]["id"], "big:0")
        self.assertEqual(page["end"], len(TRANSCRIPT) + len(big))

    def test_session_state(self) -> None:
        self.assertEqual(agentchat.session_state("working(since: 2026-11-02 08:45:00 +0000)"), "working")
        self.assertEqual(agentchat.session_state("needsInput(since: 2026-11-02 08:45:00 +0000)"), "needs_input")
        self.assertEqual(agentchat.session_state("idle"), "idle")


class AgentChatServerTest(ServerHarness):
    def setUp(self) -> None:
        super().setUp()
        self.transcript = os.path.join(self.tmp, "session.jsonl")
        with open(self.transcript, "wb") as fh:
            fh.write(TRANSCRIPT)
        self.fake.chat_sessions = [
            {"session_id": "sess-1", "agent": "claude", "surface_id": SURF_ID.upper(), "workspace_id": WS_ID,
             "transcript_path": self.transcript, "state": "working(since: 2026-11-02 08:45:00 +0000)",
             "last_activity": 2.0},
            {"session_id": "old", "agent": "claude", "surface_id": SURF_ID, "workspace_id": WS_ID,
             "transcript_path": self.transcript, "state": "ended", "last_activity": 9.0},
            {"session_id": "codex-1", "agent": "codex", "surface_id": SURF2_ID, "workspace_id": WS_ID,
             "transcript_path": self.transcript, "state": "idle"},
        ]

    def post(self, path: str, body: Dict[str, Any]) -> Any:
        resp = self.request("POST", path, body, headers={"Origin": f"http://127.0.0.1:{self.port}"})
        return resp.status, json.loads(resp.read() or b"null")

    def test_tree_marks_agent_terminals(self) -> None:
        tree = json.loads(self.request("GET", "/api/tree").read())
        surfaces = {s["id"]: s for p in tree["windows"][0]["workspaces"][0]["panes"] for s in p["surfaces"]}
        self.assertEqual(surfaces[SURF_ID]["agent"],
                         {"session_id": "sess-1", "agent": "claude", "state": "working", "chat": True})
        self.assertFalse(surfaces[SURF2_ID]["agent"]["chat"])
        self.assertIsNone(surfaces[BROWSER_ID]["agent"])
        self.assertNotIn(self.transcript, json.dumps(tree))

    def test_history_and_poll(self) -> None:
        status, page = self.post("/api/chat/history", {"session_id": "sess-1"})
        self.assertEqual(status, 200)
        self.assertEqual(page["state"], "working")
        self.assertEqual(len(page["items"]), 7)
        done = threading.Event()
        got: Dict[str, Any] = {}

        def poll() -> None:
            got["r"] = self.post("/api/chat/poll", {"session_id": "sess-1", "after": page["end"], "state": "working"})
            done.set()

        threading.Thread(target=poll, daemon=True).start()
        time.sleep(0.6)
        self.assertFalse(done.is_set(), "poll should wait for new transcript lines")
        with open(self.transcript, "ab") as fh:
            fh.write(jl({"message": {"role": "assistant", "content": [{"type": "text", "text": "All green."}]},
                         "type": "assistant", "uuid": "a9"}))
        self.assertTrue(done.wait(5))
        status, update = got["r"]
        self.assertEqual([i["text"] for i in update["items"]], ["All green."])

    def test_poll_returns_when_state_changes(self) -> None:
        self.fake.chat_sessions[0]["state"] = "needsInput(since: 2026-11-02 08:46:00 +0000)"
        status, update = self.post("/api/chat/poll", {"session_id": "sess-1", "after": len(TRANSCRIPT), "state": "working"})
        self.assertEqual((status, update["state"], update["items"]), (200, "needs_input", []))

    def test_unknown_and_unsupported_sessions(self) -> None:
        self.assertEqual(self.post("/api/chat/history", {"session_id": "nope"})[0], 404)
        self.assertEqual(self.post("/api/chat/history", {"session_id": "codex-1"})[0], 409)

    def test_send_goes_through_mobile_chat_send(self) -> None:
        status, _ = self.post("/api/chat/send", {"session_id": "sess-1", "text": "line one\nline two"})
        self.assertEqual(status, 200)
        call = [c for c in self.fake.calls if c["method"] == "mobile.chat.send"][-1]
        self.assertEqual(call["params"], {"session_id": "sess-1", "text": "line one\nline two"})

    def test_send_falls_back_to_paste_when_binding_is_lost(self) -> None:
        self.fake.chat_send_lost = True
        status, _ = self.post("/api/chat/send", {"session_id": "sess-1", "text": "hi"})
        self.assertEqual(status, 200)
        tail = [(c["method"], c["params"].get("key") or c["params"].get("text")) for c in self.fake.calls[-4:]]
        self.assertEqual(tail, [("surface.send_key", "ctrl+a"), ("surface.send_key", "ctrl+k"),
                                ("surface.send_key", "ctrl+u"), ("terminal.paste", "hi")])

    def test_interrupt(self) -> None:
        status, _ = self.post("/api/chat/interrupt", {"session_id": "sess-1"})
        self.assertEqual(status, 200)
        self.assertEqual([c for c in self.fake.calls if c["method"] == "mobile.chat.interrupt"][-1]["params"],
                         {"session_id": "sess-1", "hard": False})


class PanelsTest(ServerHarness):
    def post(self, path: str, body: Dict[str, Any]) -> Any:
        resp = self.request("POST", path, body, headers={"Origin": f"http://127.0.0.1:{self.port}"})
        return resp.status, json.loads(resp.read() or b"null")

    def last(self, method: str) -> Dict[str, Any]:
        return [c for c in self.fake.calls if c["method"] == method][-1]["params"]

    def test_assets(self) -> None:
        resp = self.request("GET", "/assets/app.js")
        self.assertEqual(resp.status, 200)
        self.assertIn("javascript", resp.getheader("Content-Type"))
        self.assertEqual(self.request("GET", "/assets/..%2Fserver.py").status, 404)
        self.assertEqual(self.request("GET", "/assets/server.py").status, 404)

    def test_page_allows_data_images(self) -> None:
        self.assertIn("img-src 'self' data:", self.request("GET", "/").getheader("Content-Security-Policy"))

    def test_browser_shot_holds_the_viewport_and_skips_unchanged_images(self) -> None:
        status, first = self.post("/api/browser/shot", {"surface_id": BROWSER_ID, "width": 390, "height": 700})
        self.assertEqual((status, first["png"], first["url"]), (200, self.fake.screenshot, "https://example.com/"))
        self.assertEqual(self.last("browser.viewport.set"), {"surface_id": BROWSER_ID, "width": 390, "height": 700})
        sets = len([c for c in self.fake.calls if c["method"] == "browser.viewport.set"])
        status, second = self.post("/api/browser/shot", {"surface_id": BROWSER_ID, "hash": first["hash"], "width": 390, "height": 700})
        self.assertNotIn("png", second)
        self.assertEqual(len([c for c in self.fake.calls if c["method"] == "browser.viewport.set"]), sets)
        self.post("/api/browser/release", {"surface_id": BROWSER_ID})
        self.assertEqual(self.last("browser.viewport.set"), {"surface_id": BROWSER_ID, "reset": True})

    def test_browser_actions(self) -> None:
        self.assertEqual(self.post("/api/browser/act", {"surface_id": BROWSER_ID, "action": "tap", "fx": 0.5, "fy": 0.25})[0], 200)
        script = self.last("browser.eval")["script"]
        self.assertIn("elementFromPoint", script)
        self.assertIn("0.250000 * innerHeight", script)
        self.post("/api/browser/act", {"surface_id": BROWSER_ID, "action": "type", "text": "it's \"quoted\""})
        self.assertIn(json.dumps("it's \"quoted\""), self.last("browser.eval")["script"])
        self.post("/api/browser/act", {"surface_id": BROWSER_ID, "action": "navigate", "url": "example.org/x"})
        self.assertEqual(self.last("browser.navigate")["url"], "https://example.org/x")
        self.post("/api/browser/act", {"surface_id": BROWSER_ID, "action": "navigate", "url": "localhost:8080"})
        self.assertEqual(self.last("browser.navigate")["url"], "https://localhost:8080")
        bad = [{"action": "navigate", "url": "javascript:alert(1)"}, {"action": "navigate", "url": "file:///etc/passwd"}, {"action": "key", "key": "Meta"},
               {"action": "tap", "fx": "0.5", "fy": 0.1}, {"action": "explode"}]
        for body in bad:
            self.assertEqual(self.post("/api/browser/act", dict(body, surface_id=BROWSER_ID))[0], 400, body)

    def test_file_panels_read_the_path_cmux_saved(self) -> None:
        doc = os.path.join(self.tmp, "notes.md")
        with open(doc, "w") as fh:
            fh.write("# Notes\nhello")
        session = os.path.join(self.tmp, "session-com.cmuxterm.app.json")
        with open(session, "w") as fh:
            json.dump({"windows": [{"panels": [{"id": BROWSER_ID.lower(), "type": "markdown", "markdown": {"filePath": doc}}]}]}, fh)
        self.httpd.RequestHandlerClass.files = panels.SessionFiles(os.path.join(self.tmp, "session-com.cmuxterm.app*.json"))
        status, first = self.post("/api/file", {"surface_id": BROWSER_ID})
        self.assertEqual((status, first["text"], first["name"]), (200, "# Notes\nhello", "notes.md"))
        status, again = self.post("/api/file", {"surface_id": BROWSER_ID, "stamp": first["stamp"]})
        self.assertEqual(again, {"unchanged": True, "stamp": first["stamp"]})
        self.assertEqual(self.post("/api/file", {"surface_id": SURF_ID})[0], 404)

    def test_new_session_starts_claude_with_the_prompt(self) -> None:
        status, r = self.post("/api/session/new", {"cwd": "~/src/api", "prompt": "fix it; rm -rf /"})
        self.assertEqual((status, r["workspace_id"]), (200, "new-ws"))
        self.assertEqual(self.last("workspace.create"),
                         {"cwd": "~/src/api", "initial_input": "claude 'fix it; rm -rf /'\n"})
        self.post("/api/session/new", {"cwd": "/tmp"})
        self.assertEqual(self.last("workspace.create")["initial_input"], "claude\n")


class PhoneAccessTest(unittest.TestCase):
    def test_reed_solomon_matches_iso_example(self) -> None:
        # ISO/IEC 18004 Annex I: "01234567" as version 1-M.
        data = [16, 32, 12, 86, 97, 128, 236, 17, 236, 17, 236, 17, 236, 17, 236, 17]
        self.assertEqual(qrterm.rs_remainder(data, 10), [165, 36, 212, 193, 237, 54, 199, 135, 44, 85])

    def test_format_bits_match_the_spec_table(self) -> None:
        self.assertEqual(qrterm.format_bits("M", 0), 0b101010000010010)
        self.assertEqual(qrterm.format_bits("L", 4), 0b110011000101111)

    def test_login_url_fits_and_has_finder_patterns(self) -> None:
        url = "https://m1max.tailf211.ts.net/login?token=" + "x" * 32
        m = qrterm.encode(url)
        n = len(m)
        self.assertEqual((n - 17) % 4, 0)
        ring = [[m[y][x] for x in range(7)] for y in range(7)]
        self.assertTrue(all(ring[0]) and all(ring[6]) and not any(ring[1][1:6]) and all(ring[3][2:5]))
        lines = qrterm.render(m).split("\n")
        self.assertEqual(len(lines), (n + 8 + 1) // 2)

    def test_too_long_text_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            qrterm.encode("x" * 400)

    def test_tailscale_serve_url_finds_our_proxy(self) -> None:
        status = {"TCP": {"443": {"HTTPS": True}},
                  "Web": {"mac.tail1.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}}}}
        self.assertEqual(server.tailscale_serve_url(8787, status), "https://mac.tail1.ts.net")
        self.assertIsNone(server.tailscale_serve_url(9999, status))
        other = {"Web": {"mac.tail1.ts.net:8443": {"Handlers": {"/": {"Proxy": "localhost:8787"}}}}}
        self.assertEqual(server.tailscale_serve_url(8787, other), "https://mac.tail1.ts.net:8443")
        self.assertIsNone(server.tailscale_serve_url(8787, None))

    def test_phone_url(self) -> None:
        self.assertIsNone(server.phone_url("127.0.0.1", 8787, None, None))
        self.assertIsNone(server.phone_url("0.0.0.0", 8787, None, None))
        self.assertEqual(server.phone_url("100.1.2.3", 8787, None, None), "http://100.1.2.3:8787")
        self.assertEqual(server.phone_url("127.0.0.1", 8787, None, "https://m.ts.net"), "https://m.ts.net")
        self.assertEqual(server.phone_url("0.0.0.0", 8787, "http://mac.local:8787/", None), "http://mac.local:8787")
        self.assertEqual(server.origin_of("https://m.ts.net/x?y"), "https://m.ts.net")


@unittest.skipUnless(shutil.which("openssl"), "needs the openssl command to make a test certificate")
class TlsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.cert = os.path.join(self.tmp, "cert.pem")
        self.key = os.path.join(self.tmp, "key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=mac.tail1.ts.net", "-addext", "subjectAltName=DNS:mac.tail1.ts.net",
                        "-keyout", self.key, "-out", self.cert], check=True, capture_output=True)
        self.fake = FakeCmux(os.path.join(self.tmp, "cmux.sock"), "pw")
        client = server.CmuxClient(self.fake.path, "pw")
        self.hub = server.EventHub(client)
        self.hub.start()
        ctx = server.load_tls_context(self.cert, self.key)
        self.httpd = server.make_server("127.0.0.1", 0, client, self.hub, "tok", ssl_context=ctx)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.fake.close()

    def https(self) -> http.client.HTTPSConnection:
        ctx = ssl.create_default_context(cafile=self.cert)
        ctx.check_hostname = False  # the test connects by IP
        return http.client.HTTPSConnection("127.0.0.1", self.port, timeout=10, context=ctx)

    def test_https_login_sets_a_secure_cookie(self) -> None:
        conn = self.https()
        conn.request("GET", "/login?token=tok")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn("; Secure", resp.getheader("Set-Cookie"))

    def test_plain_http_client_does_not_block_https_clients(self) -> None:
        stuck = socket.create_connection(("127.0.0.1", self.port))  # connects, never handshakes
        try:
            conn = self.https()
            conn.request("GET", "/api/tree", headers={"Authorization": "Bearer tok"})
            self.assertEqual(conn.getresponse().status, 200)
        finally:
            stuck.close()

    def test_key_next_to_the_cert_is_found(self) -> None:
        crt = os.path.join(self.tmp, "mac.crt")
        shutil.copy(self.cert, crt)
        self.assertIsNone(server.default_tls_key(crt))
        shutil.copy(self.key, os.path.join(self.tmp, "mac.key"))
        key = server.default_tls_key(crt)
        self.assertEqual(key, os.path.join(self.tmp, "mac.key"))
        server.load_tls_context(crt, key)

    def test_missing_key_says_so(self) -> None:
        with self.assertRaisesRegex(ssl.SSLError, "--tls-key"):
            server.load_tls_context(self.cert, None)

    def test_cert_hostname_and_phone_url(self) -> None:
        name = server.cert_hostname(self.cert)
        self.assertEqual(name, "mac.tail1.ts.net")
        self.assertEqual(server.phone_url("100.1.2.3", 8787, None, None, name), "https://mac.tail1.ts.net:8787")
        self.assertEqual(server.phone_url("0.0.0.0", 443, None, None, name), "https://mac.tail1.ts.net")
        self.assertEqual(server.phone_url("100.1.2.3", 8787, None, None, None, "https"), "https://100.1.2.3:8787")


if __name__ == "__main__":
    unittest.main(verbosity=2)
