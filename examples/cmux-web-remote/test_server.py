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
import socket
import sys
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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
        self.assertEqual(resp.status, 303)
        cookie = resp.getheader("Set-Cookie")
        self.assertIn(f"{server.COOKIE_NAME}={self.token}", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(self.request("GET", "/login?token=wrong", auth=False).status, 401)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
