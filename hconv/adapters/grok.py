"""Grok Build adapter.

Transcript lives under $GROK_HOME/sessions/<urlencode(cwd)>/<uuid>/ as a small
directory, not a single JSONL file:

  summary.json         identity + title + counts (the picker index entry)
  chat_history.jsonl   model context (user / assistant+tool_calls / tool_result)
  updates.jsonl        ACP session/update stream (UI scrollback + restore path)

Docs say updates.jsonl is authoritative for resume; forge-testing showed that
summary + chat_history + a minimal updates stream is enough for
`grok --resume <uuid>` to load history and continue. Write emits all three.
Read prefers chat_history for the common records (clean tool pairing) and
summary.json for identity/title.

Private reasoning is encrypted per provider and is dropped. System rows and
scaffolding user turns (user_info / system-reminder without a <user_query>)
are dropped. Foreign non-UUID ids map to a deterministic uuid5 so re-runs
land on the same destination session.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote

from ..adapter import Adapter, SessionRef, register
from ..common import (AssistantMessage, Session, ToolCall, ToolResult,
                      UserMessage)

HOME = Path(os.environ.get("GROK_HOME") or os.path.expanduser("~/.grok"))
SESSIONS = HOME / "sessions"
MODEL_ID = "grok-4.5"
AGENT_NAME = "grok-build-plan"
CHAT_FORMAT_VERSION = 1

# Other harness vocab -> Grok's (cosmetic: history is context, not re-run).
INBOUND_NAMES = {
    "Bash": "run_terminal_command",
    "shell": "run_terminal_command",
    "exec_command": "run_terminal_command",
    "local_shell": "run_terminal_command",
    "Read": "read_file",
    "read_file": "read_file",
    "view_image": "read_file",
    "Edit": "search_replace",
    "apply_patch": "search_replace",
    "StrReplace": "search_replace",
    "Write": "write",
    "list_dir": "list_dir",
}

# Grok vocab -> Claude-ish middle other adapters already remap on write.
OUTBOUND_NAMES = {
    "run_terminal_command": "Bash",
    "read_file": "Read",
    "search_replace": "Edit",
    "write": "Write",
}

_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def enc(cwd: str) -> str:
    """Grok's project-dir encoding: full URL percent-encode, no safe chars."""
    return quote(cwd, safe="")


def _dest_id(session_id: str) -> str:
    """Grok requires a UUID session id. Keep valid ones; map the rest
    deterministically so re-conversion upserts the same destination."""
    if _UUID_RE.match(session_id or ""):
        return str(uuid.UUID(session_id))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"harness-convert:grok:{session_id}"))


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _unix(iso: str) -> int:
    if not iso:
        return int(time.time())
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return int(time.time())


def _ms(iso: str) -> int:
    return _unix(iso) * 1000


def _user_text(content) -> str | None:
    """Pull the human-typed text out of a user row. Prefer <user_query> bodies;
    drop pure scaffolding (user_info / system-reminder / skill dumps)."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        return None
    qs = _QUERY.findall(text)
    if qs:
        return "\n\n".join(q.strip() for q in qs if q.strip())
    stripped = text.strip()
    if not stripped:
        return None
    # Scaffolding turns always open with a known harness tag and have no query.
    if stripped.startswith(("<user_info>", "<system-reminder>", "<git_status>")):
        return None
    return stripped


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            val = json.loads(raw)
            return val if isinstance(val, dict) else {"input": val}
        except json.JSONDecodeError:
            return {"raw": raw}
    return {}


class GrokAdapter(Adapter):
    name = "grok"

    def _group_dirs(self) -> list[Path]:
        if not SESSIONS.is_dir():
            return []
        return [p for p in SESSIONS.iterdir() if p.is_dir()]

    def _summary(self, session_dir: Path) -> dict:
        p = session_dir / "summary.json"
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return {}

    def _cwd_of(self, group: Path, session_dir: Path | None = None) -> str:
        if session_dir is not None:
            cwd = (self._summary(session_dir).get("info") or {}).get("cwd")
            if cwd:
                return cwd
        cwd_file = group / ".cwd"
        if cwd_file.exists():
            try:
                return cwd_file.read_text().strip()
            except OSError:
                pass
        try:
            return unquote(group.name)
        except Exception:
            return ""

    def _iter_sessions(self, cwd: str | None = None):
        """Yield (updated_at_epoch, session_dir, summary) for real sessions."""
        for group in self._group_dirs():
            for child in group.iterdir():
                if not child.is_dir() or child.name.startswith("."):
                    continue
                if not (child / "chat_history.jsonl").exists() and not (child / "summary.json").exists():
                    continue
                summary = self._summary(child)
                info = summary.get("info") or {}
                scwd = info.get("cwd") or self._cwd_of(group, child)
                if cwd is not None and scwd != cwd:
                    continue
                updated = summary.get("updated_at") or summary.get("last_active_at") or summary.get("created_at") or ""
                try:
                    epoch = datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    epoch = child.stat().st_mtime
                yield epoch, child, summary

    def list_sessions(self, cwd: str, limit: int = 10) -> list[SessionRef]:
        if limit < 1:
            return []
        found = sorted(self._iter_sessions(cwd=cwd), key=lambda t: t[0], reverse=True)
        out = []
        for epoch, d, summary in found[:limit]:
            info = summary.get("info") or {}
            sid = info.get("id") or d.name
            title = (summary.get("generated_title")
                     or summary.get("session_summary")
                     or summary.get("title")
                     or "")
            if title in ("New session", "New Agent"):
                title = ""
            out.append(SessionRef(path=d, session_id=sid, mtime=epoch, title=title))
        return out

    def locate(self, cwd: str, session_id: str | None = None) -> Path:
        if not SESSIONS.is_dir():
            raise SystemExit(f"no Grok sessions directory at {SESSIONS}")
        if session_id:
            # Exact dir name match under any group, restricted to cwd when known.
            hits = []
            for _epoch, d, summary in self._iter_sessions(cwd=None):
                if session_id not in d.name and session_id != (summary.get("info") or {}).get("id"):
                    continue
                scwd = (summary.get("info") or {}).get("cwd") or self._cwd_of(d.parent, d)
                if scwd == cwd or not cwd:
                    hits.append(d)
            # Also accept a bare id even if cwd filter would miss (user passed id).
            if not hits:
                for group in self._group_dirs():
                    for child in group.iterdir():
                        if child.is_dir() and session_id in child.name:
                            hits.append(child)
            if not hits:
                raise SystemExit(f"no Grok session {session_id} for cwd {cwd} "
                                 f"(looked in {SESSIONS})")
            # Prefer cwd match if multiple.
            for h in hits:
                if (self._summary(h).get("info") or {}).get("cwd") == cwd:
                    return h
            return hits[0]

        refs = self.list_sessions(cwd, limit=1)
        if not refs:
            raise SystemExit(f"no Grok sessions found for cwd {cwd} (looked in {SESSIONS})")
        return refs[0].path

    def read(self, path: Path) -> Session:
        d = Path(path)
        if d.is_file():
            d = d.parent
        summary = self._summary(d)
        info = summary.get("info") or {}
        sid = info.get("id") or d.name
        cwd = info.get("cwd") or self._cwd_of(d.parent, d)
        started = summary.get("created_at") or ""
        git = summary.get("head_branch") or ""
        ts = started
        records = []
        chat = d / "chat_history.jsonl"
        if chat.exists():
            for line in chat.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = row.get("type")
                if kind == "user":
                    text = _user_text(row.get("content"))
                    if text:
                        records.append(UserMessage(text, ts))
                elif kind == "assistant":
                    content = row.get("content") or ""
                    if isinstance(content, list):
                        content = "\n".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    if isinstance(content, str) and content.strip():
                        records.append(AssistantMessage(content, ts))
                    for tc in row.get("tool_calls") or []:
                        if not isinstance(tc, dict):
                            continue
                        name = tc.get("name") or "tool"
                        records.append(ToolCall(
                            tc.get("id") or "",
                            OUTBOUND_NAMES.get(name, name),
                            _parse_args(tc.get("arguments")),
                            ts,
                        ))
                elif kind == "tool_result":
                    out = row.get("content", "")
                    if not isinstance(out, str):
                        out = json.dumps(out)
                    records.append(ToolResult(
                        row.get("tool_call_id") or "",
                        out, ts,
                        out.startswith("Error:"),
                    ))
                # system / reasoning / backend_tool_call -> dropped

        s = Session("grok", sid, cwd, records, git, started)
        title = (summary.get("generated_title")
                 or summary.get("session_summary")
                 or summary.get("title")
                 or "")
        if title and title not in ("", "New session", "New Agent"):
            s.extra["title"] = title
        return s

    def dest_path(self, session: Session, dest_cwd: str) -> Path:
        sid = session.extra.get("dest_session_id") or _dest_id(session.session_id)
        return SESSIONS / enc(dest_cwd) / sid

    def write(self, session: Session, dest_cwd: str) -> Path:
        sid = _dest_id(session.session_id)
        session.extra["dest_session_id"] = sid
        dest = SESSIONS / enc(dest_cwd) / sid
        dest.mkdir(parents=True, exist_ok=True)

        started = session.started_at or _iso_now()
        # Use last record ts as updated_at when present.
        last_ts = started
        for r in session.records:
            t = getattr(r, "ts", "") or ""
            if t:
                last_ts = t
        title = (session.extra.get("out", {}).get("grok_title")
                 or session.extra.get("title")
                 or "Relocated session")

        chat_rows: list[dict] = []
        updates: list[dict] = []
        event_n = 0
        prompt_index = 0
        results = {r.call_id: r for r in session.records if isinstance(r, ToolResult)}
        # Pending assistant text + tool_calls coalesced into one chat row.
        pending_text = ""
        pending_calls: list[dict] = []
        pending_ts = started

        def flush_assistant():
            nonlocal pending_text, pending_calls, pending_ts
            if not pending_text and not pending_calls:
                return
            row: dict = {"type": "assistant", "content": pending_text or "",
                         "model_id": MODEL_ID}
            if pending_calls:
                row["tool_calls"] = pending_calls
            chat_rows.append(row)
            if pending_text:
                _update("agent_message_chunk", {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": pending_text},
                }, pending_ts)
            for tc in pending_calls:
                name = tc["name"]
                args = _parse_args(tc["arguments"])
                cid = tc["id"]
                _update("tool_call", {
                    "sessionUpdate": "tool_call",
                    "toolCallId": cid,
                    "title": name,
                    "rawInput": args,
                }, pending_ts)
                res = results.get(cid)
                out = res.output if res else ""
                status = "failed" if (res and res.is_error) else "completed"
                _update("tool_call_update", {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": cid,
                    "status": status,
                    "content": [{"type": "content",
                                 "content": {"type": "text", "text": out}}],
                    "rawOutput": {"type": name, "content": out},
                }, pending_ts)
            pending_text, pending_calls = "", []

        def _update(kind: str, update: dict, ts: str):
            nonlocal event_n
            event_n += 1
            updates.append({
                "timestamp": _unix(ts),
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": update,
                    "_meta": {
                        "eventId": f"{sid}-{event_n}",
                        "agentTimestampMs": _ms(ts),
                    },
                },
            })

        for r in session.records:
            ts = getattr(r, "ts", "") or started
            if isinstance(r, UserMessage):
                flush_assistant()
                body = f"<user_query>\n{r.text}\n</user_query>"
                chat_rows.append({
                    "type": "user",
                    "content": [{"type": "text", "text": body}],
                    "prompt_index": prompt_index,
                })
                _update("user_message_chunk", {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": r.text},
                    "_meta": {"modelId": MODEL_ID, "promptIndex": prompt_index},
                }, ts)
                prompt_index += 1
            elif isinstance(r, AssistantMessage):
                # Consecutive assistant text merges; tool calls force a flush after.
                if pending_calls:
                    flush_assistant()
                if pending_text:
                    pending_text = pending_text + "\n" + r.text
                else:
                    pending_text = r.text
                pending_ts = ts
            elif isinstance(r, ToolCall):
                name = INBOUND_NAMES.get(r.name, r.name)
                pending_calls.append({
                    "id": r.call_id or f"call-{uuid.uuid4()}",
                    "name": name,
                    "arguments": json.dumps(r.input if isinstance(r.input, dict) else {"input": r.input}),
                })
                pending_ts = ts
            elif isinstance(r, ToolResult):
                # Tool results land as their own chat rows after the assistant
                # that issued the call. Flush the pending assistant first so
                # tool_calls appear before tool_result (model ordering).
                flush_assistant()
                chat_rows.append({
                    "type": "tool_result",
                    "tool_call_id": r.call_id,
                    "content": r.output,
                })

        flush_assistant()

        n_chat = len(chat_rows)
        n_updates = len(updates)
        summary = {
            "info": {"id": sid, "cwd": dest_cwd},
            "session_summary": title,
            "generated_title": title,
            "created_at": started,
            "updated_at": last_ts,
            "last_active_at": last_ts,
            "num_messages": n_updates,
            "num_chat_messages": n_chat,
            "current_model_id": MODEL_ID,
            "next_trace_turn": 0,
            "chat_format_version": CHAT_FORMAT_VERSION,
            "grok_home": str(HOME),
            "agent_name": AGENT_NAME,
            "sandbox_profile": "off",
            "reasoning_effort": "high",
        }
        if session.git_branch:
            summary["head_branch"] = session.git_branch

        (dest / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        (dest / "chat_history.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in chat_rows))
        (dest / "updates.jsonl").write_text(
            "".join(json.dumps(u, ensure_ascii=False) + "\n" for u in updates))
        return dest


register(GrokAdapter())
