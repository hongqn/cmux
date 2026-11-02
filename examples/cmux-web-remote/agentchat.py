"""Read Claude Code transcripts as chat items for the web remote's chat view.

cmux knows which terminal runs which agent session and where its transcript
lives (`chat.sessions.dump`). The transcript is Claude Code's JSONL log; this
module turns its tail into small display items, following the rules of cmux's
own ClaudeTranscriptParser (Packages/Shared/CmuxAgentChat), which feeds the
iOS app's chat view.

Transcripts grow to hundreds of megabytes, so pages are read backwards from the
end and live updates are read forward from a byte offset.
"""

import difflib
import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

NOISE_PREFIXES = ("<command-name>", "<local-command", "<system-reminder")
SUMMARY_KEYS = ("file_path", "notebook_path", "path", "pattern", "command", "query",
                "url", "description", "prompt", "skill", "name")
EDIT_TOOLS = {"Edit", "MultiEdit", "NotebookEdit"}
TEXT_LIMIT = 20000
DETAIL_LIMIT = 4000
RESULT_LIMIT = 4000
SUMMARY_LIMIT = 160
# Bookkeeping lines (api-request blobs run to megabytes) start with their type,
# so they are skipped without parsing; chat lines start with other keys.
LEADING_TYPE_RE = re.compile(rb'^\{"type":"([a-z_-]+)"')
CHAT_TYPES = {b"user", b"assistant", b"system"}
CHUNK = 1 << 20
MAX_BACK_SCAN = 48 << 20
MAX_FORWARD_READ = 16 << 20


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n… ({len(text) - limit} more characters)"


def _one_line(text: str, limit: int = SUMMARY_LIMIT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _diff(old: str, new: str) -> Tuple[str, int, int]:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=2))[2:]
    adds = sum(1 for l in lines if l.startswith("+"))
    dels = sum(1 for l in lines if l.startswith("-"))
    return "\n".join(lines), adds, dels


def _tool_item(block: Dict[str, Any], base: Dict[str, Any]) -> List[Dict[str, Any]]:
    name = block.get("name") or "tool"
    call_id = block.get("id") or ""
    raw_input = block.get("input")
    inp: Dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
    item = dict(base, kind="tool", id=call_id or base["id"], tool=name)
    if name == "Bash" and isinstance(inp.get("command"), str):
        item.update(summary=_one_line(inp["command"]), command=_cut(inp["command"], DETAIL_LIMIT))
        if inp.get("description"):
            item["note"] = _one_line(str(inp["description"]))
        return [item]
    if (name == "Write" or name in EDIT_TOOLS) and (inp.get("file_path") or inp.get("notebook_path")):
        path = inp.get("file_path") or inp.get("notebook_path")
        if name == "Write":
            diff, adds, dels = _diff("", inp.get("content") or "")
        elif name == "MultiEdit":
            parts = [_diff(e.get("old_string") or "", e.get("new_string") or "") for e in inp.get("edits") or []]
            diff = "\n…\n".join(p[0] for p in parts)
            adds, dels = sum(p[1] for p in parts), sum(p[2] for p in parts)
        else:
            diff, adds, dels = _diff(inp.get("old_string") or "", inp.get("new_string") or inp.get("new_source") or "")
        item.update(summary=path, path=path, diff=_cut(diff, DETAIL_LIMIT), additions=adds, deletions=dels)
        return [item]
    if name == "AskUserQuestion" and isinstance(inp.get("questions"), list):
        out = []
        for i, q in enumerate(inp["questions"]):
            if not isinstance(q, dict) or not q.get("question"):
                continue
            out.append(dict(base, kind="question", id=f"{call_id}#{i}", call_id=call_id,
                            prompt=q["question"], header=q.get("header") or "",
                            multi=bool(q.get("multiSelect")),
                            options=[{"label": o.get("label", ""), "detail": o.get("description") or ""}
                                     for o in q.get("options") or [] if isinstance(o, dict)]))
        if out:
            return out
    if name == "TodoWrite" and isinstance(inp.get("todos"), list):
        todos = [{"text": t.get("content") or t.get("activeForm") or "", "status": t.get("status") or ""}
                 for t in inp["todos"] if isinstance(t, dict)]
        done = sum(1 for t in todos if t["status"] == "completed")
        item.update(summary=f"{done}/{len(todos)} done", todos=todos)
        return [item]
    summary = ""
    for key in SUMMARY_KEYS:
        value = inp.get(key)
        if isinstance(value, str) and value.strip():
            summary = _one_line(value)
            break
    item.update(summary=summary, detail=_cut(json.dumps(inp, ensure_ascii=False, indent=1), DETAIL_LIMIT) if inp else "")
    return [item]


def _command_text(text: str) -> Optional[str]:
    """`/name args` for a slash command the user typed, else None."""
    name = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
    if not name:
        return None
    args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
    cmd = name.group(1).strip()
    if not cmd.startswith("/"):
        cmd = "/" + cmd
    return (cmd + " " + (args.group(1).strip() if args else "")).strip()


def _user_text_items(text: str, base: Dict[str, Any]) -> List[Dict[str, Any]]:
    trimmed = text.strip()
    if not trimmed:
        return []
    command = _command_text(trimmed) if trimmed.startswith("<command-") else None
    if command:
        return [dict(base, kind="user", text=command)]
    if trimmed.startswith(NOISE_PREFIXES):
        return []
    if trimmed.startswith("[Request interrupted by user"):
        return [dict(base, kind="note", text="Interrupted")]
    if trimmed.startswith("<task-notification>"):
        summary = re.search(r"<summary>(.*?)</summary>", trimmed, re.S)
        return [dict(base, kind="note", text=_one_line(summary.group(1) if summary else "Background task update"))]
    return [dict(base, kind="user", text=_cut(text, TEXT_LIMIT))]


def parse_line(raw: bytes) -> List[Dict[str, Any]]:
    """Display items for one transcript line (often none)."""
    m = LEADING_TYPE_RE.match(raw)
    if m and m.group(1) not in CHAT_TYPES:
        return []
    try:
        d = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(d, dict):
        return []
    kind = d.get("type")
    if kind not in ("user", "assistant", "system") or d.get("isSidechain") or d.get("isMeta"):
        return []
    uid = d.get("uuid") or ""
    base = {"id": uid, "ts": d.get("timestamp") or ""}
    if kind == "system":
        if d.get("subtype") == "compact_boundary":
            return [dict(base, kind="note", text="Conversation compacted")]
        return []
    if d.get("isCompactSummary"):
        return []
    content = (d.get("message") or {}).get("content")
    items: List[Dict[str, Any]] = []
    blocks = content if isinstance(content, list) else [{"type": "text", "text": content}] if isinstance(content, str) else []
    for i, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        bbase = dict(base, id=f"{uid}:{i}")
        btype = block.get("type")
        if kind == "user":
            if btype == "text":
                items += _user_text_items(block.get("text") or "", bbase)
            elif btype == "image":
                items.append(dict(bbase, kind="user", text="[image]"))
            elif btype == "tool_result":
                text = _result_text(block.get("content"))
                items.append(dict(bbase, kind="result", call_id=block.get("tool_use_id") or "",
                                  is_error=bool(block.get("is_error")), text=_cut(text, RESULT_LIMIT)))
        else:
            if btype == "text" and (block.get("text") or "").strip():
                items.append(dict(bbase, kind="assistant", text=_cut(block["text"], TEXT_LIMIT)))
            elif btype == "tool_use":
                items += _tool_item(block, bbase)
    return items


def parse_lines(lines: Iterable[bytes]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line in lines:
        out += parse_line(line)
    return out


def read_tail(path: str, before: Optional[int] = None, want: int = 80, chunk: int = CHUNK) -> Dict[str, Any]:
    """Items from complete lines ending at byte offset `before` (default: end of file).

    Reads backwards until at least `want` displayable items (not counting tool
    results) are found, the file starts, or MAX_BACK_SCAN bytes were read.
    Returns the items in file order with `start` (offset of the first line
    read; pass it as `before` for the previous page) and `end`.
    """
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        end = size if before is None else max(0, min(before, size))
        if before is None:
            # Ignore a final line that is still being written: end after the last newline.
            pos = end
            while pos > 0:
                step = min(chunk, pos)
                fh.seek(pos - step)
                cut = fh.read(step).rfind(b"\n")
                if cut >= 0:
                    end = pos - step + cut + 1
                    break
                pos -= step
            else:
                end = 0
        pos = end
        carry = b""
        pages: List[List[Dict[str, Any]]] = []
        count = 0
        scanned = 0
        while pos > 0 and count < want and scanned < MAX_BACK_SCAN:
            step = min(chunk, pos)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + carry
            scanned += step
            lines = buf.split(b"\n")
            carry = lines[0] if pos > 0 else b""
            body = lines[1:] if pos > 0 else lines
            items = parse_lines(l for l in body if l)
            pages.append(items)
            count += sum(1 for it in items if it["kind"] != "result")
        start = pos + len(carry)
        if pos == 0 and carry:
            pages.append(parse_line(carry))
            start = 0
    items = [it for page in reversed(pages) for it in page]
    return {"items": items, "start": start, "end": end}


def read_since(path: str, offset: int) -> Dict[str, Any]:
    """Items from complete lines after byte `offset`, and the offset to continue from."""
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        if offset > size:  # truncated or replaced; start over from the tail
            return dict(read_tail(path), reset=True)
        if size - offset > MAX_FORWARD_READ:
            return dict(read_tail(path), reset=True)
        fh.seek(offset)
        buf = fh.read(size - offset)
    cut = buf.rfind(b"\n")
    if cut < 0:
        return {"items": [], "end": offset}
    return {"items": parse_lines(l for l in buf[:cut].split(b"\n") if l), "end": offset + cut + 1}


def session_state(raw: str) -> str:
    """cmux's ChatAgentState description -> idle | working | needs_input | ended."""
    raw = (raw or "").strip()
    if raw.startswith("working"):
        return "working"
    if raw.startswith("needsInput") or raw.startswith("needs_input"):
        return "needs_input"
    if raw.startswith("ended"):
        return "ended"
    return "idle" if raw.startswith("idle") else raw or "unknown"
