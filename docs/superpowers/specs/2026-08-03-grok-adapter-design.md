# Grok adapter for harness-convert

Date: 2026-08-03
Status: approved (design greenlit in-session; forge resume validated)

## Motivation

`hc` already relocates sessions across Claude, Codex, OpenCode, and Cursor
(read-only). Grok Build is a first-class coding harness with on-disk sessions
under `~/.grok/sessions/`. Escape-hatch parity means full read/write:

```bash
hc --from claude --to grok --write   # then: grok --resume <id>
hc --from grok  --to codex --write
```

## Validation already done

A minimal forged session with only:

- `summary.json`
- `chat_history.jsonl` (user + assistant)
- `updates.jsonl` (user_message_chunk + agent_message_chunk)

resumed successfully via `grok -p ... -r <uuid>` and the model saw the prior
user turn. So write path is viable (not Cursor-style read-only).

## Storage layout

```
$GROK_HOME/sessions/<urlencode(cwd, safe="")>/<session-uuid>/
  summary.json
  chat_history.jsonl
  updates.jsonl
```

- Default home: `~/.grok`. Honor `GROK_HOME` when set.
- Cwd encoding: `urllib.parse.quote(cwd, safe="")`. Long-path slug+hash + `.cwd`
  is rare; locate prefers matching `summary.json` `info.cwd` over directory name.
- Session IDs must be UUIDs. Non-UUID source ids map to deterministic
  `uuid5(NAMESPACE_URL, "harness-convert:grok:" + source_id)`.

## Pipeline

Same as every other adapter:

```
locate → read → synthesize_missing_results → enrich → write
```

### Read

Source of truth for conversation: `chat_history.jsonl`.
Identity/title: `summary.json`.

| Grok row | Common record |
|---|---|
| `user` with `<user_query>` | `UserMessage` (extracted text) |
| `user` scaffolding only | dropped |
| bare `user` text (no tags) | `UserMessage` (full text) |
| `assistant` text | `AssistantMessage` |
| `assistant.tool_calls[]` | `ToolCall` each |
| `tool_result` | `ToolResult` (`is_error` if content starts with `Error:`) |
| `reasoning` / `system` / `backend_tool_call` | dropped |

Outbound tool names (cosmetic middle other writers already understand):

- `run_terminal_command` → `Bash`
- `read_file` → `Read`
- `search_replace` → `Edit`
- `write` → `Write`

### Write

Emit three files. No system prompt (Grok regenerates on load; forge resume worked without it).

1. **`chat_history.jsonl`**: user rows wrapped in `<user_query>`; assistant
   text + optional `tool_calls`; separate `tool_result` rows. Tool names remapped
   inbound (`Bash`/`shell` → `run_terminal_command`, etc.).
2. **`updates.jsonl`**: coalesced ACP events (not token-streamed):
   `user_message_chunk`, `agent_message_chunk`, `tool_call`, completed
   `tool_call_update` with content/rawOutput.
3. **`summary.json`**: `info.{id,cwd}`, timestamps, title fields, message
   counts, `current_model_id: "grok-4.5"`, `chat_format_version: 1`,
   `agent_name: "grok-build-plan"`.

Resume hint: `grok --resume <sid>`.

### Enrichment

Carry titles via the existing N² map:

- `* → grok`: `out["grok_title"]`
- `grok → claude`: `out["ai_title"]`
- `grok → codex`: `out["thread_name"]`
- `grok → opencode`: `out["opencode_title"]`

## Files

- `hconv/adapters/grok.py` (new)
- `hconv/adapters/__init__.py`
- `hconv/enrich.py`
- `hconv/cli.py`
- `tests/test_hconv.py`
- `README.md`
- `docs/grok-format.md` (short reverse-eng notes)

## Out of scope (v1)

- Subagent dirs, rewind points, terminal logs, hunk records
- Writing `session_search.sqlite` (resume-by-id works; picker may lag)
- Live ACP `session/import` (no public CLI)

## Success criteria

1. Hermetic tests: write invariants, round-trip text, locate-by-cwd, title enrich.
2. `python3 tests/test_hconv.py` all pass.
3. Manual: `hc --from claude --to grok --write` then `grok --resume <id>` loads history.
