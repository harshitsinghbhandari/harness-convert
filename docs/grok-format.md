# How Grok Build stores sessions on disk

Reverse-engineered for `hc`'s `grok` adapter. Official overview lives in
`~/.grok/docs/user-guide/17-sessions.md`; this file pins the shapes `hc` reads
and writes.

**Provenance.** Inspected on 2026-08-03 against local sessions under
`~/.grok/sessions/` and a successful forge-resume of a three-file minimal
session via `grok -r <uuid>`.

---

## 1. Layout

```
$GROK_HOME/sessions/<urlencode(cwd, safe="")>/<session-uuid>/
    summary.json           # index entry: title, timestamps, model, counts
    chat_history.jsonl     # model context stream
    updates.jsonl          # ACP session/update stream (UI + restore)
    events.jsonl           # telemetry (not required for resume)
    system_prompt.txt      # regenerated; not required for resume
    ...                    # optional: signals, rewind, terminal/, etc.
```

`GROK_HOME` defaults to `~/.grok`. Cwd directories are percent-encoded path
strings (`/Users/x/proj` → `%2FUsers%2Fx%2Fproj`). When the encoded name would
exceed 255 bytes, Grok uses a slug+hash and records the real path in a `.cwd`
file inside the group dir; `hc` still matches via `summary.json` `info.cwd`.

Session IDs are UUIDs (Grok mints UUIDv7; any valid UUID resumes).

---

## 2. `summary.json` (verified)

```json
{
  "info": {"id": "<uuid>", "cwd": "/abs/path"},
  "session_summary": "...",
  "generated_title": "...",
  "created_at": "2026-08-03T15:02:33.639622Z",
  "updated_at": "2026-08-03T15:04:14.338886Z",
  "last_active_at": "2026-08-03T15:04:14.338886Z",
  "num_messages": 118,
  "num_chat_messages": 51,
  "current_model_id": "grok-4.5",
  "chat_format_version": 1,
  "agent_name": "grok-build-plan",
  "sandbox_profile": "off",
  "reasoning_effort": "high"
}
```

Optional git fields (`git_root_dir`, `head_branch`, …) appear on real sessions
but are not required for resume.

---

## 3. `chat_history.jsonl` (verified)

One JSON object per line. Types `hc` cares about:

| type | shape |
|---|---|
| `system` | `{"type":"system","content":"..."}` (dropped on read) |
| `user` | `{"type":"user","content":[{"type":"text","text":"..."}], "prompt_index"?: N}` |
| `assistant` | `{"type":"assistant","content":"<string>", "tool_calls"?: [...], "model_id"?: "..."}` |
| `tool_result` | `{"type":"tool_result","tool_call_id":"...","content":"<string>"}` |
| `reasoning` | encrypted; dropped |
| `backend_tool_call` | dropped in v1 |

Human prompts are usually wrapped in `<user_query>...</user_query>`. Scaffolding
turns (`<user_info>`, `<system-reminder>`, skill lists) are bare user rows
without that tag; `hc` drops them on read.

`tool_calls` entries:

```json
{"id":"call-...","name":"run_terminal_command","arguments":"{\"command\":\"...\"}"}
```

`arguments` is a JSON **string**. Tool error results have no `is_error` flag;
content typically starts with `Error:`.

---

## 4. `updates.jsonl` (verified)

Authoritative UI/restore stream per docs. Each line:

```json
{
  "timestamp": 1785769357,
  "method": "session/update",
  "params": {
    "sessionId": "<uuid>",
    "update": {
      "sessionUpdate": "user_message_chunk|agent_message_chunk|tool_call|tool_call_update|...",
      "...": "..."
    },
    "_meta": {"eventId": "<uuid>-N", "agentTimestampMs": 1785769355875}
  }
}
```

Live sessions stream many `tool_call_update`s (pending → in_progress →
completed). `hc` writes coalesced completed events only.

---

## 5. Minimal resume set (verified by forge)

Writing only `summary.json` + `chat_history.jsonl` + `updates.jsonl` is enough
for `grok --resume <uuid>` to load history and continue. The local search index
(`session_search.sqlite`) is not required for resume-by-id; the session picker
may lag until Grok reindexes.
