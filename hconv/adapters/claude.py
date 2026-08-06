"""Claude Code adapter.

Transcript: ~/.claude/projects/<enc(cwd)>/<sessionId>.jsonl, a parentUuid tree.
One row set both renders and feeds the model (no dual stream). Resume keys off
(launch cwd -> project dir) + (filename stem == sessionId) + the tree.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..adapter import Adapter, SessionRef, register
from ..common import (AssistantMessage, Session, ToolCall, ToolResult,
                      UserMessage)

PROJECTS = Path(os.path.expanduser("~/.claude/projects"))
VERSION = "2.1.153"
# Only the fallback. read() captures the SOURCE session's own model into
# extra["source_model"] and write() prefers that, so a converted transcript
# attributes each turn to the model that actually produced it. This constant
# is used only when the source carried no model at all (e.g. a harness that
# does not record one), and it is the one thing here that goes stale on its
# own: bump it when the current Claude model changes.
MODEL = "claude-opus-5"

# Codex tool vocabulary -> Claude's, so converted calls render as native cards.
INBOUND_NAMES = {"exec_command": "Bash", "shell": "Bash",
                 "apply_patch": "Edit", "read_file": "Read", "view_image": "Read"}

# Harness-control wrappers that pollute a first-user-message-derived title.
# See docs/codex-external-agent-migration.md section 4.4 (title.rs:5).
_CONTROL_TAGS = {
    "command-message", "command-name", "command-args", "local-command-caveat",
    "local-command-stderr", "local-command-stdout", "task-notification",
    "system-reminder", "ide_opened_file", "ide_selection",
}
_TAG_RE = re.compile(r"<(/?)([A-Za-z0-9_-]+)>")


def enc(cwd: str) -> str:
    """Claude's project-dir encoding: every non-alphanumeric char -> '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _toolu(call_id: str) -> str:
    return "toolu_" + hashlib.sha1(call_id.encode()).hexdigest()[:24]


def _iso_utc(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _record_ts(d: dict, fallback_ts: str) -> str:
    """RFC3339 timestamp, else timestamp_ms, else the transcript's own mtime."""
    ts = d.get("timestamp")
    if ts:
        return ts
    ms = d.get("timestamp_ms")
    if isinstance(ms, (int, float)):
        return _iso_utc(ms / 1000)
    return fallback_ts


def _strip_control_wrappers(text: str) -> str:
    """Strip leading harness-control blocks (title use only; never mutates the
    stored message body). Nesting-aware; fails closed (strips nothing) on any
    mismatched or unclosed tag anywhere inside a wrapper being consumed."""
    pos = 0
    while True:
        rest = text[pos:]
        lstripped = rest.lstrip()
        skip = len(rest) - len(lstripped)
        m = _TAG_RE.match(lstripped)
        if not m or m.group(1) or m.group(2) not in _CONTROL_TAGS:
            break
        scan_start = pos + skip + m.end()
        stack = [m.group(2)]
        closed_at = None
        for tm in _TAG_RE.finditer(text, scan_start):
            name = tm.group(2)
            if not tm.group(1):
                stack.append(name)
            else:
                if not stack or stack[-1] != name:
                    return text
                stack.pop()
                if not stack:
                    closed_at = tm.end()
                    break
        if closed_at is None:
            return text
        pos = closed_at
    return text[pos:]


def _fallback_title(text: str | None) -> str:
    if not text:
        return ""
    for line in _strip_control_wrappers(text).splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _cap_title(title: str) -> str:
    return title if len(title) <= 120 else title[:117].rstrip() + "..."


def _peek_title(path: Path) -> str:
    """Light scan for custom-title / ai-title without a full transcript parse."""
    custom = ai = ""
    try:
        with path.open() as fh:
            for i, line in enumerate(fh):
                if i > 200:  # titles are near the top or sparse; don't walk MB files
                    break
                if not line.strip():
                    continue
                if "custom-title" not in line and "ai-title" not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "custom-title":
                    v = (d.get("customTitle") or "").strip()
                    if v:
                        custom = v
                elif d.get("type") == "ai-title":
                    v = (d.get("aiTitle") or "").strip()
                    if v:
                        ai = v
                if custom:
                    break
    except OSError:
        return ""
    return custom or ai


class ClaudeAdapter(Adapter):
    name = "claude"

    def list_sessions(self, cwd: str, limit: int = 10) -> list[SessionRef]:
        if limit < 1:
            return []
        d = PROJECTS / enc(cwd)
        if not d.is_dir():
            return []
        files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for p in files[:limit]:
            out.append(SessionRef(
                path=p, session_id=p.stem, mtime=p.stat().st_mtime,
                title=_peek_title(p),
            ))
        return out

    def locate(self, cwd: str, session_id: str | None = None) -> Path:
        d = PROJECTS / enc(cwd)
        if session_id:
            p = d / f"{session_id}.jsonl"
            if not p.exists():
                raise SystemExit(f"no Claude session {session_id} under {d}")
            return p
        refs = self.list_sessions(cwd, limit=1)
        if not refs:
            raise SystemExit(f"no Claude sessions found for cwd {cwd} (looked in {d})")
        return refs[0].path

    def read(self, path: Path) -> Session:
        sid = path.stem
        cwd = git = started = custom_title = ai_title = src_model = ""
        first_user_text = None
        mtime_ts = _iso_utc(path.stat().st_mtime)
        records = []
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") == "custom-title":
                v = (d.get("customTitle") or "").strip()
                if v:
                    custom_title = v
            elif d.get("type") == "ai-title":
                v = (d.get("aiTitle") or "").strip()
                if v:
                    ai_title = v
            if d.get("type") not in ("user", "assistant") or not d.get("uuid"):
                continue
            if d.get("isSidechain"):
                continue
            cwd = d.get("cwd", cwd) or cwd
            git = d.get("gitBranch", git) or git
            ts = _record_ts(d, mtime_ts)
            started = started or ts
            msg = d.get("message", {})
            role = msg.get("role")
            # Surplus, not part of the common floor: the model that produced
            # this turn. Last assistant row wins (a resumed session can change
            # model mid-thread; the newest is the better default).
            if role == "assistant" and msg.get("model"):
                src_model = msg["model"]
            content = msg.get("content")
            if isinstance(content, str):
                records.append((UserMessage if role == "user" else AssistantMessage)(content, ts))
                if role == "user" and first_user_text is None:
                    first_user_text = content
                continue
            if not isinstance(content, list):
                continue
            text = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    text.append(b.get("text", ""))
                elif bt == "tool_use":
                    if text:
                        records.append(AssistantMessage("\n".join(text), ts)); text = []
                    records.append(ToolCall(b["id"], b.get("name", "tool"),
                                            b.get("input", {}) or {}, ts))
                elif bt == "tool_result":
                    c = b.get("content", "")
                    if isinstance(c, list):
                        c = "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in c)
                    records.append(ToolResult(b["tool_use_id"],
                                              c if isinstance(c, str) else json.dumps(c),
                                              ts, bool(b.get("is_error"))))
                # thinking / redacted_thinking -> dropped
            if text:
                joined = "\n".join(text)
                records.append((UserMessage if role == "user" else AssistantMessage)(joined, ts))
                if role == "user" and first_user_text is None:
                    first_user_text = joined
        s = Session("claude", sid, cwd, records, git, started)
        title = custom_title or ai_title or _fallback_title(first_user_text)
        if title:
            s.extra["title"] = _cap_title(title)
        if src_model:
            s.extra["source_model"] = src_model
        return s

    def dest_path(self, session: Session, dest_cwd: str) -> Path:
        return PROJECTS / enc(dest_cwd) / f"{session.session_id}.jsonl"

    def write(self, session: Session, dest_cwd: str) -> Path:
        # records -> Claude rows, merging consecutive same-side blocks into one
        # message (Anthropic tool-ordering), chaining parentUuid into a tree.
        merged, side, blocks = [], None, []

        def block_of(r):
            if isinstance(r, UserMessage):
                return "user", {"type": "text", "text": r.text}
            if isinstance(r, AssistantMessage):
                return "assistant", {"type": "text", "text": r.text}
            if isinstance(r, ToolCall):
                return "assistant", {"type": "tool_use", "id": _toolu(r.call_id),
                                     "name": INBOUND_NAMES.get(r.name, r.name), "input": r.input}
            return "user", {"type": "tool_result", "tool_use_id": _toolu(r.call_id),
                            "content": r.output, **({"is_error": True} if r.is_error else {})}

        ts = session.started_at
        for r in session.records:
            s, b = block_of(r)
            if s != side and blocks:
                merged.append((side, blocks, ts)); blocks = []
            side, ts = s, getattr(r, "ts", "") or session.started_at
            blocks.append(b)
        if blocks:
            merged.append((side, blocks, ts))

        rows, prev = [], None
        for s, blks, ts in merged:
            u = str(uuid.uuid4())
            content = (blks[0]["text"] if s == "user" and len(blks) == 1
                       and blks[0]["type"] == "text" else blks)
            msg = {"role": s, "content": content}
            if s == "assistant":
                msg["model"] = session.extra.get("source_model") or MODEL
            rows.append({"parentUuid": prev, "isSidechain": False, "userType": "external",
                         "cwd": dest_cwd, "sessionId": session.session_id, "version": VERSION,
                         "gitBranch": session.git_branch, "type": s, "message": msg,
                         "uuid": u, "timestamp": ts})
            prev = u

        # N^2 surplus: a session title rides as an ai-title row.
        ai_title = session.extra.get("out", {}).get("ai_title")
        if ai_title:
            rows.append({"type": "ai-title", "aiTitle": ai_title,
                         "sessionId": session.session_id, "uuid": str(uuid.uuid4()),
                         "timestamp": session.started_at})

        dest = self.dest_path(session, dest_cwd)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return dest


register(ClaudeAdapter())
