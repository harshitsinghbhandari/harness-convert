# harness-convert (`hc`)

Relocate a coding-agent session across harnesses and resume it natively.

**The escape hatch:** you're 80% through a fix, your harness hits a rate limit /
outage mid-task, and you can't even ask it for a handoff. `hc` reads the session
transcript off disk (the dead harness doesn't need to be running or your quota
intact), rewrites it into the target harness's format, and you keep going there.

```bash
hc --from claude --to codex                 # move the latest Claude session here -> Codex
hc --from codex  --to claude <session-id>   # a specific session
hc --from claude --to codex --dest-cwd DIR  # land it in a different folder
hc list --from codex                        # what's the latest convertible session here
```

By default it's a dry run; pass `--write` to create the file, then it prints the
exact `codex resume` / `claude --resume` command.

## How it works

A session is **(a)** a model-context stream, **(b)** a UI-render stream, and
**(c)** identity metadata. Conversion maps all three.

- **Common interface** (`hconv/common.py`): every harness maps to four records —
  `UserMessage`, `AssistantMessage`, `ToolCall`, `ToolResult`. This universal floor
  guarantees any pair converts and resumes. Private reasoning is dropped (each
  harness encrypts/owns its own; unrecoverable).
- **N² enrichment** (`hconv/enrich.py`): surplus the floor can't hold (session
  titles, ...) rides a sparse `(source, dest)` map, layered on top. A pair with no
  entry is simply common-only. The map never re-encodes the common records.
- **Adapters** (`hconv/adapters/`): one per harness, `locate / read / dest_path /
  write`. Codex's writer emits BOTH streams (`response_item` for the model,
  `event_msg` for scrollback incl. `exec_command_end` / `patch_apply_end` tool
  cards); Claude's single row set serves both.
- **Ragged-tail close** (`synthesize_missing_results`): the source usually died
  mid-tool-call, so every orphan `ToolCall` gets a synthetic result — else the
  resumed API call rejects the history.

## Install

```bash
pipx install .     # or: pip install -e .
```

Stdlib only, no dependencies. `python3 hc.py ...` also works without installing.

## Supported

Codex (`~/.codex`) ↔ Claude Code (`~/.claude`), both directions. Within a harness,
sessions are also freely relocatable across working directories (pure metadata
rewrite, lossless).

## Test

```bash
python3 tests/test_hconv.py
```
