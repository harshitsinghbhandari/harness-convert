"""OpenCode adapter.

Storage is SQLite, not JSONL: $XDG_DATA_HOME/opencode/opencode.db, with three
tables (session, message, part) whose `data` columns hold JSON. A `tool` part
bundles the call AND its result together (state.input / state.output /
state.status), where Codex/Claude keep them as two separate records; read()
splits it back into our ToolCall + ToolResult, write() fuses them.

Read is direct + read-only (the dead source harness never has to run). Write does
NOT poke the live DB (FK to project, WAL, drizzle schema drift = corruption risk);
it emits the canonical `{info, messages}` interchange file that `opencode import`
validates and ingests. Session ids must be ses_-prefixed (`opencode -s` enforces
the prefix; import does not), so foreign ids are mapped to a deterministic
ses_<sha1> and the CLI prints the mapped id in the resume hint.

Times on disk are epoch-milliseconds ints; the common records carry ISO strings
(what Codex/Claude use), so we convert on the boundary in both directions.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..adapter import Adapter, register
from ..common import (AssistantMessage, Session, ToolCall, ToolResult,
                      UserMessage)

DATA = (Path(os.environ.get("XDG_DATA_HOME") or "~/.local/share").expanduser()
        / "opencode")
DB = DATA / "opencode.db"
IMPORTS = DATA / "imports"                       # where write() drops import files
STATE = (Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser()
         / "opencode")
VERSION = "1.14.46"

# Claude/Codex tool vocab -> OpenCode's (cosmetic: history is context, not re-run).
INBOUND_NAMES = {"Bash": "bash", "shell": "bash", "exec_command": "bash",
                 "local_shell": "bash", "Edit": "edit", "apply_patch": "edit",
                 "Write": "write", "Read": "read", "read_file": "read",
                 "view_image": "read"}


def _iso(ms) -> str:
    try:
        return (datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
                .isoformat().replace("+00:00", "Z"))
    except (TypeError, ValueError, OSError):
        return ""


def _ms(iso: str) -> int:
    if not iso:
        return 0
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00"))
                   .timestamp() * 1000)
    except ValueError:
        return 0


def _id(prefix: str, *seed) -> str:
    """Deterministic opencode-style id, so re-importing upserts instead of piling
    up duplicates (import keys off the id)."""
    return prefix + hashlib.sha1(":".join(map(str, seed)).encode()).hexdigest()[:24]


def _default_model() -> dict:
    """The model the resumed scrollback attributes to past assistant turns. Read
    the user's own recent choice so it renders native; fall back to a generic id.
    ponytail: cosmetic metadata on already-happened turns, never re-run."""
    try:
        recent = json.loads((STATE / "model.json").read_text()).get("recent") or []
        if recent:
            return {"providerID": recent[0]["providerID"],
                    "modelID": recent[0]["modelID"]}
    except (OSError, ValueError, KeyError):
        pass
    return {"providerID": "anthropic", "modelID": "claude-sonnet-4-6"}


class OpenCodeAdapter(Adapter):
    name = "opencode"

    # locate() returns "<db>#<session_id>": one db holds every session, so a bare
    # path can't address one. read() splits the id back off.
    def _open(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    def locate(self, cwd: str, session_id: str | None = None) -> Path:
        if not DB.exists():
            raise SystemExit(f"no OpenCode database at {DB}")
        con = self._open()
        try:
            if session_id:
                row = con.execute(
                    "SELECT id FROM session WHERE id = ? OR id LIKE ? "
                    "ORDER BY time_created DESC LIMIT 1",
                    (session_id, f"%{session_id}%")).fetchone()
                if not row:
                    raise SystemExit(f"no OpenCode session {session_id} in {DB}")
                return Path(f"{DB}#{row[0]}")
            row = con.execute(
                "SELECT id FROM session WHERE directory = ? "
                "ORDER BY time_created DESC LIMIT 1", (cwd,)).fetchone()
            if not row:
                raise SystemExit(f"no OpenCode sessions found for cwd {cwd}")
            return Path(f"{DB}#{row[0]}")
        finally:
            con.close()

    def read(self, path: Path) -> Session:
        _, _, sid = str(path).rpartition("#")
        con = self._open()
        try:
            srow = con.execute(
                "SELECT directory, title, time_created FROM session WHERE id = ?",
                (sid,)).fetchone()
            if not srow:
                raise SystemExit(f"no OpenCode session {sid} in {DB}")
            cwd, title, started_ms = srow
            records = []
            msgs = con.execute(
                "SELECT id, data FROM message WHERE session_id = ? "
                "ORDER BY time_created, id", (sid,)).fetchall()
            for mid, mdata in msgs:
                role = json.loads(mdata).get("role")
                Msg = AssistantMessage if role == "assistant" else UserMessage
                parts = con.execute(
                    "SELECT data FROM part WHERE message_id = ? "
                    "ORDER BY time_created, id", (mid,)).fetchall()
                for (pdata,) in parts:
                    p = json.loads(pdata)
                    pt = p.get("type")
                    ts = _iso(p.get("time", {}).get("start") if pt == "tool"
                              else started_ms)
                    if pt == "text":
                        t = p.get("text", "")
                        if t.strip():
                            records.append(Msg(t, _iso(started_ms)))
                    elif pt == "tool":
                        st = p.get("state", {}) or {}
                        cid = p.get("callID", p.get("id", ""))
                        records.append(ToolCall(cid, p.get("tool", "tool"),
                                                st.get("input") or {}, ts))
                        status = st.get("status")
                        if status in ("completed", "error"):
                            records.append(ToolResult(
                                cid, st.get("output", ""), ts,
                                is_error=(status == "error")))
                        # running/pending -> no result; synthesize closes it
                    # reasoning / step-* / file / patch / compaction -> dropped
            s = Session("opencode", sid, cwd or "", records,
                        started_at=_iso(started_ms))
            if title and not title.startswith("New session"):
                s.extra["title"] = title
            return s
        finally:
            con.close()

    def dest_path(self, session: Session, dest_cwd: str) -> Path:
        return IMPORTS / f"{session.session_id}.json"

    def write(self, session: Session, dest_cwd: str) -> Path:
        # opencode session ids must start with ses_; map foreign ids
        # deterministically so re-importing upserts instead of duplicating.
        sid = (session.session_id if session.session_id.startswith("ses_")
               else _id("ses_", session.session_id))
        session.extra["dest_session_id"] = sid
        model = _default_model()
        results = {r.call_id: r for r in session.records
                   if isinstance(r, ToolResult)}
        consumed: set[str] = set()

        def text_part(mid, i, text):
            return {"type": "text", "text": text, "id": _id("prt_", sid, mid, i),
                    "sessionID": sid, "messageID": mid}

        def tool_part(mid, i, call: ToolCall):
            res = results.get(call.call_id)
            consumed.add(call.call_id)
            name = INBOUND_NAMES.get(call.name, call.name.lower())
            ms = _ms(call.ts) or _ms(session.started_at)
            time = {"start": ms, "end": _ms(res.ts) if res else ms}
            # error tool parts carry state.error (a string); completed carry output.
            if res and res.is_error:
                state = {"status": "error", "input": call.input,
                         "error": res.output, "time": time}
            else:
                state = {"status": "completed", "input": call.input,
                         "output": res.output if res else "",
                         "metadata": {}, "title": name, "time": time}
            return {"type": "tool", "tool": name,
                    "callID": _id("call_", call.call_id), "state": state,
                    "id": _id("prt_", sid, mid, i), "sessionID": sid, "messageID": mid}

        # Group records into role-runs; a message is one role's contiguous parts.
        runs: list[tuple[str, list]] = []
        side = None
        for r in session.records:
            s = "user" if isinstance(r, UserMessage) else "assistant"
            if isinstance(r, ToolResult) and r.call_id in consumed:
                continue  # already fused into its ToolCall's tool part
            if s != side:
                runs.append((s, [])); side = s
            runs[-1][1].append(r)

        messages, prev_mid = [], None
        for run_i, (s, recs) in enumerate(runs):
            mid = _id("msg_", sid, run_i)
            parts = []
            for i, r in enumerate(recs):
                if isinstance(r, (UserMessage, AssistantMessage)):
                    parts.append(text_part(mid, i, r.text))
                elif isinstance(r, ToolCall):
                    parts.append(tool_part(mid, i, r))
                elif isinstance(r, ToolResult):  # orphan result (no matching call)
                    parts.append(text_part(mid, i, r.output))
            if not parts:
                continue
            created = _ms(getattr(recs[0], "ts", "")) or _ms(session.started_at)
            if s == "user":
                info = {"role": "user", "time": {"created": created},
                        "agent": "build", "model": model,
                        "summary": {"diffs": []}, "id": mid, "sessionID": sid}
            else:
                info = {"parentID": prev_mid or mid, "role": "assistant",
                        "mode": "build", "agent": "build",
                        "path": {"cwd": dest_cwd, "root": dest_cwd}, "cost": 0,
                        "tokens": {"total": 0, "input": 0, "output": 0,
                                   "reasoning": 0, "cache": {"write": 0, "read": 0}},
                        "modelID": model["modelID"], "providerID": model["providerID"],
                        "time": {"created": created, "completed": created},
                        "finish": "stop", "id": mid, "sessionID": sid}
            messages.append({"info": info, "parts": parts})
            prev_mid = mid

        title = (session.extra.get("out", {}).get("opencode_title")
                 or "Relocated session")
        created = _ms(session.started_at)
        last = _ms(getattr(session.records[-1], "ts", "")) if session.records else created
        doc = {"info": {"id": sid, "slug": _slug(title), "directory": dest_cwd,
                        "projectID": hashlib.sha1(dest_cwd.encode()).hexdigest()[:40],
                        "title": title, "version": VERSION,
                        "summary": {"additions": 0, "deletions": 0, "files": 0},
                        "time": {"created": created, "updated": max(last, created)}},
               "messages": messages}

        dest = self.dest_path(session, dest_cwd)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(doc))
        return dest


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:40] or "relocated"


register(OpenCodeAdapter())
