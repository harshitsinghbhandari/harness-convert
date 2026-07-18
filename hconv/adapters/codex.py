"""Codex adapter.

Transcript: ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl, a flat log with
TWO parallel streams: response_item (model context, replayed on resume) and
event_msg (UI scrollback, replayed to paint history). BOTH are required, else
resume works but the conversation is invisible. Sessions are stored by date, so
locating "latest for cwd" means scanning rollouts and filtering session_meta.cwd.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from ..adapter import Adapter, register
from ..common import (AssistantMessage, Session, ToolCall, ToolResult,
                      UserMessage)

SESSIONS = Path(os.path.expanduser("~/.codex/sessions"))
INDEX = Path(os.path.expanduser("~/.codex/session_index.jsonl"))
CLI_VERSION = "0.144.5"
# codex >= 0.144 registers every rollout in its threads db (state_5.sqlite),
# building the row from session_meta. model_provider is NOT NULL there; if the
# meta omits it the row backfills as "" and TUI resume dies with
# "Model provider `` not found". ponytail: hardcoded stock provider, read the
# user's config.toml if custom providers ever matter.
MODEL_PROVIDER = "openai"

# Claude tool vocabulary -> Codex's. (Cosmetic: history is context, not re-run.)
INBOUND_NAMES = {"Bash": "shell", "Edit": "apply_patch", "Write": "apply_patch",
                 "Read": "read_file"}
# Which destination tool names render as a shell card vs a patch card.
SHELL_NAMES = {"shell", "exec_command", "Bash", "local_shell"}
PATCH_NAMES = {"apply_patch", "Edit", "Write"}


def _call(call_id: str) -> str:
    return "call_" + hashlib.sha1(call_id.encode()).hexdigest()[:22]


class CodexAdapter(Adapter):
    name = "codex"

    def _meta(self, path: Path) -> dict:
        with path.open() as fh:
            first = fh.readline()
        try:
            d = json.loads(first)
            return d["payload"] if d.get("type") == "session_meta" else {}
        except (json.JSONDecodeError, KeyError):
            return {}

    def locate(self, cwd: str, session_id: str | None = None) -> Path:
        if session_id:
            hits = list(SESSIONS.glob(f"**/rollout-*{session_id}*.jsonl"))
            if not hits:
                raise SystemExit(f"no Codex session {session_id} under {SESSIONS}")
            return hits[0]
        best = None  # newest rollout whose session_meta.cwd matches
        for p in SESSIONS.glob("**/rollout-*.jsonl"):
            m = self._meta(p)
            if m.get("cwd") == cwd:
                ts = m.get("timestamp", "")
                if best is None or ts > best[0]:
                    best = (ts, p)
        if best is None:
            raise SystemExit(f"no Codex sessions found for cwd {cwd}")
        return best[1]

    def _title(self, sid: str) -> str:
        if not INDEX.exists():
            return ""
        for line in INDEX.read_text().splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("id") == sid:
                return d.get("thread_name", "")
        return ""

    def read(self, path: Path) -> Session:
        meta = self._meta(path)
        sid = meta.get("id", path.stem)
        cwd = meta.get("cwd", "")
        git = (meta.get("git") or {}).get("branch", "")
        started = meta.get("timestamp", "")
        records = []
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") != "response_item":
                continue
            p = d["payload"]
            pt = p.get("type")
            ts = d.get("timestamp", "")
            if pt == "reasoning":
                continue
            if pt == "message":
                role = p.get("role")
                if role == "developer":
                    continue
                text = "".join(c.get("text", "") for c in p.get("content", [])
                               if c.get("type") in ("input_text", "output_text", "text"))
                if not text.strip():
                    continue
                records.append((AssistantMessage if role == "assistant" else UserMessage)(text, ts))
            elif pt in ("function_call", "custom_tool_call"):
                raw = p.get("arguments", p.get("input", ""))
                try:
                    inp = json.loads(raw) if isinstance(raw, str) else (raw or {})
                except json.JSONDecodeError:
                    inp = {"raw": raw}
                records.append(ToolCall(p["call_id"], p.get("name", "tool"),
                                        inp if isinstance(inp, dict) else {"input": inp}, ts))
            elif pt in ("function_call_output", "custom_tool_call_output"):
                o = p.get("output", "")
                records.append(ToolResult(p["call_id"],
                                          o if isinstance(o, str) else json.dumps(o), ts))
        s = Session("codex", sid, cwd, records, git, started)
        title = self._title(sid)
        if title:
            s.extra["title"] = title
        return s

    def dest_path(self, session: Session, dest_cwd: str) -> Path:
        ts = session.started_at or "1970-01-01T00:00:00Z"
        date, _, rest = ts.partition("T")
        hms = rest.split(".")[0].replace(":", "-") or "00-00-00"
        y, m, d = date.split("-")
        return SESSIONS / y / m / d / f"rollout-{date}T{hms}-{session.session_id}.jsonl"

    def write(self, session: Session, dest_cwd: str) -> Path:
        out = []
        calls = {r.call_id: r for r in session.records if isinstance(r, ToolCall)}

        def ri(payload, ts):
            out.append({"timestamp": ts, "type": "response_item", "payload": payload})

        def ev(payload, ts):
            out.append({"timestamp": ts, "type": "event_msg", "payload": payload})

        def tool_card(call: ToolCall, result: ToolResult, ts):
            """The event_msg the TUI renders a tool card from (only _end exists)."""
            name = INBOUND_NAMES.get(call.name, call.name)
            cid = _call(call.call_id)
            if name in PATCH_NAMES:
                ev({"type": "patch_apply_end", "call_id": cid, "stdout": result.output,
                    "stderr": "", "success": not result.is_error}, ts)
            elif name in SHELL_NAMES:
                cmd = call.input.get("command") or call.input.get("cmd") or ""
                ev({"type": "exec_command_end", "call_id": cid,
                    "command": ["/bin/zsh", "-lc", cmd] if isinstance(cmd, str) else cmd,
                    "cwd": dest_cwd, "stdout": result.output, "stderr": "",
                    "aggregated_output": result.output,
                    "exit_code": 1 if result.is_error else 0,
                    "status": "failed" if result.is_error else "completed"}, ts)
            # other tools (Read, MCP, ...) -> function_call_output carries them; no card

        for r in session.records:
            ts = getattr(r, "ts", "") or session.started_at
            if isinstance(r, UserMessage):
                ri({"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": r.text}]}, ts)
                ev({"type": "user_message", "message": r.text}, ts)
            elif isinstance(r, AssistantMessage):
                ri({"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": r.text}]}, ts)
                ev({"type": "agent_message", "message": r.text}, ts)
            elif isinstance(r, ToolCall):
                ri({"type": "function_call", "name": INBOUND_NAMES.get(r.name, r.name),
                    "arguments": json.dumps(r.input), "call_id": _call(r.call_id)}, ts)
            elif isinstance(r, ToolResult):
                ri({"type": "function_call_output", "call_id": _call(r.call_id),
                    "output": r.output}, ts)
                if r.call_id in calls:
                    tool_card(calls[r.call_id], r, ts)

        meta = {"timestamp": session.started_at, "type": "session_meta",
                "payload": {"id": session.session_id, "session_id": session.session_id,
                            "timestamp": session.started_at,
                            "cwd": dest_cwd, "originator": "codex-cli",
                            "cli_version": CLI_VERSION, "instructions": None,
                            "source": "cli", "thread_source": "user",
                            "model_provider": MODEL_PROVIDER,
                            **({"git": {"branch": session.git_branch}} if session.git_branch else {})}}
        dest = self.dest_path(session, dest_cwd)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("".join(json.dumps(l) + "\n" for l in [meta] + out))

        # N^2 surplus: a session title goes in the index the picker reads.
        name = session.extra.get("out", {}).get("thread_name")
        if name:
            self._index_put(session.session_id, name, session.started_at)
        return dest

    def _index_put(self, sid: str, thread_name: str, updated_at: str) -> None:
        rows = []
        if INDEX.exists():
            rows = [l for l in INDEX.read_text().splitlines()
                    if l.strip() and json.loads(l).get("id") != sid]
        rows.append(json.dumps({"id": sid, "thread_name": thread_name,
                                "updated_at": updated_at}))
        INDEX.parent.mkdir(parents=True, exist_ok=True)
        INDEX.write_text("\n".join(rows) + "\n")


register(CodexAdapter())
