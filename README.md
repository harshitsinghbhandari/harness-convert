# harness-convert (`hc`)

Relocate a coding-agent session across harnesses and resume it natively.

**The escape hatch:** you're 80% through a fix, your harness hits a rate limit /
outage mid-task, and you can't even ask it for a handoff. `hc` reads the session
transcript off disk (the dead harness doesn't need to be running or your quota
intact), rewrites it into the target harness's format, and you keep going there.

```bash
hc                                          # interactive wizard (TTY): pick from/session/to
hc --from claude --to codex                 # dry-run latest; TTY asks before write
hc --from claude --to codex -y              # write without prompting
hc --from codex  --to claude <session-id>   # a specific session
hc --from claude --to codex --dest-cwd DIR  # land it in a different folder
hc list --from claude -n 5                  # newest 5; TTY: pick one and convert
hc list --from claude --no-interactive      # plain table (also when piped)
hc truncate 20 --from claude                # new session, 20% lighter
hc truncate 20 --from claude <session-id>   # a specific one
```

By default it's a dry run. Pass `--write` or `-y` to create the file (or confirm
when prompted on a TTY). It then prints the exact resume command. Flags always
win; missing pieces prompt only on an interactive TTY. Stdlib only; set
`HC_NO_INTERACTIVE=1` to force non-interactive mode.

## How it works

A session is **(a)** a model-context stream, **(b)** a UI-render stream, and
**(c)** identity metadata. Conversion maps all three.

- **Common interface** (`hconv/common.py`): every harness maps to four records:
  `UserMessage`, `AssistantMessage`, `ToolCall`, `ToolResult`. This universal floor
  guarantees any pair converts and resumes. Private reasoning is dropped (each
  harness encrypts/owns its own; unrecoverable).
- **N² enrichment** (`hconv/enrich.py`): surplus the floor can't hold (session
  titles, ...) rides a sparse `(source, dest)` map, layered on top. A pair with no
  entry is simply common-only. The map never re-encodes the common records.
- **Adapters** (`hconv/adapters/`): one per harness, `locate / read / dest_path /
  write`. Codex's writer emits BOTH streams (`response_item` for the model,
  `event_msg` for scrollback incl. `exec_command_end` / `patch_apply_end` tool
  cards); Claude's single row set serves both. OpenCode is SQLite, not JSONL: it
  reads the `session`/`message`/`part` tables read-only, and writes the canonical
  `{info, messages}` file that `opencode import` validates and ingests (safer than
  poking a live WAL DB), so `opencode -s <id>` resumes it. Grok Build stores a
  session directory (`summary.json` + `chat_history.jsonl` + `updates.jsonl`
  under `~/.grok/sessions/`); write emits all three so `grok --resume <id>`
  loads history. Cursor is a content-addressed protobuf blob tree inside SQLite;
  the adapter walks it from the current root, and is read-only
  (`writable = False`, enforced at the CLI).
- **Ragged-tail close** (`synthesize_missing_results`): the source usually died
  mid-tool-call, so every orphan `ToolCall` gets a synthetic result, else the
  resumed API call rejects the history.

## Freeing context (`hc truncate`)

A long session is mostly dead tool payload. `hc truncate 20` clips the heaviest
payloads until 20% of the session is gone and writes a **new** session in the
same harness; the original is never touched, so a bad trim costs you nothing.

```bash
hc truncate 20 --from claude       # dry run, shows the cap and what it frees
hc truncate 20 --from claude -y    # write it, then resume the new id
```

What it clips, and why that shape: measuring the 40 largest sessions per
harness showed payload concentration is extreme, so it clips the **biggest**
payloads, not the oldest. On Codex, 41 records over 200KB are 45.9% of all
payload, and the *newest* decile holds the most tool output, so an oldest-first
rule would free almost nothing. It pools tool **inputs** alongside outputs
(`Bash.command`, `Write.content`, `Edit.new_string`) because inputs are 37% of
a Claude session; outputs alone cannot pass ~44% freed. Conversation text is
never clipped. `view_image` payloads are dropped outright rather than
head-clipped, since 4KB of base64 is bounded junk rather than information.

The new id is `uuid5("harness-convert:truncate:<orig>:<pct>")`: deterministic,
so re-running the same trim upserts one session instead of piling up copies.
Cursor is read-only and cannot be truncated. Design notes and the full
measurements: `docs/superpowers/specs/2026-08-06-truncate-subcommand-design.md`.

## Install

```bash
pipx install harness-convert                            # PyPI
npm i -g @theharshitsingh/hc                            # npm (needs python3 on PATH)
brew install harshitsinghbhandari/tap/harness-convert   # Homebrew
```

Stdlib only, no dependencies. From a checkout, `pipx install .` or plain
`python3 hc.py ...` also work.

## Supported

Codex (`~/.codex`), Claude Code (`~/.claude`), OpenCode
(`~/.local/share/opencode`), and Grok Build (`~/.grok`, or `$GROK_HOME`): any
direction between the writable ones. Converting *into* OpenCode writes an import
file; resume with `opencode import <file> && opencode -s <id>` (the command `hc`
prints). Converting *into* Grok writes a session directory; resume with
`grok --resume <id>`. Within a harness, sessions are also freely relocatable
across working directories (pure metadata rewrite, lossless, title included).

Cursor (`~/.cursor/chats`) is a **source only**: `--from cursor` works, `--to
cursor` is rejected. `cursor-agent` has no import command, so writing a session
would mean authoring its undocumented protobuf blob tree with nothing to
validate the result against. Reading is full fidelity, tool outputs included.
See `docs/cursor-format.md`. Grok on-disk shapes: `docs/grok-format.md`.

## Test

```bash
python3 tests/test_hconv.py
```
