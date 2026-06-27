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
from pathlib import Path

from ..adapter import Adapter, register
from ..common import (AssistantMessage, Session, ToolCall, ToolResult,
                      UserMessage)

PROJECTS = Path(os.path.expanduser("~/.claude/projects"))
VERSION = "2.1.153"

# Codex tool vocabulary -> Claude's, so converted calls render as native cards.
INBOUND_NAMES = {"exec_command": "Bash", "shell": "Bash",
                 "apply_patch": "Edit", "read_file": "Read", "view_image": "Read"}


def enc(cwd: str) -> str:
    """Claude's project-dir encoding: every non-alphanumeric char -> '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _toolu(call_id: str) -> str:
    return "toolu_" + hashlib.sha1(call_id.encode()).hexdigest()[:24]


class ClaudeAdapter(Adapter):
    name = "claude"

    def locate(self, cwd: str, session_id: str | None = None) -> Path:
        d = PROJECTS / enc(cwd)
        if session_id:
            p = d / f"{session_id}.jsonl"
            if not p.exists():
                raise SystemExit(f"no Claude session {session_id} under {d}")
            return p
        files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            raise SystemExit(f"no Claude sessions found for cwd {cwd} (looked in {d})")
        return files[0]

    def read(self, path: Path) -> Session:
        sid = path.stem
        cwd = git = started = title = ""
        records = []
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("aiTitle"):
                title = d["aiTitle"]
            if d.get("type") not in ("user", "assistant") or not d.get("uuid"):
                continue
            if d.get("isSidechain"):
                continue
            cwd = d.get("cwd", cwd) or cwd
            git = d.get("gitBranch", git) or git
            started = started or d.get("timestamp", "")
            ts = d.get("timestamp", "")
            msg = d.get("message", {})
            role = msg.get("role")
            content = msg.get("content")
            if isinstance(content, str):
                records.append((UserMessage if role == "user" else AssistantMessage)(content, ts))
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
                records.append((UserMessage if role == "user" else AssistantMessage)("\n".join(text), ts))
        s = Session("claude", sid, cwd, records, git, started)
        if title:
            s.extra["title"] = title
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
                msg["model"] = "claude-opus-4-7"
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
