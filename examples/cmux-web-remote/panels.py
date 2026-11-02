"""Browser, Markdown and file-preview surfaces for the web remote.

Markdown and file-preview panels: the socket API does not report which file a
panel shows, but cmux's saved session (Application Support/cmux/
session-<bundle id>.json, rewritten as panels change) records `filePath` for
each one. That file is cmux's own format and may change; when it does, these
panels fall back to "not available".

Browser panels: `browser.screenshot` returns a PNG of the page; taps, scrolls
and typing run as small scripts through `browser.eval`, because the socket has
no raw pointer input (`browser.input_mouse` is unsupported).
"""

import glob
import hashlib
import json
import os
import threading
from typing import Any, Dict, Optional

MAX_FILE_BYTES = 2 << 20
SESSION_GLOB = os.path.expanduser("~/Library/Application Support/cmux/session-com.cmuxterm.app*.json")


class SessionFiles:
    """surface id -> file path for Markdown and file-preview panels, from cmux's saved session."""

    def __init__(self, pattern: str = SESSION_GLOB) -> None:
        self.pattern = pattern
        self.lock = threading.Lock()
        self.stamp: Any = None
        self.paths: Dict[str, str] = {}

    def _session_file(self) -> Optional[str]:
        # The running app's file, not the "-previous" backup or another channel's.
        candidates = [p for p in glob.glob(self.pattern) if not p.endswith("-previous.json")]
        return max(candidates, key=os.path.getmtime) if candidates else None

    def path_for(self, surface_id: str) -> Optional[str]:
        path = self._session_file()
        if not path:
            return None
        try:
            stamp = (path, os.path.getmtime(path))
        except OSError:
            return None
        with self.lock:
            if stamp != self.stamp:
                try:
                    with open(path, encoding="utf-8") as fh:
                        data = json.load(fh)
                except (OSError, ValueError):
                    data = None
                self.paths = {}
                self._walk(data)
                self.stamp = stamp
            return self.paths.get(surface_id.upper())

    def _walk(self, node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") in ("markdown", "filepreview") and isinstance(node.get("id"), str):
                inner = node.get("markdown") or node.get("filePreview") or {}
                if isinstance(inner, dict) and isinstance(inner.get("filePath"), str):
                    self.paths[node["id"].upper()] = inner["filePath"]
            for value in node.values():
                self._walk(value)
        elif isinstance(node, list):
            for value in node:
                self._walk(value)


def read_file(path: str, known_stamp: Optional[str] = None) -> Dict[str, Any]:
    """The file's text (UTF-8, replaced errors) with a stamp; unchanged=True when it matches known_stamp."""
    st = os.stat(path)
    stamp = f"{st.st_mtime_ns}:{st.st_size}"
    if stamp == known_stamp:
        return {"unchanged": True, "stamp": stamp}
    with open(path, "rb") as fh:
        data = fh.read(MAX_FILE_BYTES + 1)
    truncated = len(data) > MAX_FILE_BYTES
    return {"text": data[:MAX_FILE_BYTES].decode("utf-8", "replace"), "stamp": stamp,
            "truncated": truncated, "name": os.path.basename(path), "path": path}


def shot_hash(png_base64: str) -> str:
    return hashlib.sha1(png_base64.encode("ascii")).hexdigest()[:16]


# fx, fy: 0..1 across the visible viewport (the screenshot).
TAP_SCRIPT = """(() => {
  const x = %(fx)f * innerWidth, y = %(fy)f * innerHeight;
  const el = document.elementFromPoint(x, y);
  if (!el) return null;
  const opts = { bubbles: true, cancelable: true, composed: true, clientX: x, clientY: y, view: window, button: 0 };
  el.dispatchEvent(new PointerEvent('pointerdown', opts));
  el.dispatchEvent(new MouseEvent('mousedown', opts));
  if (typeof el.focus === 'function') el.focus({ preventScroll: true });
  el.dispatchEvent(new PointerEvent('pointerup', opts));
  el.dispatchEvent(new MouseEvent('mouseup', opts));
  el.click();
  return el.tagName.toLowerCase();
})()"""

# Scroll the nearest scrollable ancestor of the point, or the page.
SCROLL_SCRIPT = """(() => {
  const x = %(fx)f * innerWidth, y = %(fy)f * innerHeight;
  const dy = %(dy)f * innerHeight, dx = %(dx)f * innerWidth;
  let el = document.elementFromPoint(x, y);
  while (el && el !== document.body && el !== document.documentElement) {
    const s = getComputedStyle(el);
    const canY = dy && /(auto|scroll|overlay)/.test(s.overflowY) && el.scrollHeight > el.clientHeight;
    const canX = dx && /(auto|scroll|overlay)/.test(s.overflowX) && el.scrollWidth > el.clientWidth;
    if (canY || canX) { el.scrollBy(dx, dy); return 'element'; }
    el = el.parentElement;
  }
  window.scrollBy(dx, dy);
  return 'page';
})()"""

# Insert text where the page's focus is, as typing would (fires input events).
TYPE_SCRIPT = """(() => {
  const el = document.activeElement;
  if (!el || el === document.body) return false;
  return document.execCommand('insertText', false, %(text)s);
})()"""


def tap_script(fx: float, fy: float) -> str:
    return TAP_SCRIPT % {"fx": _unit(fx), "fy": _unit(fy)}


def scroll_script(fx: float, fy: float, dx: float, dy: float) -> str:
    return SCROLL_SCRIPT % {"fx": _unit(fx), "fy": _unit(fy), "dx": _clamp(dx, -5, 5), "dy": _clamp(dy, -5, 5)}


def type_script(text: str) -> str:
    return TYPE_SCRIPT % {"text": json.dumps(text)}


def _unit(v: float) -> float:
    return _clamp(v, 0.0, 1.0)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))
