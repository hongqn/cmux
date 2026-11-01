#!/usr/bin/env python3
"""cmux web remote: a small HTTP server that drives cmux from a browser.

It talks to the running cmux app over its v2 JSON-lines control socket and
serves a single-page UI. Python standard library only; runs on the macOS
system python3 (3.9+).

Start it from a terminal inside cmux (so the default `cmuxOnly` socket mode
trusts it), then open the printed URL:

    python3 examples/cmux-web-remote/server.py

See README.md for remote access, authentication and security notes.
"""

from __future__ import annotations

import argparse
import hmac
import http.server
import json
import os
import queue
import secrets
import socket
import sys
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "static", "index.html")

COOKIE_NAME = "cmux_web_token"
EVENT_CATEGORIES = ["notification", "workspace", "surface"]
# Surface events that change the tree; input/key echoes are skipped.
SKIPPED_EVENTS = {"surface.input_sent", "surface.key_sent", "surface.action"}
WAIT_TIMEOUT_MS = 25000
MAX_BODY_BYTES = 256 * 1024
MAX_SCREEN_LINES = 2000


# ---------------------------------------------------------------------------
# cmux socket client
# ---------------------------------------------------------------------------


class CmuxError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def default_socket_path() -> str:
    """Mirror the cmux CLI: CMUX_SOCKET_PATH, then the stable socket."""
    for var in ("CMUX_SOCKET_PATH", "CMUX_SOCKET"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    candidates = [
        os.path.expanduser("~/.local/state/cmux/cmux.sock"),
        os.path.expanduser("~/Library/Application Support/cmux/cmux.sock"),
        "/tmp/cmux.sock",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[0]


class LineSocket:
    """One connection to the cmux socket, reading newline-delimited frames."""

    def __init__(self, path: str, password: Optional[str], timeout: float):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.buf = b""
        try:
            self.sock.connect(path)
        except OSError as exc:
            self.sock.close()
            raise CmuxError("socket_unavailable", f"cannot connect to {path}: {exc}")
        if password:
            self.write_line("auth " + password)
            reply = self.read_line()
            if reply.startswith("ERROR") and "Unknown command 'auth'" not in reply:
                self.close()
                raise CmuxError("auth_failed", reply.strip())

    def write_line(self, line: str) -> None:
        self.sock.sendall(line.encode("utf-8") + b"\n")

    def read_line(self) -> str:
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CmuxError("socket_closed", "cmux closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class CmuxClient:
    def __init__(self, socket_path: str, password: Optional[str]):
        self.socket_path = socket_path
        self.password = password
        self._next_id = 0
        self._id_lock = threading.Lock()

    def _new_id(self) -> str:
        with self._id_lock:
            self._next_id += 1
            return f"web-{self._next_id}"

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 15.0) -> Any:
        """One request per connection: simple, and safe across threads."""
        conn = LineSocket(self.socket_path, self.password, timeout)
        try:
            req_id = self._new_id()
            conn.write_line(json.dumps({"id": req_id, "method": method, "params": params or {}}))
            try:
                resp = json.loads(conn.read_line())
            except socket.timeout:
                raise CmuxError("timeout", f"{method} timed out")
        finally:
            conn.close()
        if not isinstance(resp, dict):
            raise CmuxError("bad_response", "non-object response")
        if resp.get("ok") is True:
            return resp.get("result")
        err = resp.get("error") or {}
        raise CmuxError(str(err.get("code") or "error"), str(err.get("message") or "unknown error"))

    def open_event_stream(self, after_seq: Optional[int]) -> LineSocket:
        conn = LineSocket(self.socket_path, self.password, timeout=60.0)
        params: Dict[str, Any] = {"categories": EVENT_CATEGORIES, "include_heartbeats": True}
        if after_seq is not None:
            params["after_seq"] = after_seq
        conn.write_line(json.dumps({"id": self._new_id(), "method": "events.stream", "params": params}))
        return conn


# ---------------------------------------------------------------------------
# Event fan-out (cmux events.stream -> browser Server-Sent Events)
# ---------------------------------------------------------------------------


class EventHub:
    """Keeps one events.stream subscription and fans frames out to browsers."""

    def __init__(self, client: CmuxClient):
        self.client = client
        self.subscribers: List["queue.Queue[Dict[str, Any]]"] = []
        self.lock = threading.Lock()
        self.last_seq: Optional[int] = None
        self.boot_id: Optional[str] = None
        self.connected = False

    def subscribe(self) -> "queue.Queue[Dict[str, Any]]":
        q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=256)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[Dict[str, Any]]") -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def publish(self, message: Dict[str, Any]) -> None:
        with self.lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(message)
            except queue.Full:
                # A stalled browser tab loses events; it refreshes on reconnect.
                pass

    def start(self) -> None:
        threading.Thread(target=self._run, name="cmux-events", daemon=True).start()

    def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                self._consume()
                backoff = 1.0
            except (CmuxError, OSError, ValueError) as exc:
                if self.connected:
                    self.publish({"type": "status", "connected": False, "error": str(exc)})
                self.connected = False
                time.sleep(backoff)
                backoff = min(backoff * 2, 15.0)

    def _consume(self) -> None:
        conn = self.client.open_event_stream(self.last_seq)
        try:
            while True:
                try:
                    line = conn.read_line()
                except socket.timeout:
                    raise CmuxError("timeout", "no heartbeat from cmux")
                if not line.strip():
                    continue
                frame = json.loads(line)
                if frame.get("ok") is False:
                    err = frame.get("error") or {}
                    raise CmuxError(str(err.get("code")), str(err.get("message")))
                kind = frame.get("type")
                if kind == "ack":
                    gap = bool((frame.get("resume") or {}).get("gap"))
                    if self.boot_id and frame.get("boot_id") != self.boot_id:
                        gap = True
                    self.boot_id = frame.get("boot_id")
                    self.connected = True
                    # A gap means events were missed: browsers reload snapshots.
                    self.publish({"type": "status", "connected": True, "resync": gap})
                elif kind == "event":
                    self._handle_event(frame)
                elif kind == "error":
                    err = frame.get("error") or {}
                    raise CmuxError(str(err.get("code") or "stream_error"), str(err.get("message") or ""))
        finally:
            conn.close()

    def _handle_event(self, frame: Dict[str, Any]) -> None:
        seq = frame.get("seq")
        if isinstance(seq, int):
            self.last_seq = seq
        if frame.get("name") in SKIPPED_EVENTS:
            return
        message: Dict[str, Any] = {
            "type": "event",
            "name": frame.get("name"),
            "category": frame.get("category"),
            "workspace_id": frame.get("workspace_id"),
            "surface_id": frame.get("surface_id"),
            "occurred_at": frame.get("occurred_at"),
        }
        if frame.get("name") == "notification.created":
            # The event stream redacts title/body; fetch the full record.
            notif_id = (frame.get("payload") or {}).get("notification_id")
            message["notification"] = self._lookup_notification(notif_id)
        self.publish(message)

    def _lookup_notification(self, notif_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not notif_id:
            return None
        try:
            result = self.client.call("notification.list") or {}
        except CmuxError:
            return None
        for item in result.get("notifications") or []:
            if str(item.get("id", "")).lower() == str(notif_id).lower():
                return item
        return None


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


def build_tree(client: CmuxClient) -> Dict[str, Any]:
    windows = (client.call("window.list") or {}).get("windows") or []
    out = []
    for win in windows:
        win_id = win.get("id")
        ws_rows = (client.call("workspace.list", {"window_id": win_id}) or {}).get("workspaces") or []
        workspaces = []
        for ws in ws_rows:
            try:
                surfaces = (client.call("surface.list", {"workspace_id": ws.get("id")}) or {}).get("surfaces") or []
            except CmuxError:
                surfaces = []
            workspaces.append({
                "id": ws.get("id"),
                "ref": ws.get("ref"),
                "title": ws.get("title"),
                "selected": ws.get("selected"),
                "pinned": ws.get("pinned"),
                "current_directory": ws.get("current_directory"),
                "latest_message": ws.get("latest_conversation_message") or ws.get("latest_submitted_message"),
                "surfaces": [
                    {
                        "id": s.get("id"),
                        "ref": s.get("ref"),
                        "type": s.get("type"),
                        "title": s.get("title"),
                        "focused": s.get("focused"),
                        "pane_id": s.get("pane_id"),
                    }
                    for s in surfaces
                ],
            })
        out.append({"id": win_id, "ref": win.get("ref"), "workspaces": workspaces})
    return {"windows": out}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "cmux-web-remote/0.1"
    # Set by make_server().
    client: CmuxClient
    hub: EventHub
    token: str
    allowed_origins: List[str]

    # -- helpers ----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        # Never write the login token to the log.
        line = (fmt % args).replace(self.token, "<token>")
        sys.stderr.write("%s %s\n" % (self.address_string(), line))

    def _send_json(self, status: int, payload: Any, extra_headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _cookie_token(self) -> Optional[str]:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME:
                return value
        return None

    def _authorized(self) -> bool:
        presented = self._cookie_token()
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            presented = auth[len("Bearer "):].strip()
        return bool(presented) and hmac.compare_digest(presented, self.token)

    def _same_origin(self) -> bool:
        """CSRF guard for state-changing requests (on top of SameSite=Strict)."""
        origin = self.headers.get("Origin")
        if not origin:
            # Non-browser clients (curl with a bearer token) send no Origin.
            return True
        host = self.headers.get("Host") or ""
        if origin in self.allowed_origins:
            return True
        return urlparse(origin).netloc == host

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise CmuxError("too_large", "request body too large")
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise CmuxError("bad_request", "expected application/json")
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise CmuxError("bad_request", "expected a JSON object")
        return data

    # -- routing ----------------------------------------------------------

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/login":
            return self._login(query)
        if url.path in ("/", "/index.html"):
            if not self._authorized():
                return self._send_login_page()
            return self._send_file(INDEX_HTML, "text/html; charset=utf-8")
        if not self._authorized():
            return self._send_json(401, {"error": "unauthorized"})
        try:
            if url.path == "/api/tree":
                return self._send_json(200, build_tree(self.client))
            if url.path == "/api/screen":
                return self._screen(query)
            if url.path == "/api/notifications":
                result = self.client.call("notification.list") or {}
                return self._send_json(200, {"notifications": result.get("notifications") or []})
            if url.path == "/api/events":
                return self._events()
        except CmuxError as exc:
            return self._send_json(502, {"error": exc.code, "message": exc.message})
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if not self._authorized():
            return self._send_json(401, {"error": "unauthorized"})
        if not self._same_origin():
            return self._send_json(403, {"error": "bad_origin"})
        try:
            body = self._read_json()
            result = self._dispatch_post(url.path, body)
        except CmuxError as exc:
            status = 400 if exc.code in ("bad_request", "too_large", "invalid_params") else 502
            return self._send_json(status, {"error": exc.code, "message": exc.message})
        except ValueError as exc:
            return self._send_json(400, {"error": "bad_request", "message": str(exc)})
        if result is None:
            return self._send_json(404, {"error": "not_found"})
        self._send_json(200, result)

    def _dispatch_post(self, path: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        def need(key: str) -> str:
            value = body.get(key)
            if not isinstance(value, str) or not value:
                raise CmuxError("bad_request", f"missing {key}")
            return value

        if path == "/api/send":
            surface_id = need("surface_id")
            text = body.get("text") or ""
            if not isinstance(text, str):
                raise CmuxError("bad_request", "text must be a string")
            if text:
                self.client.call("surface.send_text", {"surface_id": surface_id, "text": text})
            if body.get("enter"):
                self.client.call("surface.send_key", {"surface_id": surface_id, "key": "enter"})
            return {"ok": True}
        if path == "/api/key":
            self.client.call("surface.send_key", {"surface_id": need("surface_id"), "key": need("key")})
            return {"ok": True}
        if path == "/api/workspace/select":
            # This switches the workspace on the Mac screen too.
            self.client.call("workspace.select", {"workspace_id": need("workspace_id")})
            return {"ok": True}
        if path == "/api/workspace/create":
            params: Dict[str, Any] = {}
            if isinstance(body.get("cwd"), str) and body["cwd"]:
                params["cwd"] = body["cwd"]
            if isinstance(body.get("window_id"), str) and body["window_id"]:
                params["window_id"] = body["window_id"]
            return self.client.call("workspace.create", params) or {}
        if path == "/api/notifications/read":
            if body.get("all"):
                return self.client.call("notification.mark_read", {"all": True}) or {}
            return self.client.call("notification.mark_read", {"id": need("id")}) or {}
        if path == "/api/notifications/dismiss":
            return self.client.call("notification.dismiss", {"id": need("id")}) or {}
        return None

    # -- endpoints --------------------------------------------------------

    def _login(self, query: Dict[str, List[str]]) -> None:
        presented = (query.get("token") or [""])[0]
        if not presented or not hmac.compare_digest(presented, self.token):
            return self._send_login_page(error=True)
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        self.send_response(303)
        self.send_header(
            "Set-Cookie",
            f"{COOKIE_NAME}={self.token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000{secure}",
        )
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_login_page(self, error: bool = False) -> None:
        msg = "<p style='color:#e66'>Wrong token.</p>" if error else ""
        html = (
            "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>cmux web remote</title><body style='font-family:system-ui;background:#111;color:#ddd;padding:2em'>"
            "<h2>cmux web remote</h2>" + msg +
            "<form action=/login method=get><input name=token type=password placeholder='access token' "
            "autofocus style='font-size:1.1em;padding:.4em'> <button>Sign in</button></form>"
        ).encode("utf-8")
        self.send_response(401 if error else 200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def _send_file(self, path: str, content_type: str) -> None:
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(data)

    def _screen(self, query: Dict[str, List[str]]) -> None:
        surface_id = (query.get("surface_id") or [""])[0]
        if not surface_id:
            return self._send_json(400, {"error": "bad_request", "message": "missing surface_id"})
        try:
            lines = max(1, min(int((query.get("lines") or ["200"])[0]), MAX_SCREEN_LINES))
        except ValueError:
            lines = 200
        params: Dict[str, Any] = {"surface_id": surface_id, "scrollback": True, "lines": lines}
        after = (query.get("after_revision") or [""])[0]
        if after:
            # Long-poll: cmux answers when the terminal text changes or at timeout.
            params["after_revision"] = after
            params["timeout_ms"] = WAIT_TIMEOUT_MS
        result = self.client.call("surface.wait_text_change", params, timeout=WAIT_TIMEOUT_MS / 1000 + 10) or {}
        self._send_json(200, {
            "text": result.get("text") or "",
            "revision": result.get("revision"),
            "changed": result.get("changed", True),
        })

    def _events(self) -> None:
        q = self.hub.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self._sse({"type": "status", "connected": self.hub.connected, "resync": False})
            while True:
                try:
                    message = q.get(timeout=20)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self._sse(message)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.hub.unsubscribe(q)

    def _sse(self, message: Dict[str, Any]) -> None:
        self.wfile.write(b"data: " + json.dumps(message).encode("utf-8") + b"\n\n")
        self.wfile.flush()


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


def make_server(host: str, port: int, client: CmuxClient, hub: EventHub, token: str,
                allowed_origins: Optional[List[str]] = None) -> Server:
    handler = type("BoundHandler", (Handler,), {
        "client": client,
        "hub": hub,
        "token": token,
        "allowed_origins": allowed_origins or [],
    })
    return Server((host, port), handler)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Control cmux workspaces from a browser.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default 127.0.0.1; expose remotely through Tailscale Serve or a TLS proxy)")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--socket", default=None, help="cmux socket path (default: $CMUX_SOCKET_PATH)")
    parser.add_argument("--allow-origin", action="append", default=[],
                        help="extra Origin allowed for POSTs, e.g. https://mac.tailnet.ts.net")
    args = parser.parse_args(argv)

    token = os.environ.get("CMUX_WEB_TOKEN") or secrets.token_urlsafe(24)
    client = CmuxClient(args.socket or default_socket_path(), os.environ.get("CMUX_SOCKET_PASSWORD"))

    try:
        client.call("system.ping")
    except CmuxError as exc:
        print(f"warning: cmux socket check failed ({exc}); will keep retrying", file=sys.stderr)

    hub = EventHub(client)
    hub.start()
    server = make_server(args.host, args.port, client, hub, token, args.allow_origin)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("warning: listening beyond loopback without TLS; anyone who gets the token can type into your terminals",
              file=sys.stderr)
    print(f"cmux socket: {client.socket_path}")
    print(f"open: http://{args.host}:{args.port}/login?token={token}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
