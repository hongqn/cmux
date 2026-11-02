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
import base64
import gzip
import hashlib
import hmac
import http.server
import json
import os
import queue
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import agentchat
import qrterm

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "static", "index.html")

COOKIE_NAME = "cmux_web_token"
# "sidebar" carries status pills, logs and progress; "terminal" carries
# program_status.changed (the "claude-code · Working" pill).
EVENT_CATEGORIES = ["notification", "workspace", "surface", "sidebar", "terminal"]
# Surface events that change the tree; input/key echoes are skipped.
SKIPPED_EVENTS = {"surface.input_sent", "surface.key_sent", "surface.action"}
WAIT_TIMEOUT_MS = 25000
# Poll interval when cmux lacks surface.wait_text_change (0.65.x and older).
FALLBACK_POLL_SECONDS = 0.5
MAX_BODY_BYTES = 256 * 1024
MAX_SCREEN_LINES = 2000
# A v1 reply has no terminator; the CLI stops after this long without data.
V1_IDLE_SECONDS = 0.12
SIDEBAR_FETCH_WORKERS = 8
HOME = os.path.expanduser("~")
# Panes a browser may watch in one /api/screens long-poll.
MAX_SCREENS = 12


# ---------------------------------------------------------------------------
# cmux socket client
# ---------------------------------------------------------------------------


class CmuxError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def decode_frame(line: str) -> Dict[str, Any]:
    """Parse one socket reply. cmux refuses with a plain-text ERROR line."""
    if line.startswith("ERROR"):
        text = line.strip()
        if "Access denied" in text:
            raise CmuxError(
                "access_denied",
                text + ". Start this server in the foreground of a cmux terminal (not nohup/launchd), "
                "or set the socket to password mode and export CMUX_SOCKET_PASSWORD.",
            )
        raise CmuxError("socket_error", text)
    try:
        frame = json.loads(line)
    except ValueError:
        raise CmuxError("bad_response", f"unexpected reply: {line[:200]!r}")
    if not isinstance(frame, dict):
        raise CmuxError("bad_response", "non-object response")
    return frame


def screen_text(result: Dict[str, Any]) -> str:
    """surface.read_text answers with text, base64, or both."""
    if isinstance(result.get("text"), str):
        return result["text"]
    raw = result.get("base64")
    return base64.b64decode(raw).decode("utf-8", errors="replace") if raw else ""


def compact_grid(grid: Dict[str, Any]) -> Dict[str, Any]:
    """Shrink a cmux.render-grid.v1 frame (from terminal.replay) for the browser.

    Styles become CSS strings keyed by id; spans become [column, cells, text,
    style] rows, scrollback first, then the viewport.
    """
    term_fg = grid.get("terminal_foreground") or "#FFFFFF"
    term_bg = grid.get("terminal_background") or "#000000"
    styles: Dict[str, str] = {}
    for st in grid.get("styles") or []:
        fg = st.get("foreground") if st.get("foreground_source", "default") != "default" else None
        bg = st.get("background") if st.get("background_source", "default") != "default" else None
        if st.get("inverse"):
            fg, bg = (bg or term_bg), (fg or term_fg)
        css = []
        if fg:
            css.append("color:" + fg)
        if bg:
            css.append("background:" + bg)
        if st.get("bold"):
            css.append("font-weight:bold")
        if st.get("italic"):
            css.append("font-style:italic")
        if st.get("faint"):
            css.append("opacity:.6")
        deco = [name for key, name in (("underline", "underline"), ("strikethrough", "line-through"),
                                       ("overline", "overline")) if st.get(key)]
        if deco:
            css.append("text-decoration:" + " ".join(deco))
        if st.get("invisible"):
            css.append("visibility:hidden")
        styles[str(st.get("id"))] = ";".join(css)

    scrollback = int(grid.get("scrollback_rows") or 0)
    rows = int(grid.get("rows") or 0)
    lines: List[List[List[Any]]] = [[] for _ in range(scrollback + rows)]
    for offset, key in ((0, "scrollback_spans"), (scrollback, "row_spans")):
        for span in grid.get(key) or []:
            index = offset + int(span.get("row", -1))
            if 0 <= index < len(lines):
                lines[index].append([span.get("column", 0), span.get("cell_width", 0),
                                     span.get("text", ""), span.get("style_id", 0)])
    for line in lines:
        line.sort(key=lambda item: item[0])

    cursor = grid.get("cursor") or {}
    return {
        "columns": grid.get("columns"),
        "rows": rows,
        "foreground": term_fg,
        "background": term_bg,
        "styles": styles,
        "lines": lines,
        "cursor": {
            "line": scrollback + int(cursor.get("row", 0)),
            "column": int(cursor.get("column", 0)),
            "visible": bool(cursor.get("visible", False)),
        },
    }


_STATUS_ATTR = re.compile(r" (icon|color|url|priority|format|work)=(\S+)$")
WORKING_STATES = {"running", "subagents"}


def parse_sidebar_state(lines: Iterable[str]) -> Dict[str, Any]:
    """Parse the v1 `sidebar_state` listing into what a sidebar row shows.

    Status pills keep cmux's order, highest priority first. Only the newest
    log entry is kept, as the sidebar row shows only that one.
    """
    statuses: List[Dict[str, Any]] = []
    log: Optional[Dict[str, str]] = None
    progress: Optional[Dict[str, Any]] = None
    git_dirty = False
    section = ""
    for raw in lines:
        if raw.startswith("  "):
            item = raw[2:]
            if section == "status":
                attrs: Dict[str, str] = {}
                while True:
                    m = _STATUS_ATTR.search(item)
                    if not m:
                        break
                    attrs[m.group(1)] = m.group(2)
                    item = item[:m.start()]
                key, _, value = item.partition("=")
                statuses.append({
                    "key": key, "value": value,
                    "icon": attrs.get("icon"), "color": attrs.get("color"), "url": attrs.get("url"),
                    "priority": int(attrs.get("priority", "0") or 0),
                    "format": attrs.get("format", "plain"), "work": attrs.get("work"),
                })
            elif section == "log":
                m = re.match(r"\[(\w+)\] (.*)$", item)
                if m:
                    log = {"level": m.group(1), "message": m.group(2)}
            continue
        key, _, value = raw.partition("=")
        section = {"status_count": "status", "log_count": "log"}.get(key, "")
        if key == "git_branch":
            git_dirty = value.endswith(" dirty")
        elif key == "progress" and value != "none":
            number, _, label = value.partition(" ")
            try:
                progress = {"value": float(number), "label": label or None}
            except ValueError:
                progress = None
    statuses.sort(key=lambda e: -e["priority"])  # stable: ties keep cmux's order
    return {
        "statuses": statuses,
        "log": log,
        "progress": progress,
        "working": any(e["work"] in WORKING_STATES for e in statuses),
        "git_dirty": git_dirty,
    }


def abbreviate_home(path: Optional[str]) -> Optional[str]:
    if path and (path == HOME or path.startswith(HOME + "/")):
        return "~" + path[len(HOME):]
    return path


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
        # None until the first screen read finds out whether cmux has it.
        self.has_wait_text_change: Optional[bool] = None

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
                resp = decode_frame(conn.read_line())
            except socket.timeout:
                raise CmuxError("timeout", f"{method} timed out")
        finally:
            conn.close()
        if resp.get("ok") is True:
            return resp.get("result")
        err = resp.get("error") or {}
        raise CmuxError(str(err.get("code") or "error"), str(err.get("message") or "unknown error"))

    def read_screen(self, surface_id: str, lines: int, after_revision: str) -> Dict[str, Any]:
        """Terminal text plus a revision; with after_revision, wait for a change.

        Uses surface.wait_text_change when cmux has it, else polls
        surface.read_text and derives the revision from the text.
        """
        params: Dict[str, Any] = {"surface_id": surface_id, "scrollback": True, "lines": lines}
        if self.has_wait_text_change is not False:
            native = dict(params)
            if after_revision:
                native["after_revision"] = after_revision
                native["timeout_ms"] = WAIT_TIMEOUT_MS
            try:
                result = self.call("surface.wait_text_change", native, timeout=WAIT_TIMEOUT_MS / 1000 + 10) or {}
                self.has_wait_text_change = True
                return {
                    "text": screen_text(result),
                    "revision": result.get("revision"),
                    "changed": result.get("changed", True),
                }
            except CmuxError as exc:
                if exc.code != "method_not_found":
                    raise
                self.has_wait_text_change = False
        deadline = time.monotonic() + WAIT_TIMEOUT_MS / 1000
        while True:
            text = screen_text(self.call("surface.read_text", params) or {})
            revision = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
            if revision != after_revision:
                return {"text": text, "revision": revision, "changed": True}
            if time.monotonic() >= deadline:
                return {"text": text, "revision": revision, "changed": False}
            time.sleep(FALLBACK_POLL_SECONDS)

    def read_screens(self, revisions: Dict[str, str], lines: int) -> Dict[str, Dict[str, Any]]:
        """Wait until any of several terminals changes; attach a colored grid.

        One surface uses read_screen (native wait when cmux has it). Several
        are polled together, so one request covers every visible pane.
        """
        if len(revisions) == 1:
            ((surface_id, after),) = revisions.items()
            results = {surface_id: self.read_screen(surface_id, lines, after)}
        else:
            results = self._poll_screens(revisions, lines)
        for surface_id, result in results.items():
            if result.get("changed") and "error" not in result:
                grid = self.read_grid(surface_id)
                if grid is not None:
                    result["grid"] = grid
        return results

    def _poll_screens(self, revisions: Dict[str, str], lines: int) -> Dict[str, Dict[str, Any]]:
        deadline = time.monotonic() + WAIT_TIMEOUT_MS / 1000
        while True:
            results: Dict[str, Dict[str, Any]] = {}
            for surface_id, after in revisions.items():
                params = {"surface_id": surface_id, "scrollback": True, "lines": lines}
                try:
                    text = screen_text(self.call("surface.read_text", params) or {})
                except CmuxError as exc:
                    results[surface_id] = {"error": exc.code, "message": exc.message, "changed": True}
                    continue
                revision = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
                results[surface_id] = {"text": text, "revision": revision, "changed": revision != after}
            if any(r["changed"] for r in results.values()) or time.monotonic() >= deadline:
                return results
            time.sleep(FALLBACK_POLL_SECONDS)

    def read_grid(self, surface_id: str) -> Optional[Dict[str, Any]]:
        """The colored screen via terminal.replay, or None when unavailable."""
        try:
            result = self.call("terminal.replay", {"surface_id": surface_id}) or {}
        except CmuxError:
            return None
        grid = result.get("render_grid")
        return compact_grid(grid) if isinstance(grid, dict) else None

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


class SidebarCache:
    """Per-workspace status pills, newest log line and progress.

    cmux has no v2 read for these, so they come from the v1 `sidebar_state`
    command: all workspaces once, then one workspace per sidebar event.
    """

    def __init__(self, client: "CmuxClient"):
        self.client = client
        self.entries: Dict[str, Dict[str, Any]] = {}
        self.pending: set = set()
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(SIDEBAR_FETCH_WORKERS, thread_name_prefix="sidebar")
        self.on_change = None  # callable(workspace_id, entry)

    def client_v1_lines(self, command: str) -> List[str]:
        """Send one v1 text command; read its reply to the end."""
        conn = LineSocket(self.client.socket_path, self.client.password, timeout=5.0)
        try:
            conn.write_line(command)
            lines = [conn.read_line()]
            if lines[0].startswith("ERROR"):
                raise CmuxError("v1_error", lines[0])
            remaining: Optional[int] = None  # log lines still expected, once known
            conn.sock.settimeout(V1_IDLE_SECONDS)
            while remaining is None or remaining > 0:
                try:
                    line = conn.read_line()
                except (socket.timeout, CmuxError):
                    break
                lines.append(line)
                if remaining is not None:
                    remaining -= 1
                elif line.startswith("log_count="):
                    # sidebar_state lists at most the 5 newest log entries last.
                    remaining = min(int(line.split("=", 1)[1] or 0), 5)
            return lines
        finally:
            conn.close()

    def fetch(self, workspace_id: str) -> Optional[Dict[str, Any]]:
        try:
            entry = parse_sidebar_state(self.client_v1_lines(f"sidebar_state --tab={workspace_id}"))
        except (CmuxError, OSError, ValueError):
            entry = None
        with self.lock:
            self.pending.discard(workspace_id)
            if entry is not None:
                self.entries[workspace_id] = entry
        if entry is not None and self.on_change:
            self.on_change(workspace_id, entry)
        return entry

    def refresh(self, workspace_ids: Iterable[str]) -> None:
        """Fetch now and wait (startup and tests)."""
        list(self.pool.map(self.fetch, list(workspace_ids)))

    def ensure(self, workspace_ids: Iterable[str]) -> None:
        """Fetch in the background the workspaces not cached or queued yet."""
        with self.lock:
            missing = [w for w in workspace_ids if w not in self.entries and w not in self.pending]
            self.pending.update(missing)
        for workspace_id in missing:
            self.pool.submit(self.fetch, workspace_id)

    def update_later(self, workspace_id: str) -> None:
        self.pool.submit(self.fetch, workspace_id)

    def get(self, workspace_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            return self.entries.get(workspace_id)


class EventHub:
    """Keeps one events.stream subscription and fans frames out to browsers."""

    def __init__(self, client: CmuxClient):
        self.client = client
        self.sidebar = SidebarCache(client)
        self.sidebar.on_change = lambda ws_id, entry: self.publish(
            {"type": "sidebar", "workspace_id": ws_id, "sidebar": entry})
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
                frame = decode_frame(line)
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
        name = str(frame.get("name") or "")
        if name in SKIPPED_EVENTS:
            return
        if name.startswith("sidebar.") or name == "program_status.changed":
            # The row's pills changed: re-read that workspace, then tell
            # browsers (SidebarCache.on_change publishes a "sidebar" message).
            if frame.get("workspace_id"):
                self.sidebar.update_later(str(frame["workspace_id"]))
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


CHAT_POLL_SECONDS = 25
CHAT_CHECK_SECONDS = 0.4
CHAT_AGENTS = {"claude"}  # transcripts agentchat can read


class AgentSessions:
    """Coding-agent sessions cmux tracks, from `chat.sessions.dump`.

    Each record ties a session to its terminal (surface_id), state and
    transcript path. Transcript paths stay on the server; the browser only
    names sessions by id, and only ids cmux reports are accepted.
    """

    def __init__(self, client: CmuxClient, max_age: float = 1.0) -> None:
        self.client = client
        self.max_age = max_age
        self.lock = threading.Lock()
        self.fetched = 0.0
        self.sessions: Dict[str, Dict[str, Any]] = {}

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self.lock:
            if time.monotonic() - self.fetched < self.max_age:
                return self.sessions
        try:
            result = self.client.call("chat.sessions.dump") or {}
        except CmuxError:
            result = {}  # older cmux, or the chat service is off
        sessions = {s["session_id"]: s for s in result.get("sessions") or [] if s.get("session_id")}
        with self.lock:
            self.sessions, self.fetched = sessions, time.monotonic()
        return sessions

    def get(self, session_id: str) -> Dict[str, Any]:
        record = self.all().get(session_id)
        if not record:
            raise CmuxError("not_found", "no such agent session")
        return record

    def transcript(self, session_id: str) -> Tuple[Dict[str, Any], str]:
        record = self.get(session_id)
        path = record.get("transcript_path") or ""
        if record.get("agent") not in CHAT_AGENTS or not os.path.isfile(path):
            raise CmuxError("unsupported", "no readable transcript for this session; use the terminal")
        return record, path

    @staticmethod
    def public(record: Dict[str, Any]) -> Dict[str, Any]:
        path = record.get("transcript_path") or ""
        return {
            "session_id": record.get("session_id"),
            "agent": record.get("agent"),
            "state": agentchat.session_state(record.get("state") or ""),
            "chat": record.get("agent") in CHAT_AGENTS and bool(path) and os.path.isfile(path),
        }

    def by_surface(self) -> Dict[str, Dict[str, Any]]:
        """The current session of each terminal: live ones beat ended ones, then the latest."""
        best: Dict[str, Dict[str, Any]] = {}
        for record in self.all().values():
            surface = (record.get("surface_id") or "").upper()
            if not surface:
                continue
            rank = (agentchat.session_state(record.get("state") or "") != "ended", record.get("last_activity") or 0)
            if surface not in best or rank > best[surface][0]:
                best[surface] = (rank, record)  # type: ignore[assignment]
        return {k: self.public(v[1]) for k, v in best.items()}  # type: ignore[index]


def build_tree(client: CmuxClient, sidebar: Optional[SidebarCache] = None,
               agents: Optional[AgentSessions] = None) -> Dict[str, Any]:
    """Windows, workspaces, split layouts, panes, tabs and sidebar rows.

    system.tree carries the layout and panes; extension.sidebar.snapshot
    carries what a cmux sidebar row shows (unread count, latest notification,
    description, branch, ports); the SidebarCache adds status pills and logs.
    """
    snapshot = client.call("system.tree") or {}
    agent_by_surface = agents.by_surface() if agents else {}
    out = []
    all_ids: List[str] = []
    for win in snapshot.get("windows") or []:
        rows: Dict[str, Dict[str, Any]] = {}
        try:
            listed = (client.call("extension.sidebar.snapshot", {"window_id": win.get("id")}) or {}).get("workspaces") or []
            rows = {str(row.get("id")): row for row in listed}
        except CmuxError:
            pass
        workspaces = []
        for ws in win.get("workspaces") or []:
            ws_id = str(ws.get("id"))
            all_ids.append(ws_id)
            row = rows.get(ws_id, {})
            branches = row.get("git_branches") or []
            workspaces.append({
                "id": ws.get("id"),
                "ref": ws.get("ref"),
                "title": ws.get("title"),
                "selected": ws.get("selected"),
                "pinned": row.get("pinned", ws.get("pinned")),
                "description": row.get("description"),
                "custom_color": row.get("custom_color"),
                "unread_count": row.get("unread_count") or 0,
                "latest_notification_text": row.get("latest_notification_text"),
                "latest_message": row.get("latest_conversation_message") or row.get("latest_submitted_message"),
                "directory": abbreviate_home(row.get("current_directory")),
                "branch": row.get("branch_summary"),
                "git_dirty": any(b.get("dirty") for b in branches),
                "pull_requests": row.get("pull_request_urls") or [],
                "ports": row.get("listening_ports") or [],
                "sidebar": sidebar.get(ws_id) if sidebar else None,
                "layout": ws.get("layout"),
                "panes": [
                    {
                        "id": pane.get("id"),
                        "ref": pane.get("ref"),
                        "focused": pane.get("focused"),
                        "selected_surface_id": pane.get("selected_surface_id"),
                        "surfaces": [
                            {
                                "id": surf.get("id"),
                                "ref": surf.get("ref"),
                                "type": surf.get("type"),
                                "title": surf.get("title"),
                                "url": surf.get("url"),
                                "agent": agent_by_surface.get(str(surf.get("id") or "").upper()),
                            }
                            for surf in pane.get("surfaces") or []
                        ],
                    }
                    for pane in ws.get("panes") or []
                ],
            })
        out.append({"id": win.get("id"), "ref": win.get("ref"), "workspaces": workspaces})
    if sidebar:
        sidebar.ensure(all_ids)
    return {"windows": out}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "cmux-web-remote/0.1"
    # Set by make_server().
    client: CmuxClient
    hub: EventHub
    agents: AgentSessions
    token: str
    allowed_origins: List[str]
    # Off by default: the server usually runs in a cmux terminal the browser
    # is watching, and a log line per poll would change that terminal and
    # answer the next poll at once, forever. Errors are always logged.
    access_log = False

    # -- helpers ----------------------------------------------------------

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        status = getattr(code, "value", code)
        if self.access_log or not str(status).isdigit() or int(status) >= 400:
            super().log_request(code, size)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Never write the login token to the log.
        line = (fmt % args).replace(self.token, "<token>")
        sys.stderr.write("%s %s\n" % (self.address_string(), line))

    def _send_json(self, status: int, payload: Any, extra_headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        compressed = len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "")
        if compressed:
            body = gzip.compress(body, compresslevel=5)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if compressed:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
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
        # Fetched by the browser without cookies when adding to the Home Screen; nothing secret.
        if url.path == "/manifest.webmanifest":
            return self._send_bytes(json.dumps(MANIFEST).encode(), "application/manifest+json", "max-age=86400")
        if url.path in ICON_SIZES:
            return self._send_bytes(icon_png(ICON_SIZES[url.path]), "image/png", "max-age=86400")
        if url.path in ("/", "/index.html"):
            if not self._authorized():
                return self._send_login_page()
            return self._send_file(INDEX_HTML, "text/html; charset=utf-8")
        if not self._authorized():
            return self._send_json(401, {"error": "unauthorized"})
        try:
            if url.path == "/api/tree":
                return self._send_json(200, build_tree(self.client, self.hub.sidebar, self.agents))
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
            status = {"not_found": 404, "unsupported": 409}.get(exc.code, 502)
            if exc.code in ("bad_request", "too_large", "invalid_params"):
                status = 400
            return self._send_json(status, {"error": exc.code, "message": exc.message})
        except ValueError as exc:
            return self._send_json(400, {"error": "bad_request", "message": str(exc)})
        if result is None:
            return self._send_json(404, {"error": "not_found"})
        self._send_json(200, result)

    def _chat(self, action: str, body: Dict[str, Any], need: Any) -> Optional[Dict[str, Any]]:
        session_id = need("session_id")

        def offset(key: str) -> Optional[int]:
            value = body.get(key)
            if value is None:
                return None
            if not isinstance(value, int) or value < 0:
                raise CmuxError("bad_request", f"{key} must be a byte offset")
            return value

        if action == "history":
            record, path = self.agents.transcript(session_id)
            page = agentchat.read_tail(path, offset("before"))
            return dict(page, state=agentchat.session_state(record.get("state") or ""))
        if action == "poll":
            # Long-poll: return once the transcript grows or the agent's state changes.
            after = offset("after") or 0
            known_state = body.get("state")
            deadline = time.monotonic() + CHAT_POLL_SECONDS
            while True:
                record, path = self.agents.transcript(session_id)
                state = agentchat.session_state(record.get("state") or "")
                update = agentchat.read_since(path, after)
                if update["items"] or update["end"] != after or state != known_state or time.monotonic() >= deadline:
                    return dict(update, state=state)
                time.sleep(CHAT_CHECK_SECONDS)
        if action == "send":
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                raise CmuxError("bad_request", "text required")
            try:
                return self.client.call("mobile.chat.send", {"session_id": session_id, "text": text}) or {"ok": True}
            except CmuxError as exc:
                if exc.code not in ("not_found", "method_not_found"):
                    raise
            # cmux lost the session's terminal binding; paste into the surface it last reported.
            record = self.agents.get(session_id)
            target = {"surface_id": record.get("surface_id"), "workspace_id": record.get("workspace_id")}
            for key in ("ctrl+a", "ctrl+k", "ctrl+u"):
                self.client.call("surface.send_key", {"surface_id": target["surface_id"], "key": key})
            return self.client.call("terminal.paste", dict(target, text=text)) or {"ok": True}
        if action == "interrupt":
            hard = bool(body.get("hard"))
            try:
                return self.client.call("mobile.chat.interrupt", {"session_id": session_id, "hard": hard}) or {"ok": True}
            except CmuxError as exc:
                if exc.code not in ("not_found", "method_not_found"):
                    raise
            record = self.agents.get(session_id)
            self.client.call("surface.send_key", {"surface_id": record.get("surface_id"),
                                                   "key": "ctrl+c" if hard else "escape"})
            return {"interrupted": True, "hard": hard}
        return None

    def _dispatch_post(self, path: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        def need(key: str) -> str:
            value = body.get(key)
            if not isinstance(value, str) or not value:
                raise CmuxError("bad_request", f"missing {key}")
            return value

        if path == "/api/screens":
            revisions = body.get("surfaces")
            if not isinstance(revisions, dict) or not revisions:
                raise CmuxError("bad_request", "surfaces must be an object of surface_id -> revision")
            if len(revisions) > MAX_SCREENS:
                raise CmuxError("bad_request", f"at most {MAX_SCREENS} surfaces per request")
            if not all(isinstance(k, str) and k and isinstance(v, str) for k, v in revisions.items()):
                raise CmuxError("bad_request", "surface ids and revisions must be strings")
            try:
                lines = max(1, min(int(body.get("lines") or 400), MAX_SCREEN_LINES))
            except (TypeError, ValueError):
                lines = 400
            return {"screens": self.client.read_screens(revisions, lines)}
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
        if path == "/api/peek":
            # The visible screen, for prompts the transcript does not show yet.
            result = self.client.call("surface.read_text", {"surface_id": need("surface_id")}) or {}
            return {"text": screen_text(result)}
        if path.startswith("/api/chat/"):
            return self._chat(path[len("/api/chat/"):], body, need)
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
            if isinstance(body.get("workspace_id"), str) and body["workspace_id"]:
                # A pane was looked at: mark its notifications read, as focusing it in cmux does.
                params = {"workspace_id": body["workspace_id"]}
                if isinstance(body.get("surface_id"), str) and body["surface_id"]:
                    params["surface_id"] = body["surface_id"]
                return self.client.call("notification.mark_read", params) or {}
            return self.client.call("notification.mark_read", {"id": need("id")}) or {}
        if path == "/api/notifications/clear_all":
            # cmux's "Clear All": dismiss every notification, one by one.
            items = (self.client.call("notification.list") or {}).get("notifications") or []
            for item in items:
                self.client.call("notification.dismiss", {"id": item.get("id")})
            return {"dismissed": len(items)}
        if path == "/api/notifications/dismiss":
            return self.client.call("notification.dismiss", {"id": need("id")}) or {}
        return None

    # -- endpoints --------------------------------------------------------

    def _login(self, query: Dict[str, List[str]]) -> None:
        presented = (query.get("token") or [""])[0]
        if not presented or not hmac.compare_digest(presented, self.token):
            return self._send_login_page(error=True)
        tls = getattr(self.server, "ssl_context", None) is not None
        secure = "; Secure" if tls or self.headers.get("X-Forwarded-Proto") == "https" else ""
        # Not an HTTP redirect: a link opened from another app (the iPhone
        # camera, Messages) is a cross-site navigation, a redirect stays part of
        # it, and Safari would not send the SameSite=Strict cookie to "/". A
        # navigation started by this page is same-site, so the cookie goes along.
        html = (
            '<!doctype html><meta http-equiv="refresh" content="0;url=/">'
            "<title>cmux web remote</title>"
            '<script>location.replace("/")</script>'
            "<body style='font-family:system-ui;background:#111;color:#ddd;padding:2em'>"
            "<a href=/ style='color:#0a84ff'>Continue</a>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header(
            "Set-Cookie",
            f"{COOKIE_NAME}={self.token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000{secure}",
        )
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(html)

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

    def _send_bytes(self, data: bytes, content_type: str, cache: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

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


TLS_HANDSHAKE_SECONDS = 10


MANIFEST = {
    "name": "cmux web remote",
    "short_name": "cmux",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#111111",
    "theme_color": "#111111",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
    ],
}
ICON_SIZES = {"/apple-touch-icon.png": 180, "/icon-192.png": 192, "/icon-512.png": 512, "/favicon.ico": 32}
_icon_cache: Dict[int, bytes] = {}


def icon_png(size: int) -> bytes:
    """A ">_" prompt on a dark square, drawn here so the example ships no binary files."""
    if size in _icon_cache:
        return _icon_cache[size]
    bg, fg = (0x1E, 0x1E, 0x1E), (0x0A, 0x84, 0xFF)
    half = 0.045 * size
    segments = [((0.27, 0.32), (0.47, 0.50)), ((0.47, 0.50), (0.27, 0.68))]
    segments = [((ax * size, ay * size), (bx * size, by * size)) for (ax, ay), (bx, by) in segments]
    bar = (0.53 * size, 0.62 * size, 0.75 * size, 0.62 * size + 2 * half)

    def on(x: float, y: float) -> bool:
        if bar[0] <= x <= bar[2] and bar[1] <= y <= bar[3]:
            return True
        for (ax, ay), (bx, by) in segments:
            dx, dy = bx - ax, by - ay
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
            if (x - ax - t * dx) ** 2 + (y - ay - t * dy) ** 2 <= half * half:
                return True
        return False

    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            rows.extend(fg if on(x + 0.5, y + 0.5) else bg)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")

    header = size.to_bytes(4, "big") * 2 + bytes([8, 2, 0, 0, 0])
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b"")
    _icon_cache[size] = png
    return png


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    ssl_context: Optional[ssl.SSLContext] = None

    def finish_request(self, request: Any, client_address: Any) -> None:
        if self.ssl_context is None:
            return super().finish_request(request, client_address)
        # Handshake in the connection's own thread, so a slow or plain-HTTP
        # client cannot stall accept() for everyone else.
        try:
            request.settimeout(TLS_HANDSHAKE_SECONDS)
            tls = self.ssl_context.wrap_socket(request, server_side=True)
            tls.settimeout(None)
        except (ssl.SSLError, OSError):
            return
        try:
            super().finish_request(tls, client_address)
        finally:
            try:
                tls.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            tls.close()


def default_tls_key(cert: str) -> Optional[str]:
    """The key `tailscale cert` writes next to the cert (name.crt -> name.key), unless the cert holds one."""
    try:
        with open(cert, encoding="utf-8", errors="replace") as fh:
            if "PRIVATE KEY-----" in fh.read():
                return None
    except OSError:
        return None
    stem, _ = os.path.splitext(cert)
    for candidate in (stem + ".key", cert + ".key"):
        if os.path.isfile(candidate):
            return candidate
    return None


def load_tls_context(cert: str, key: Optional[str]) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.load_cert_chain(cert, key)
    except ssl.SSLError as exc:
        if key is None:
            raise ssl.SSLError(f"{cert} holds no private key; pass it with --tls-key") from exc
        raise ssl.SSLError(f"{cert} and {key} are not a matching PEM certificate and key ({exc})") from exc
    return ctx


def cert_hostname(cert: str) -> Optional[str]:
    """First non-wildcard DNS name in the certificate, e.g. mac.tailnet.ts.net from `tailscale cert`."""
    try:
        decoded = ssl._ssl._test_decode_cert(cert)  # type: ignore[attr-defined]
    except (AttributeError, OSError, ssl.SSLError, ValueError):
        return None
    for kind, name in decoded.get("subjectAltName", ()):
        if kind == "DNS" and "*" not in name:
            return name
    return None


def make_server(host: str, port: int, client: CmuxClient, hub: EventHub, token: str,
                allowed_origins: Optional[List[str]] = None, access_log: bool = False,
                ssl_context: Optional[ssl.SSLContext] = None) -> Server:
    handler = type("BoundHandler", (Handler,), {
        "client": client,
        "hub": hub,
        "agents": AgentSessions(client),
        "token": token,
        "allowed_origins": allowed_origins or [],
        "access_log": access_log,
    })
    httpd = Server((host, port), handler)
    httpd.ssl_context = ssl_context
    return httpd


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
WILDCARD_HOSTS = ("0.0.0.0", "::", "")


def tailscale_serve_url(port: int, status: Any) -> Optional[str]:
    """The https://<name>.ts.net URL whose Tailscale Serve handler proxies to our port, if any.

    status is the parsed output of `tailscale serve status --json`.
    """
    if not isinstance(status, dict):
        return None
    for host_port, web in (status.get("Web") or {}).items():
        for handler in ((web or {}).get("Handlers") or {}).values():
            target = str((handler or {}).get("Proxy") or "")
            parsed = urlparse(target if "://" in target else "http://" + target)
            if parsed.hostname in LOOPBACK_HOSTS and parsed.port == port:
                name, _, https_port = host_port.rpartition(":")
                return f"https://{name}" + ("" if https_port == "443" else f":{https_port}")
    return None


def detect_tailscale_serve(port: int) -> Optional[str]:
    try:
        out = subprocess.run(["tailscale", "serve", "status", "--json"], capture_output=True, text=True, timeout=3)
        return tailscale_serve_url(port, json.loads(out.stdout or "null"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def phone_url(host: str, port: int, public_url: Optional[str], serve_url: Optional[str],
              tls_name: Optional[str] = None, scheme: str = "http") -> Optional[str]:
    """The base URL another device should open, or None when only this Mac can reach the server.

    tls_name is the certificate's hostname when serving TLS ourselves; the
    phone has to use that name, not the bind address, for the certificate to match.
    """
    if public_url:
        return public_url.rstrip("/")
    if tls_name:
        return f"https://{tls_name}" + ("" if port == 443 else f":{port}")
    if serve_url:
        return serve_url
    if host in LOOPBACK_HOSTS or host in WILDCARD_HOSTS:
        return None
    return f"{scheme}://[{host}]:{port}" if ":" in host else f"{scheme}://{host}:{port}"


def origin_of(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Control cmux workspaces from a browser.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default 127.0.0.1; expose remotely through Tailscale Serve or a TLS proxy)")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--socket", default=None, help="cmux socket path (default: $CMUX_SOCKET_PATH)")
    parser.add_argument("--allow-origin", action="append", default=[],
                        help="extra Origin allowed for POSTs, e.g. https://mac.tailnet.ts.net")
    parser.add_argument("--public-url", default=None,
                        help="URL other devices use to reach this server, e.g. https://mac.tailnet.ts.net; "
                             "printed as a QR code (default: found from `tailscale serve`, or --host)")
    parser.add_argument("--tls-cert", default=None,
                        help="serve HTTPS with this certificate (PEM; may also hold the key), "
                             "e.g. from `tailscale cert <mac>.<tailnet>.ts.net`")
    parser.add_argument("--tls-key", default=None,
                        help="private key for --tls-cert (PEM; default: the cert's name with .key, if it exists)")
    parser.add_argument("--no-qr", action="store_true", help="do not print a QR code of the login URL")
    parser.add_argument("--access-log", action="store_true",
                        help="log every request, not only errors (avoid when the server's own terminal is watched)")
    args = parser.parse_args(argv)

    if args.tls_key and not args.tls_cert:
        parser.error("--tls-key needs --tls-cert")
    ssl_context = None
    if args.tls_cert:
        try:
            ssl_context = load_tls_context(args.tls_cert, args.tls_key or default_tls_key(args.tls_cert))
        except (OSError, ssl.SSLError) as exc:
            print(f"error: cannot load TLS certificate: {exc}", file=sys.stderr)
            return 1

    token = os.environ.get("CMUX_WEB_TOKEN") or secrets.token_urlsafe(24)
    client = CmuxClient(args.socket or default_socket_path(), os.environ.get("CMUX_SOCKET_PASSWORD"))

    try:
        client.call("system.ping")
    except CmuxError as exc:
        if exc.code in ("access_denied", "auth_failed"):
            print(f"error: {exc.message}", file=sys.stderr)
            return 1
        print(f"warning: cmux socket check failed ({exc}); will keep retrying", file=sys.stderr)

    tls_name = cert_hostname(args.tls_cert) if args.tls_cert else None
    serve_url = None if args.public_url or ssl_context else detect_tailscale_serve(args.port)
    remote = phone_url(args.host, args.port, args.public_url, serve_url, tls_name,
                       "https" if ssl_context else "http")
    origins = list(args.allow_origin)
    if remote and remote.startswith("https://"):
        origins.append(origin_of(remote))  # proxies may rewrite Host

    hub = EventHub(client)
    hub.start()
    server = make_server(args.host, args.port, client, hub, token, origins, args.access_log, ssl_context)
    if args.host not in LOOPBACK_HOSTS and ssl_context is None:
        print("warning: listening beyond loopback without TLS; anyone who gets the token can type into your terminals",
              file=sys.stderr)
    print(f"cmux socket: {client.socket_path}")
    local_host = "127.0.0.1" if args.host in WILDCARD_HOSTS else args.host
    scheme = "https" if ssl_context else "http"
    print(f"open: {scheme}://{local_host}:{args.port}/login?token={token}")
    if remote:
        login = f"{remote}/login?token={token}"
        print(f"phone: {login}")
        if not args.no_qr and sys.stdout.isatty():
            print(qrterm.render(qrterm.encode(login)))
    elif args.host in WILDCARD_HOSTS:
        print("phone: pass --public-url <url other devices use> to print a QR code")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
