# How Cursor Agent CLI stores sessions on disk

Reverse-engineered for `hc`'s `cursor` adapter. Cursor ships no format
documentation and no `.proto`, so this file IS the spec. When the adapter breaks
after a Cursor upgrade, start here.

**Provenance.** Read off a real machine on 2026-08-03. Sessions inspected span
2026-04-09 to 2026-08-03, written by CLI builds `2026.04.08-a41fba1`,
`2026.05.09-0afadcc` and `2026.06.04-5fd875e`. Facts below are marked
**(verified)** where observed directly and **(inferred)** where reasoned from
structure.

---

## 1. Layout

```
~/.cursor/chats/<md5(abs_cwd)>/<session-uuid>/
    meta.json          identity; present ONLY on real user sessions
    store.db           SQLite; the actual transcript
    store.db-wal       often large and uncheckpointed
    prompt_history.json  the user's typed prompts, newest first
~/.cursor/projects/<encoded-cwd>/agent-transcripts/<uuid>/<uuid>.jsonl
```

`md5(abs_cwd)` is plain `hashlib.md5` over the UTF-8 path bytes, no trailing
slash **(verified** on four distinct project directories**)**.

The `projects/` directory name uses a different encoding: non-alphanumerics to
`-`, and long paths are truncated and suffixed with a 7-hex hash. The exact
truncation rule was **not** derived, and `hc` deliberately never needs it,
because `meta.json` carries `cwd` outright.

### Subagent sessions

A `Task` tool call spawns a **separate session** with its own directory and its
own `store.db`, but **no `meta.json`** **(verified)**. That absence is the
discriminator `hc` uses to exclude subagent transcripts, the same role
`isSidechain` plays in the Claude adapter.

---

## 2. `meta.json`

```json
{"schemaVersion":1,
 "createdAtMs":1785695807732,
 "updatedAtMs":1785695950093,
 "hasConversation":true,
 "title":"Hello There",
 "cwd":"/Users/.../harness-convert"}
```

This file is **new**: sessions from April through June 2026 do not have it.
`hc` skips sessions without it rather than supporting the older shape.

`schemaVersion` is the single most useful field in the whole format. It is the
only explicit version marker any supported harness exposes, so assert on it and
fail loudly when it moves.

---

## 3. `store.db`

```sql
CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);  -- id = sha256(data) hex
CREATE TABLE meta  (key TEXT PRIMARY KEY, value TEXT);
```

The single `meta` row's `value` is **hex-encoded JSON** (`binascii.unhexlify`
then `json.loads`), carrying `agentId`, `latestRootBlobId`, `name`, `mode`,
`createdAt`, `isRunEverything`. Older sessions also had `lastUsedModel`.

Content-addressed storage means **superseded revisions stay in the table**.
Never iterate `blobs` directly: you get stale duplicates in arbitrary order.
Always walk from `latestRootBlobId`.

A live or recently-active session may have a 4 KB `store.db` against a 1 MB
uncheckpointed WAL. Opening `file:...?mode=ro` reads the WAL correctly
**(verified** on a session with exactly that shape**)**.

---

## 4. Framing: two parallel trees

The single most important fact, and the one that costs a day if you miss it:
**only the root blob is protobuf you must decode. The conversation itself is
plain JSON.**

The root holds TWO independent branches of 32-byte sha256 child refs:

| root field | branch | encoding |
|---|---|---|
| `1` | the conversation | **raw JSON**, one message per blob |
| `8` | a UI-event tree | protobuf |

Both describe the same conversation. Field 8 is the harder rendering of data you
get for free as JSON from field 1, so `hc` reads field 1 and ignores field 8
entirely.

### Decoding the root

A generic wire walk suffices: varint key, `field_number = key >> 3`,
`wire_type = key & 7` (2 length-delimited, 0 varint, 1 fixed64, 5 fixed32).
Collect the ordered 32-byte payloads. That order IS conversation order
**(verified:** every child resolves in `blobs`**)**.

### The conversation branch (field 1)

Each child blob is a JSON document starting at byte 0, so detection is simply
"does `json.loads` succeed". Across all 9 sessions on the reference machine:
**126 field-1 children, 126 JSON, 0 protobuf, 0 missing (verified)**.

Shape is Vercel AI SDK, not Anthropic:

```json
{"role":"user","content":[{"type":"text","text":"..."}]}
```

| block | keys | hc treatment |
|---|---|---|
| `text` | `text` | User/AssistantMessage |
| `reasoning` | `text`, `signature`, `providerOptions` | dropped |
| `tool-call` | `toolCallId`, `toolName`, `args` | ToolCall |
| `tool-result` | (role `"tool"`) | ToolResult, `json.dumps`'d when a dict |

Two records are NOT conversation and must be skipped: the `system` role blob,
and a `<user_info>` environment preamble. The reliable discriminator is that
both carry `content` as a **bare string**, whereas every real turn carries a
**block list (verified)**. Real user turns additionally arrive wrapped in
`<user_query>...</user_query>`, which is unwrapped.

### The UI-event branch (field 8), documented but unused

Recorded so nobody re-derives it. Top-level field `2` is a tool invocation whose
sub-field number selects the tool: `1` shell, `5` grep, `8` read, `15` MCP call,
`44` MCP discovery. Within it, sub-field `1` is the request and `2` the
response (shell command at `[2,1,1,1]`, shell stdout at `[2,1,2,1,5]`, read path
at `[2,8,1,1]`, grep pattern at `[2,5,1,1]`). Field `57` carries the call id.
User text sits bare at field 1; assistant prose sits at field 1 nested one level
deeper.

### Tool call ids

The composite two-line string `"call-<uuid>-<n>\nfc_<uuid>_<n>"`, appearing both
as `toolCallId` in the JSON and at field `57` in the event tree. Results pair to
calls **by id, never positionally (verified:** a 5-call session with a parallel
Glob+Read batch paired 5/5 with 0 orphans**)**. This is what makes parallel tool
calls safe to import.

### What Cursor does not store

- **No error flag.** Nothing in the store marks a tool result as failed. `hc`
  infers it from an `"Error:"` prefix; a rejected call reading `"Rejected: "`
  therefore imports as a success. Named in a `ponytail:` comment.
- **No per-message timestamps.** Every record inherits the session's
  `createdAtMs`. The field-8 tree is the upgrade path if this ever matters.
- **No title on headless sessions.** `meta.json` written by `cursor-agent -p`
  omits `title` entirely, so treat it as optional.

---

## 5. Tool vocabulary

Cursor's names, as seen in transcripts: `Shell`, `Read`, `Grep`, `Glob`, `Task`,
`GetMcpTools`, `CallMcpTool`. MCP calls are wrapped in the generic
`CallMcpTool` rather than surfacing the underlying server tool name, so they
pass through unmapped.

---

## 6. Why the adapter is read-only

`cursor-agent` has `--resume [chatId]` but **no import subcommand**. Writing a
session would mean authoring the protobuf Merkle tree, sha256-addressing every
blob and rewriting `meta.latestRootBlobId`, with no validator to check the
result: unlike `opencode import`, nothing tells you when you got it wrong except
launching the TUI and looking.

`create-chat` ("Create a new empty chat and return its ID") would supply a valid
registered container, so the write path is not impossible, just unjustified
until someone asks for it.

---

## 7. Drift notes

Across four CLI builds in four months, the **container** was completely stable:
SQLite schema, hex-JSON `meta`, sha256 addressing, `latestRootBlobId`. What
moved was the inner layout: one structural change between April and May, one
cosmetic relabel between May and June, and the addition of `meta.json` in
`2026.06.04`. Cursor ships roughly monthly, so expect the inner framing to drift
and the container not to.

**Adapter validated across a version boundary (verified 2026-08-03).** The
decode was derived from sessions written by `2026.06.04-5fd875e`, then tested
against a session freshly spawned by `2026.07.23-e383d2b`: `schemaVersion` still
1, identical framing, 12 records recovered with 5/5 tool calls paired and real
tool output intact. One monthly release crossed with zero breakage.

Re-run that check the cheap way after any Cursor upgrade:

```bash
cursor-agent -p --trust "list the files here" <in a scratch dir>
python3 -c "from hconv import get; a=get('cursor'); print(a.read(a.locate('<scratch dir>')).records)"
```

When it breaks, in order: check `schemaVersion`, confirm the root blob still
lists 32-byte child refs at field 1, then re-derive the tool sub-field numbers.

The cheapest re-derivation trick, and the one that made this document possible:
`prompt_history.json` lists the user's prompts verbatim and the
`agent-transcripts/*.jsonl` sidecar lists tool names and assistant text, so any
session gives you known-good strings to search for in the decoded blobs. That
turns re-mapping from guesswork into a diff.

---

## 8. The jsonl sidecar, and why it is not the source

`projects/<encoded-cwd>/agent-transcripts/<uuid>/<uuid>.jsonl` looks like the
obvious read path and is a trap. Census of a 22-line session **(verified)**:

```
text        23
tool_use    21
tool_result  0      <- never present
```

and `tool_use` blocks carry `{type, name, input}` with **no `id`**. So the
sidecar has no tool outputs and no way to pair them. Sourcing from it would
close all 21 calls with synthetic errors and tell the resumed model that 21
commands which actually succeeded had failed. It is useful only as ground truth
for validating the blob decode.
