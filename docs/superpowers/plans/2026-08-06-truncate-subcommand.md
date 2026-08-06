# `hc truncate` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `hc truncate <pct>`, which clips a session's heaviest tool payloads until `pct` of total payload is freed and writes a new session in the same harness, leaving the original untouched.

**Architecture:** One pure transform (`truncate_payload`) on the common record stream in `hconv/common.py`, called from `convert()` between `synthesize_missing_results` and `enrich`. Selection is a binary-searched per-record byte cap over a pool of tool outputs plus each tool input's largest string field. Zero adapter changes: every harness gets this for free because they all read and write the same four records.

**Tech Stack:** Python 3.10+, standard library only. Tests are plain `assert` + `print("PASS ...")` in `tests/test_hconv.py`, registered by hand in the `__main__` block, run with `python3 tests/test_hconv.py`.

## Global Constraints

- **Stdlib only.** No new dependencies, ever. `pyproject.toml` keeps `dependencies = []`.
- **Python >= 3.10** (`requires-python` floor). `X | Y` unions are fine; `match` is fine.
- **No em dashes or en dashes** in any code, comment, docstring, commit message, or doc. Use a period, comma, colon, semicolon, or parentheses.
- **No `Co-Authored-By` or AI attribution trailer** on any commit. End the message at the last body line.
- **Test style:** no pytest, no framework, no fixtures. A test is a module-level `def test_x(tmp)` using bare `assert`, ending in `print("PASS name: what held")`, and manually appended to the `__main__` block.
- **Every test run is the full suite:** `python3 tests/test_hconv.py`. Expected final line: `ALL PASS`.
- **`ponytail:` comments** name the ceiling and the upgrade path when a shortcut has a known limit.
- Spec of record: `docs/superpowers/specs/2026-08-06-truncate-subcommand-design.md`.

---

### Task 1: `truncate_payload` core transform

**Files:**
- Modify: `hconv/common.py` (append after `synthesize_missing_results`, which ends at line 90)
- Test: `tests/test_hconv.py`

**Interfaces:**
- Consumes: `UserMessage`, `AssistantMessage`, `ToolCall`, `ToolResult`, `Record` (all already in `hconv/common.py`)
- Produces:
  - `TrimStats` dataclass with int fields `total`, `freed`, `cap`, `clipped`, `pooled`, `pooled_bytes`, `target_pct`, and a float property `freed_pct`
  - `truncate_payload(records: list[Record], pct: int) -> tuple[list[Record], TrimStats]`
  - `truncated_id(session_id: str, pct: int) -> str`
  - `human_bytes(n: float) -> str`

- [ ] **Step 1: Write the failing tests**

Add these five tests to `tests/test_hconv.py`, immediately after `test_tail_closed` (which ends around line 52):

```python
def _big_session():
    """Payload with a deliberate size spread: one whale, one image, small fry."""
    return Session("test", "aaaa-bbbb-cccc-dddd-eeee", CWD, [
        UserMessage("do the thing", "2026-08-06T01:00:00Z"),
        AssistantMessage("on it", "2026-08-06T01:00:01Z"),
        ToolCall("c1", "Bash", {"command": "rg foo", "description": "search"},
                 "2026-08-06T01:00:02Z"),
        ToolResult("c1", "M" * 100_000, "2026-08-06T01:00:03Z"),      # the whale
        ToolCall("c2", "Write", {"file_path": "/x.py", "content": "W" * 40_000},
                 "2026-08-06T01:00:04Z"),
        ToolResult("c2", "ok", "2026-08-06T01:00:05Z"),
        ToolCall("c3", "view_image", {"path": "/s.png"}, "2026-08-06T01:00:06Z"),
        ToolResult("c3", "iVBORw0KGgo" * 5_000, "2026-08-06T01:00:07Z"),  # image
        ToolCall("c4", "Read", {"file_path": "/tiny.py"}, "2026-08-06T01:00:08Z"),
        ToolResult("c4", "short", "2026-08-06T01:00:09Z"),
    ], git_branch="main", started_at="2026-08-06T01:00:00Z")


def test_truncate_meets_budget():
    from hconv.common import truncate_payload
    recs, st = truncate_payload(_big_session().records, 20)
    assert st.freed_pct >= 20, f"freed {st.freed_pct:.1f}% < target 20%"
    # gentlest cap: one byte more would miss the target
    _, looser = truncate_payload(_big_session().records, 20)
    assert looser.cap == st.cap
    assert st.cap > 0 and st.clipped >= 1
    print(f"PASS truncate-budget: freed {st.freed_pct:.1f}% at cap {st.cap}B")


def test_truncate_preserves_structure():
    from hconv.common import truncate_payload
    before = _big_session().records
    n_calls = sum(1 for r in before if isinstance(r, ToolCall))
    n_res = sum(1 for r in before if isinstance(r, ToolResult))
    recs, _ = truncate_payload(_big_session().records, 40)
    assert sum(1 for r in recs if isinstance(r, ToolCall)) == n_calls
    assert sum(1 for r in recs if isinstance(r, ToolResult)) == n_res
    orphans = {r.call_id for r in recs if isinstance(r, ToolCall)} - \
              {r.call_id for r in recs if isinstance(r, ToolResult)}
    assert not orphans, f"truncate orphaned {orphans}"
    texts = [r.text for r in recs if isinstance(r, (UserMessage, AssistantMessage))]
    assert texts == ["do the thing", "on it"], "conversation text must not be trimmed"
    print("PASS truncate-structure: no record lost, pairing intact, text untouched")


def test_truncate_inputs_stay_dicts():
    from hconv.common import truncate_payload
    recs, _ = truncate_payload(_big_session().records, 40)
    write = [r for r in recs if isinstance(r, ToolCall) and r.name == "Write"][0]
    assert isinstance(write.input, dict), "input must stay a dict"
    assert set(write.input) == {"file_path", "content"}, "keys must survive"
    assert write.input["file_path"] == "/x.py", "small field untouched"
    assert len(write.input["content"]) < 40_000, "largest string field clipped"
    assert "[hc truncated" in write.input["content"]
    print("PASS truncate-inputs: dict shape kept, only the largest string clipped")


def test_truncate_images_dropped():
    from hconv.common import truncate_payload
    recs, _ = truncate_payload(_big_session().records, 40)
    img = [r for r in recs if isinstance(r, ToolResult) and r.call_id == "c3"][0]
    assert img.output.startswith("[image, "), img.output[:40]
    assert img.output.endswith("dropped by hc]"), img.output[-40:]
    assert "iVBORw0KGgo" not in img.output, "base64 must not be head-clipped"

    data_uri = Session("test", "x", CWD, [
        ToolCall("d1", "Read", {"file_path": "/a.png"}, "2026-08-06T01:00:00Z"),
        ToolResult("d1", "data:image/png;base64," + "Q" * 90_000,
                   "2026-08-06T01:00:01Z"),
    ], started_at="2026-08-06T01:00:00Z")
    recs2, _ = truncate_payload(data_uri.records, 40)
    assert recs2[1].output.startswith("[image, "), recs2[1].output[:40]
    print("PASS truncate-images: view_image and data:image both dropped, not clipped")


def test_truncate_shortfall_and_utf8():
    from hconv.common import truncate_payload, truncated_id
    talky = Session("test", "y", CWD, [
        UserMessage("a" * 50_000, "2026-08-06T01:00:00Z"),
        AssistantMessage("b" * 50_000, "2026-08-06T01:00:01Z"),
        ToolCall("c1", "Bash", {"command": "ls"}, "2026-08-06T01:00:02Z"),
        ToolResult("c1", "x" * 100, "2026-08-06T01:00:03Z"),
    ], started_at="2026-08-06T01:00:00Z")
    _, st = truncate_payload(talky.records, 50)
    assert st.freed_pct < 50, "50% is unreachable on an all-conversation session"
    assert st.total > 0 and st.freed >= 0, "must report honestly, not raise"

    multi = Session("test", "z", CWD, [
        ToolCall("c1", "Bash", {"command": "ls"}, "2026-08-06T01:00:00Z"),
        ToolResult("c1", "é" * 60_000, "2026-08-06T01:00:01Z"),
    ], started_at="2026-08-06T01:00:00Z")
    recs, _ = truncate_payload(multi.records, 30)
    recs[1].output.encode("utf-8").decode("utf-8")   # raises if clipped mid-char

    a = truncated_id("aaaa-bbbb", 20)
    assert a == truncated_id("aaaa-bbbb", 20), "id must be deterministic"
    assert a != truncated_id("aaaa-bbbb", 30), "different pct, different session"
    assert a != "aaaa-bbbb", "must never collide with the original"
    print("PASS truncate-shortfall: honest under-delivery, valid UTF-8, stable id")
```

Register all five in the `__main__` block right after `test_tail_closed()`:

```python
        test_truncate_meets_budget()
        test_truncate_preserves_structure()
        test_truncate_inputs_stay_dicts()
        test_truncate_images_dropped()
        test_truncate_shortfall_and_utf8()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 tests/test_hconv.py`
Expected: FAIL with `ImportError: cannot import name 'truncate_payload' from 'hconv.common'`

- [ ] **Step 3: Implement in `hconv/common.py`**

Add `import json` and `import uuid` at the top of the file (it currently imports only `dataclasses`), keeping `from __future__ import annotations` first. Then append after `synthesize_missing_results`:

```python
# --- payload truncation ----------------------------------------------------
# Escape-hatch part two: you moved the session, now it is too heavy to work in.
# Policy here is measured, not guessed (see the spec): payload concentration is
# extreme, so we clip the biggest payloads rather than the oldest ones, and we
# pool tool INPUTS alongside outputs because inputs are 37% of a Claude session.

IMAGE_TOOLS = {"view_image"}
TRIM_MARK = "... [hc truncated {n:,} bytes]"
IMAGE_MARK = "[image, {size}, dropped by hc]"


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)}B" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


@dataclass
class TrimStats:
    """What a truncate pass actually did. `freed` is accumulated during apply,
    never taken from the cap search, so it always matches what lands on disk."""
    target_pct: int = 0
    total: int = 0          # bytes of ALL payload: text + tool inputs + outputs
    pooled: int = 0         # trimmable payloads considered
    pooled_bytes: int = 0   # bytes those payloads hold
    cap: int = 0
    clipped: int = 0
    freed: int = 0

    @property
    def freed_pct(self) -> float:
        return 100.0 * self.freed / self.total if self.total else 0.0

    @property
    def reached_target(self) -> bool:
        return self.freed_pct + 0.05 >= self.target_pct


def _biggest_str_field(inp: dict) -> str | None:
    """The input key holding the most bytes of plain string.

    ponytail: top-level strings only, so a structured input (AskUserQuestion.
    questions, update_plan.plan) is never clipped. Ceiling: ~1.5% of Claude
    payload, ~0.3% of Codex. Upgrade path: recurse and pool the largest leaf.
    """
    best, best_n = None, 0
    for k, v in (inp or {}).items():
        if isinstance(v, str):
            n = len(v.encode())
            if n > best_n:
                best, best_n = k, n
    return best


def _get(rec, field: str | None) -> str:
    return rec.output if field is None else rec.input[field]


def _set(rec, field: str | None, value: str) -> None:
    if field is None:
        rec.output = value
    else:
        rec.input[field] = value


def _pool(records: list[Record]) -> list[tuple[int, bool, object, str | None]]:
    """Every trimmable payload as (nbytes, is_image, record, field).

    field is None for a ToolResult.output, else the ToolCall.input key.
    """
    names = {r.call_id: r.name for r in records if isinstance(r, ToolCall)}
    out = []
    for r in records:
        if isinstance(r, ToolResult):
            is_img = (names.get(r.call_id, "") in IMAGE_TOOLS
                      or r.output.startswith("data:image/"))
            out.append((len(r.output.encode()), is_img, r, None))
        elif isinstance(r, ToolCall):
            k = _biggest_str_field(r.input)
            if k is not None:
                out.append((len(r.input[k].encode()), False, r, k))
    return out


def _clip(value: str, is_image: bool, cap: int) -> str:
    """The clipped form of `value` at `cap` bytes; unchanged if it already fits."""
    raw = value.encode()
    if len(raw) <= cap:
        return value
    if is_image:
        return IMAGE_MARK.format(size=human_bytes(len(raw)))
    # "ignore" drops a trailing partial character, which IS the UTF-8 backoff.
    head = raw[:cap].decode("utf-8", "ignore")
    return head + TRIM_MARK.format(n=len(raw) - len(head.encode()))


def _est_freed(pool, cap: int) -> int:
    """Bytes a candidate cap would free. Pure arithmetic on precomputed sizes:
    a 21-probe binary search over 20k payloads does zero string work. Exact but
    for UTF-8 backoff (<=3 bytes per clipped payload), which is why the apply
    pass, not this, is what TrimStats.freed reports."""
    total = 0
    for nbytes, is_image, _rec, _f in pool:
        if nbytes <= cap:
            continue
        if is_image:
            total += nbytes - len(IMAGE_MARK.format(
                size=human_bytes(nbytes)).encode())
        else:
            total += nbytes - cap - len(TRIM_MARK.format(n=nbytes - cap).encode())
    return total


def _search_cap(pool, want: float) -> int:
    """Largest cap that still frees >= want bytes.

    Freed shrinks as the cap grows, so valid caps are [1, threshold] and the
    largest is the gentlest. Returns 1 (maximum damage) when even that misses,
    which is the honest shortfall case; the caller reports it, never hides it.
    """
    lo, hi = 1, max(n for n, _, _, _ in pool)
    if _est_freed(pool, lo) < want:
        return lo
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _est_freed(pool, mid) >= want:
            lo = mid
        else:
            hi = mid - 1
    return lo


def truncate_payload(records: list[Record],
                     pct: int) -> tuple[list[Record], TrimStats]:
    """Clip the heaviest payloads until `pct` of TOTAL payload is freed.

    Total is the whole session (conversation text included), because a user
    asking to free 20% means 20% of what they are carrying, not 20% of some
    subset they cannot see. Conversation text is never clipped: on Claude it is
    19% of payload and it is the part you actually need on resume.

    Mutates the records in place and returns the same list, alongside stats.
    """
    stats = TrimStats(target_pct=pct)
    for r in records:
        if isinstance(r, (UserMessage, AssistantMessage)):
            stats.total += len(r.text.encode())
        elif isinstance(r, ToolResult):
            stats.total += len(r.output.encode())
        elif isinstance(r, ToolCall):
            stats.total += len(json.dumps(r.input).encode())

    pool = _pool(records)
    stats.pooled = len(pool)
    stats.pooled_bytes = sum(n for n, _, _, _ in pool)
    if not pool or pct <= 0 or not stats.total:
        return records, stats

    stats.cap = _search_cap(pool, stats.total * pct / 100)
    for nbytes, is_image, rec, field in pool:
        new = _clip(_get(rec, field), is_image, stats.cap)
        new_n = len(new.encode())
        if new_n < nbytes:
            _set(rec, field, new)
            stats.freed += nbytes - new_n
            stats.clipped += 1
    return records, stats


def truncated_id(session_id: str, pct: int) -> str:
    """Destination id for a truncated session. Deterministic so re-running the
    same truncate upserts one destination instead of piling up copies; new so
    the original is never overwritten. Same precedent as grok._dest_id."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"harness-convert:truncate:{session_id}:{pct}"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 tests/test_hconv.py`
Expected: PASS, five new `PASS truncate-*` lines, final line `ALL PASS`

- [ ] **Step 5: Commit**

```bash
git add hconv/common.py tests/test_hconv.py
git commit -m "Add truncate_payload: clip heaviest tool payloads to free context

Pools every ToolResult.output plus each ToolCall.input's largest string field,
binary-searches the largest per-record byte cap that frees the requested
percent of total payload, then clips. Conversation text is never touched.

Policy is measured, not guessed: payload concentration is extreme (41 Codex
records over 200KB are 45.9% of all payload) so biggest-first beats
oldest-first, and tool inputs are 37% of a Claude session so outputs-only
cannot pass ~44% freed.

Image payloads above the cap are replaced outright rather than head-clipped,
since 4KB of base64 is bounded junk rather than information."
```

---

### Task 2: same-harness title enrichers

**Files:**
- Modify: `hconv/enrich.py` (append after the last enricher, `_grok_to_opencode`, which ends at line 112)
- Test: `tests/test_hconv.py`

**Interfaces:**
- Consumes: `register(src, dst)` and `enrich(src, dst, session)` from `hconv/enrich.py`
- Produces: `(claude, claude)`, `(codex, codex)`, `(opencode, opencode)`, `(grok, grok)` entries in the enrichment map. No new names.

This is a standalone bug fix that `truncate` depends on. `enrich.py` has no same-harness pairs at all, so `hc --from claude --to claude --dest-cwd DIR` silently drops the session title today, despite `README.md:71-72` calling that path "pure metadata rewrite, lossless".

- [ ] **Step 1: Write the failing test**

Add to `tests/test_hconv.py` after `test_title_enrichment` (which ends around line 158):

```python
def test_same_harness_title_survives():
    """Relocating within a harness is documented as lossless; the title is
    part of that. Before this, no (X, X) enricher existed and it vanished."""
    for harness, key in (("claude", "ai_title"), ("codex", "thread_name"),
                         ("opencode", "opencode_title"), ("grok", "grok_title")):
        s = sample()
        s.extra["title"] = "keep me"
        enrich(harness, harness, s)
        got = s.extra.get("out", {}).get(key)
        assert got == "keep me", f"{harness}->{harness} lost the title ({key}={got!r})"
    print("PASS same-harness-title: claude/codex/opencode/grok keep their title")
```

Register it in the `__main__` block right after `test_title_enrichment(tmp)`:

```python
        test_same_harness_title_survives()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_hconv.py`
Expected: FAIL with `AssertionError: claude->claude lost the title (ai_title=None)`

- [ ] **Step 3: Implement in `hconv/enrich.py`**

Append at the end of the file:

```python
# Same-harness relocation (a cwd move, or `hc truncate`) is documented as a
# lossless metadata rewrite. Without these four the title silently vanished,
# because the map only ever held cross-harness pairs.

@register("claude", "claude")
def _claude_to_claude(s: Session) -> None:
    if s.extra.get("title"):
        s.extra.setdefault("out", {})["ai_title"] = s.extra["title"]


@register("codex", "codex")
def _codex_to_codex(s: Session) -> None:
    if s.extra.get("title"):
        s.extra.setdefault("out", {})["thread_name"] = s.extra["title"]


@register("opencode", "opencode")
def _opencode_to_opencode(s: Session) -> None:
    if s.extra.get("title"):
        s.extra.setdefault("out", {})["opencode_title"] = s.extra["title"]


@register("grok", "grok")
def _grok_to_grok(s: Session) -> None:
    if s.extra.get("title"):
        s.extra.setdefault("out", {})["grok_title"] = s.extra["title"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_hconv.py`
Expected: PASS with `PASS same-harness-title: ...`, final line `ALL PASS`

- [ ] **Step 5: Commit**

```bash
git add hconv/enrich.py tests/test_hconv.py
git commit -m "Fix: same-harness relocation silently dropped the session title

enrich.py's map held only cross-harness pairs, so hc --from claude --to claude
--dest-cwd DIR lost the title even though the README calls that path a lossless
pure metadata rewrite. Adds the four identity enrichers."
```

---

### Task 3: wire truncation into `convert()`

**Files:**
- Modify: `hconv/adapter.py:111-123` (the `convert` function)
- Test: `tests/test_hconv.py`

**Interfaces:**
- Consumes: `truncate_payload`, `truncated_id` from Task 1; the identity enrichers from Task 2
- Produces: `convert(src_name, dst_name, cwd, dest_cwd, session_id=None, write=False, truncate=0, new_id=False)`. When `truncate` is non-zero it sets `session.extra["trim"]` to a `TrimStats`; when `new_id` is also set it sets `session.extra["source_session_id"]` to the original id and replaces `session.session_id`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_hconv.py` after `test_same_harness_title_survives`:

```python
def test_convert_truncate_new_session(tmp):
    """truncate writes a NEW session and never touches the original."""
    claude_mod.PROJECTS = Path(tmp) / "claude_trunc"
    src = sample()
    src.records = [
        UserMessage("go", "2026-08-06T01:00:00Z"),
        ToolCall("c1", "Bash", {"command": "rg foo"}, "2026-08-06T01:00:01Z"),
        ToolResult("c1", "Z" * 80_000, "2026-08-06T01:00:02Z"),
    ]
    src.extra["title"] = "original work"
    original = claude_mod.ClaudeAdapter().write(src, CWD)
    before = original.read_bytes()

    session, dest = hconv.convert("claude", "claude", CWD, CWD,
                                  session_id=src.session_id, write=True,
                                  truncate=30, new_id=True)

    assert dest != original, "truncate must not overwrite its source"
    assert original.read_bytes() == before, "source file was modified"
    assert dest.exists(), f"no truncated session at {dest}"

    st = session.extra["trim"]
    assert st.freed_pct >= 30, f"freed {st.freed_pct:.1f}% < 30%"
    assert session.extra["source_session_id"] == src.session_id
    assert session.session_id != src.session_id

    body = dest.read_text()
    assert "[hc truncated" in body, "clipped marker missing from written session"
    assert "Z" * 80_000 not in body, "whale survived into the truncated session"
    assert "[hc -30%]" in body, "truncated title should be distinguishable"

    again, dest2 = hconv.convert("claude", "claude", CWD, CWD,
                                 session_id=src.session_id, write=False,
                                 truncate=30, new_id=True)
    assert dest2 == dest, "same truncate must be deterministic, not pile up copies"
    print("PASS convert-truncate: new session written, original byte-identical")
```

Register it in the `__main__` block right after `test_same_harness_title_survives()`:

```python
        test_convert_truncate_new_session(tmp)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_hconv.py`
Expected: FAIL with `TypeError: convert() got an unexpected keyword argument 'truncate'`

- [ ] **Step 3: Implement in `hconv/adapter.py`**

Change the import on line 20 from:

```python
from .common import Session, synthesize_missing_results
```

to:

```python
from .common import (Session, synthesize_missing_results, truncate_payload,
                     truncated_id)
```

Then replace the whole `convert` function (lines 111-123) with:

```python
def convert(src_name: str, dst_name: str, cwd: str, dest_cwd: str,
            session_id: str | None = None, write: bool = False,
            truncate: int = 0, new_id: bool = False):
    """Run the full pipeline. Returns (session, dest_path). Writes only if asked.

    truncate: percent of total payload to free (0 = off). new_id: give the
    result a fresh deterministic session id so it lands beside its source
    instead of overwriting it (what `hc truncate` wants).
    """
    from .enrich import enrich

    src, dst = get(src_name), get(dst_name)
    path = src.locate(cwd, session_id)
    session = src.read(path)
    session.records = synthesize_missing_results(session.records)
    if truncate:
        # After synthesize (pairing invariants already hold), before enrich
        # (so enrichers see final content and the marked-up title).
        session.records, stats = truncate_payload(session.records, truncate)
        session.extra["trim"] = stats
        if new_id:
            session.extra["source_session_id"] = session.session_id
            session.session_id = truncated_id(session.session_id, truncate)
        if session.extra.get("title"):
            session.extra["title"] += f" [hc -{truncate}%]"
    enrich(src_name, dst_name, session)
    if not write:
        return session, dst.dest_path(session, dest_cwd)
    return session, dst.write(session, dest_cwd)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_hconv.py`
Expected: PASS with `PASS convert-truncate: ...`, final line `ALL PASS`

- [ ] **Step 5: Commit**

```bash
git add hconv/adapter.py tests/test_hconv.py
git commit -m "Wire truncation into convert()

Adds truncate (percent) and new_id to convert(). Truncation runs after
synthesize_missing_results so pairing invariants already hold, and before
enrich so enrichers see final content and the marked-up title. Stats ride
Session.extra['trim'], which is what extra exists for."
```

---

### Task 4: `hc truncate` subcommand

**Files:**
- Modify: `hconv/cli.py` (add `cmd_truncate` and `_pct`; extend `main`'s parser and argv munging at lines 255-259)
- Modify: `README.md`
- Test: `tests/test_hconv.py`

**Interfaces:**
- Consumes: `convert(..., truncate=, new_id=)` from Task 3; `human_bytes`, `TrimStats` from Task 1; existing `_resolve_from`, `_resolve_session`, `_print_resume`, `ui` helpers in `hconv/cli.py`
- Produces: `hc truncate <pct> [session_id]` with `--from`, `--cwd`, `--dest-cwd`, `--write`, `-y/--yes`, `-n`, `--no-interactive`. Nothing later depends on it.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_hconv.py` after `test_cli_noninteractive_convert` (which ends around line 780):

```python
def test_cli_truncate(tmp):
    from hconv import cli
    claude_mod.PROJECTS = Path(tmp) / "claude_cli_trunc"
    s = sample()
    s.records = [
        UserMessage("go", "2026-08-06T01:00:00Z"),
        ToolCall("c1", "Bash", {"command": "rg foo"}, "2026-08-06T01:00:01Z"),
        ToolResult("c1", "Q" * 80_000, "2026-08-06T01:00:02Z"),
    ]
    original = claude_mod.ClaudeAdapter().write(s, CWD)
    before = original.read_bytes()

    def run(argv):
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.dict(os.environ, {"HC_NO_INTERACTIVE": "1"}), \
             mock.patch.object(sys, "stdout", buf):
            cli.main()
        return buf.getvalue()

    base = ["hc", "truncate", "30", "--from", "claude", "--cwd", CWD,
            "--dest-cwd", CWD, s.session_id]

    out = run(base)
    assert "dry run" in out, out
    assert "cap" in out and "freed" in out, out
    assert original.read_bytes() == before, "dry run must not write"

    out = run(base + ["-y"])
    assert "WROTE." in out, out
    assert "claude --resume" in out, out
    assert original.read_bytes() == before, "original must survive the write"

    # percent is validated at the boundary
    for bad in ("0", "100", "abc"):
        try:
            run(["hc", "truncate", bad, "--from", "claude", "--cwd", CWD])
        except SystemExit:
            pass
        else:
            raise AssertionError(f"percent {bad!r} should have been rejected")

    # cursor is read-only, so it can never be a truncate source
    try:
        run(["hc", "truncate", "20", "--from", "cursor", "--cwd", CWD])
    except SystemExit as e:
        assert "read-only" in str(e), e
    else:
        raise AssertionError("truncating a cursor session should be refused")
    print("PASS cli-truncate: dry-run, write, percent validation, cursor refused")
```

Register it in the `__main__` block right after `test_cli_noninteractive_convert(tmp)`:

```python
        test_cli_truncate(tmp)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_hconv.py`
Expected: FAIL with `SystemExit: 2` and argparse printing `invalid choice: 'truncate'`

- [ ] **Step 3: Implement in `hconv/cli.py`**

Change the import on line 27 from:

```python
from hconv import convert, get, known, writable
```

to:

```python
from hconv import convert, get, known, writable
from hconv.common import human_bytes
```

Add after `_print_preview` (which ends at line 68):

```python
def _print_trim(stats) -> None:
    """The three lines that make a trim auditable before it is written."""
    total, freed = stats.total, stats.freed
    print(f"{ui.dim('trim')}    {stats.target_pct}% of {human_bytes(total)} payload")
    print(f"{ui.dim('cap')}     {human_bytes(stats.cap)}  "
          f"(clips {stats.clipped} of {stats.pooled} payloads)")
    print(f"{ui.dim('freed')}   {human_bytes(freed)}  "
          f"{human_bytes(total)} -> {human_bytes(total - freed)}")
    if not stats.reached_target:
        conv = 100 - (100 * stats.pooled_bytes / total if total else 0)
        print(ui.dim(f"        target {stats.target_pct}% unreachable; freed "
                     f"{stats.freed_pct:.1f}% ({conv:.0f}% of payload is "
                     f"conversation, which is never trimmed)"))
```

Add after `_resolve_session` (which ends at line 115):

```python
def _pct(raw: str) -> int:
    try:
        n = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"percent must be a number, got {raw!r}")
    if not 1 <= n <= 99:
        raise argparse.ArgumentTypeError(f"percent must be 1-99, got {n}")
    return n
```

Add after `cmd_convert` (which ends at line 161):

```python
def cmd_truncate(a):
    """Shrink a session in place: same harness, NEW id, original untouched."""
    interactive = ui.can_interact(not a.no_interactive)
    from_h = _resolve_from(a.from_harness, interactive)
    if from_h not in writable():
        raise SystemExit(f"error: cannot truncate a '{from_h}' session (read-only)")
    session_id = _resolve_session(from_h, a.cwd, session_id=a.session_id,
                                  interactive=interactive, limit=a.n)
    dest_cwd = a.dest_cwd or a.cwd

    session, dest = convert(from_h, from_h, a.cwd, dest_cwd,
                            session_id=session_id, write=False,
                            truncate=a.pct, new_id=True)
    stats = session.extra["trim"]

    src_id = session.extra.get("source_session_id", session.session_id)
    print(f"{ui.dim('from')}    {ui.bold(from_h)}  {src_id}")
    _print_trim(stats)
    print(f"{ui.dim('new')}     {session.session_id}  {ui.dim('(original untouched)')}")
    print(f"{ui.dim('dest')}    {dest}")

    do_write = bool(a.write or a.yes)
    if not do_write and interactive:
        do_write = ui.confirm("Write truncated session?", default=False)
    if not do_write:
        print()
        print(ui.dim("(dry run; pass --write or -y to create it, "
                     "or confirm when prompted)"))
        return

    dest = get(from_h).write(session, dest_cwd)
    _print_resume(from_h, session, dest, dest_cwd)
```

In `main`, add the parser after the `list` parser block (which ends at line 253):

```python
    t = sub.add_parser("truncate",
                       help="shrink a session to free context "
                            "(writes a NEW session; original untouched)")
    add_common(t)
    t.add_argument("pct", type=_pct, metavar="PCT",
                   help="percent of total payload to free (1-99)")
    t.add_argument("session_id", nargs="?", default=None,
                   help="session id (default: pick / latest for cwd)")
    t.add_argument("--dest-cwd", default=None,
                   help="destination folder (default: same as --cwd)")
    t.add_argument("--write", action="store_true", help="write without asking")
    t.add_argument("-y", "--yes", action="store_true",
                   help="write without asking (alias: implies write)")
    t.add_argument("-n", type=int, default=15, metavar="N",
                   help="how many sessions to offer when picking (default: 15)")
    t.set_defaults(func=cmd_truncate)
```

Then teach the argv munging about it. Change line 258 from:

```python
    elif sys.argv[1] not in ("convert", "list", "-h", "--help"):
```

to:

```python
    elif sys.argv[1] not in ("convert", "list", "truncate", "-h", "--help"):
```

Finally, extend the module docstring usage block (lines 8-13) by adding this line after the `hc list` lines:

```
    hc truncate 20 --from claude       # new session, 20% lighter
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_hconv.py`
Expected: PASS with `PASS cli-truncate: ...`, final line `ALL PASS`

- [ ] **Step 5: Update `README.md`**

In the usage block (lines 10-18), add after the `hc list` lines:

```bash
hc truncate 20 --from claude                # new session, 20% lighter
hc truncate 20 --from claude <session-id>   # a specific one
```

Add a new section between "How it works" (ends line 51) and "Install" (line 53):

```markdown
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
```

Also fix the stale claim on lines 71-72. Change:

```
across working directories (pure metadata rewrite, lossless).
```

to:

```
across working directories (pure metadata rewrite, lossless, title included).
```

- [ ] **Step 6: Verify the real CLI end to end**

Run: `python3 hc.py truncate --help`
Expected: usage text showing `PCT` and `--from`, exit 0

Run: `python3 hc.py truncate 20 --from claude --no-interactive`
Expected: either a `from` / `trim` / `cap` / `freed` / `new` / `dest` preview ending in `(dry run; ...)`, or `no claude sessions found for <cwd>` if this repo has no Claude session. Both are correct; it must not traceback.

- [ ] **Step 7: Commit**

```bash
git add hconv/cli.py README.md tests/test_hconv.py
git commit -m "Add hc truncate subcommand

hc truncate <pct> shrinks a session and writes a NEW session in the same
harness, leaving the original byte-identical. Dry run by default; the preview
shows the chosen cap, how many payloads it clips, and the bytes freed, and it
declares an honest shortfall when the target is unreachable rather than
quietly under-delivering.

Deliberately not a flag on hc convert: the two compose as separate commands,
since truncate's output is an ordinary session."
```

---

## Self-Review

**Spec coverage.** Every section maps to a task: selection and clipping and images and shortfall to Task 1; new deterministic id to Tasks 1 and 3; the same-harness enricher bug to Task 2; pipeline placement to Task 3; CLI surface, argv munging, preview, and docs to Task 4. All seven spec test cases are covered (budget met, structure preserved, inputs stay dicts, image rule, shortfall, new id, UTF-8), plus two the spec did not name: the original file staying byte-identical, and cursor being refused as a truncate source.

**Placeholder scan.** No TBD, no "add error handling", no "similar to Task N". Every code step carries the literal code, every test step the literal test.

**Type consistency.** `TrimStats` fields (`target_pct`, `total`, `pooled`, `pooled_bytes`, `cap`, `clipped`, `freed`) and its `freed_pct` / `reached_target` properties are defined in Task 1 and used with those exact names in Tasks 3 and 4. `truncate_payload` returns `(records, stats)` in Task 1 and is unpacked that way in Task 3. `truncated_id(session_id, pct)` and `human_bytes(n)` match across Tasks 1, 3, and 4. `convert(..., truncate=, new_id=)` is defined in Task 3 and called with those keywords in Tasks 3 and 4. `session.extra["trim"]` and `session.extra["source_session_id"]` are written in Task 3 and read in Task 4.

**One thing deliberately left to execution:** exact line numbers drift as earlier tasks land. Anchors are given as "after function X" as well as line numbers; trust the function name when they disagree.
