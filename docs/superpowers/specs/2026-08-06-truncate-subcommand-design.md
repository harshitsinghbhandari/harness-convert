# `hc truncate`: shrink a session to free context

Date: 2026-08-06
Status: approved (design greenlit in-session; policy derived from on-disk measurement)

## Motivation

The escape hatch works but lands you in a new harness carrying the old
session's entire weight. A long session is mostly tool payload, and most of
that payload is dead: a 2MB base64 screenshot from turn 6, a `rg` dump you
already extracted one line from. You want to keep going with the *narrative*
intact and the *payloads* gone.

```bash
hc truncate --from claude 20   # new session, 20% lighter, original untouched
```

## Measurement first

Design policy was derived from the 40 largest sessions per harness, read
through `hconv`'s own adapters so every byte counted is a byte the common
record stream would carry into the destination.

| | Claude (18.5MB) | Codex (77.8MB) |
|---|---|---|
| `tool_output` | 44% | 89% |
| `tool_input` | 37% | 8% |
| assistant text | 11% | 2% |
| user text | 8% | 1% |
| biggest 1% of results | 22% of output | 56% of output |
| largest single record | 65KB | 2.0MB |

Three findings, each of which killed a plausible design:

1. **Concentration is extreme, so oldest-first loses.** On Codex, 41 records
   over 200KB are 45.9% of the entire payload; a 20% target costs 23 records
   out of 20,922 (0.1%). Oldest-first would be near useless there: Codex's
   *newest* decile holds the most tool output (16.7%) and the oldest half holds
   only 47%. Claude leans the other way (oldest half = 71%), so no
   position-based rule generalizes. Size-based does.

2. **`tool_input` is 37% of Claude's payload and is not metadata.**
   `Bash.command` 35%, `Write.content` 25%, `Agent.prompt` 11%,
   `Edit.new_string`/`old_string` 14%. Outputs-only caps out around 44% freed
   on Claude and cannot reach 50% at any setting. Including inputs is also
   *gentler* at a fixed target, not harsher:

   ```
   claude, 20% target:  outputs only    cap 2.1KB   clips 874 payloads
                        outputs+inputs  cap 3.9KB   clips 779 payloads
   ```

   Nearly double the cap, fewer records damaged. On Codex inputs are 8% of
   payload, so including them costs nothing.

3. **A percentage is the right knob, a byte cap is the right mechanism.** A
   flat per-record cap is sharply nonlinear in our favour, and "free 20%" is
   just a search for the cap that achieves it.

Cap required to hit a target (outputs + inputs pooled):

| target | claude | codex |
|---|---|---|
| 10% | 8.1KB | 995.6KB |
| 20% | 3.9KB | 594.2KB |
| 30% | 2.2KB | 316.4KB |
| 40% | 1.2KB | 117.2KB |
| 50% | 710B | 22.0KB |

## Behaviour

`hc truncate <pct>` reads a session, clips its heaviest payloads until `pct`
of total payload is freed, and writes a **new session in the same harness**.

### Selection

1. Pool every trimmable payload as a `(get, set)` pair:
   - each `ToolResult.output`
   - each `ToolCall.input`'s single largest string-valued field, chosen by
     size, not by name (so `command` / `content` / `new_string` / `raw` /
     `prompt` are all covered without a name table)

   `ponytail:` only top-level string values are poolable, so a structured
   input like `AskUserQuestion.questions` (a list, 100KB in the sample) or
   `update_plan.plan` is never clipped. Ceiling: ~1.5% of Claude payload,
   ~0.3% of Codex. Upgrade path if it ever matters: recurse into the input and
   pool the largest leaf string.
2. Binary-search the **largest** cap where clipping everything above it frees
   `>= pct` of total payload. Freed shrinks as the cap grows, so every cap in
   `[1, threshold]` meets the target and the largest one is the gentlest. An
   earlier draft of this spec said "smallest", which would have selected a
   1-byte cap and destroyed every payload in the session.
3. Apply the cap.

Total payload = user text + assistant text + tool inputs + tool outputs, so
the percentage is of the whole session, not of the trimmable subset. A user
asking to free 20% means 20% of the thing they are carrying.

### Clipping

Clip, never drop. Keep the first `cap` bytes plus a marker:

```
... [hc truncated 1,984,221 bytes]
```

Consequences that fall out of clipping rather than stubbing: no record ever
disappears, so the ragged-tail invariant from `synthesize_missing_results`
still holds untouched, and no "never trim the final result" guard is needed.
Every payload keeps its opening bytes, which for a tool result is the part
that carries the exit status and the first hits.

Inputs are clipped **field-level, never flattened**. The dict stays a dict, so
`codex`'s `json.dumps(r.input)` and `claude`'s verbatim `tool_use.input` both
still receive well-formed input.

Byte-count clipping is UTF-8 safe: clip at `cap` bytes then back off to the
last valid character boundary.

### Images

An image payload **that the cap selects** (i.e. one larger than the cap) is
replaced outright rather than clipped, because 4KB of head-clipped base64 is
bounded junk rather than information. An image already under the cap is left
alone, so `hc truncate 1` does not nuke every image in the session to satisfy
a 1% target. Matched by tool name (`view_image`) or an output starting with
`data:image/`:

```
[image, 1.9MB, dropped by hc]
```

This feeds back into step 2: when scoring a candidate cap, an image payload
contributes `size - len(marker)` to the freed total, not `size - cap`.

The search scores candidate caps arithmetically from precomputed sizes, never
by building clipped strings, so a 21-probe search over 20k payloads does no
string work. That estimate is exact except for UTF-8 backoff, which costs at
most 3 bytes per clipped payload. Reported `freed` is therefore accumulated
during the **apply** pass, not taken from the search, so the number printed is
always the number that landed on disk.

This matters: `view_image` alone is 34.7MB across 48 calls, 50% of all Codex
tool output in the sample.

### Shortfall reporting

If the target is unreachable (a session that is mostly conversation text), trim
what is reachable and say so:

```
freed 12.3% (target 20% unreachable: 88% of payload is conversation)
```

Never silently under-deliver.

## Identity: a new session, never a rewrite

The destination id is deterministic:

```python
uuid5(NAMESPACE_URL, f"harness-convert:truncate:{orig_id}:{pct}")
```

Deterministic so re-running the same truncate upserts one destination instead
of piling up copies; new so the original is never overwritten. This follows
the precedent already set by `grok._dest_id` and `opencode._id`.

The original is never read-modify-written. If a trim comes out too aggressive,
resume the original and pick a different percent.

Per-harness id handling then works unchanged: Grok's `_dest_id` keeps a valid
UUID, OpenCode's `_id` maps it to `ses_<sha1>`, Codex derives a fresh
`rollout-<ts>-<id>.jsonl` filename from it, Claude uses it as the file stem.
Cursor is refused by the existing `writable()` check.

## CLI

```bash
hc truncate --from claude 20                 # newest session in cwd, free 20%
hc truncate --from claude 20 <session-id>    # a specific one
hc truncate --from claude 20 -y              # write without prompting
hc truncate --from claude 20 --dest-cwd DIR  # land it elsewhere
hc truncate 20                               # TTY: pick harness, then session
```

`pct` is a positional int in 1..99; argparse rejects the rest. Dry-run by
default, `--write`/`-y` to commit, same as `convert`. `hc convert` gains **no**
`--truncate` flag: the two compose as separate commands, since truncate's
output is an ordinary session.

`main()`'s argv munging must learn `truncate` (the current
`if sys.argv[1] not in ("convert", "list", "-h", "--help")` inserts `convert`
and would otherwise swallow it).

Preview:

```
from     claude  e1e8eb19-...
trim     20% of 18.5MB payload
cap      3.9KB   (clips 779 of 15,765 payloads)
freed    3.7MB   18.5MB -> 14.8MB
new      f4a1c2d0-...  (original untouched)
dest     ~/.claude/projects/-Users-.../f4a1c2d0-....jsonl
```

## Code layout

`hconv/common.py`, beside `synthesize_missing_results`, since both are pure
transforms on the common record stream:

```python
def truncate_payload(records, pct) -> tuple[list[Record], TrimStats]
```

`TrimStats` carries `total`, `freed`, `cap`, `clipped`, `pooled` so the CLI
preview and the shortfall message read off one object rather than recomputing.

`hconv/adapter.py`: `convert()` grows `truncate: int = 0` and `new_id: bool =
False`, applied after `synthesize_missing_results` (so pairing invariants
already hold) and before `enrich` (so enrichers see final content).

`hconv/cli.py`: a `cmd_truncate` reusing `_resolve_from` / `_resolve_session`.

**Zero adapter changes.** Every harness pair gets this for free because they
all read and write the same four records.

## Bug found in passing

`enrich.py`'s N² map has no same-harness pairs, so `hc --from claude --to
claude --dest-cwd DIR` silently drops the session title. The README calls that
path "pure metadata rewrite, lossless"; it is not. Truncate needs a
same-harness title to survive, so this is fixed here with four one-line
enrichers (`claude→claude`, `codex→codex`, `opencode→opencode`,
`grok→grok`).

Truncated sessions get their title suffixed `... [hc -20%]` so a session and
its trimmed child are distinguishable in the picker.

## Testing

Added to `tests/test_hconv.py` (assert-based, stdlib, no framework):

1. **Budget met**: synthetic session with known payload sizes; assert
   `freed >= pct` of total and that the chosen cap is the smallest that does so.
2. **Structure preserved**: no `ToolCall` or `ToolResult` lost, `call_id`
   pairing still closes after `synthesize_missing_results`.
3. **Inputs stay dicts**: a clipped `ToolCall.input` is still a dict with the
   same keys; only the largest string value shrank.
4. **Image rule**: a `view_image` result and a `data:image/png;base64,...`
   output are both replaced with the marker, not clipped.
5. **Shortfall**: an all-conversation session with `pct=50` reports honest
   under-delivery rather than raising or over-trimming.
6. **New id**: destination id differs from source, is stable across two runs
   with the same `pct`, and the original file is byte-identical afterwards.
7. **UTF-8**: clipping mid-multibyte-character yields valid UTF-8.

## Explicitly not doing

- No `--truncate` flag on `hc convert`. Two commands compose.
- No explicit-cap spelling (`--truncate 10KB`). The percentage is the knob;
  an explicit cap is three lines to add if it ever earns its place.
- No token estimation. Bytes are what we can measure honestly without a
  tokenizer, and the ratio is stable enough for this purpose.
- No trimming of user or assistant text. On Claude that is 19% of payload and
  it is the part you actually need on resume.
