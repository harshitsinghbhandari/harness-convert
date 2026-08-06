"""The common interface every harness must satisfy.

A session is structural metadata + an ordered list of records. The four record
kinds below are the universal floor: EVERY adapter reads its transcript into
these and writes these back out, no exceptions. This is what guarantees that any
harness pair converts at all.

Anything richer than the four records (session titles, permission modes, model
settings, ...) is deliberately NOT here. That surplus rides the N^2 enrichment
map (see enrich.py) and is parked in Session.extra. The common interface never
encodes the surplus; the surplus never re-encodes the common conversation.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field


@dataclass
class UserMessage:
    """Something the human typed."""
    text: str
    ts: str = ""


@dataclass
class AssistantMessage:
    """Agent's visible text reply (not its private reasoning, which is dropped)."""
    text: str
    ts: str = ""


@dataclass
class ToolCall:
    """An agent tool invocation. `name`/`input` are the source harness's; adapters
    translate to the destination's tool vocabulary on write."""
    call_id: str
    name: str
    input: dict
    ts: str = ""


@dataclass
class ToolResult:
    """The output that came back for a ToolCall, paired by call_id."""
    call_id: str
    output: str
    ts: str = ""
    is_error: bool = False


# The closed set of common records. A harness that needs more uses enrich.py.
Record = UserMessage | AssistantMessage | ToolCall | ToolResult


@dataclass
class Session:
    """Harness-neutral session: identity + the common record stream + a parking
    lot for enrichment payloads.

    Identity fields (id/cwd/branch/started_at) are structural: every adapter needs
    them to materialize a transcript. They are not "features"; they're addressing.
    """
    harness: str                                  # source harness name, e.g. "claude"
    session_id: str
    cwd: str
    records: list[Record] = field(default_factory=list)
    git_branch: str = ""
    started_at: str = ""                          # ISO timestamp of first record
    extra: dict = field(default_factory=dict)     # surplus, populated by enrich.py


def synthesize_missing_results(records: list[Record]) -> list[Record]:
    """Close every open ToolCall.

    Escape-hatch reality: the source harness usually died MID-TURN (rate limit hit
    while a tool was running), so the last ToolCall often has no ToolResult. Every
    destination needs the pairing closed or the resumed API call rejects the
    history. Inject a synthetic error result immediately after each orphan.

    This is the common case for this tool, not an edge case.
    """
    have = {r.call_id for r in records if isinstance(r, ToolResult)}
    out: list[Record] = []
    for r in records:
        out.append(r)
        if isinstance(r, ToolCall) and r.call_id not in have:
            out.append(ToolResult(r.call_id,
                                  "[no output; source session ended here]",
                                  ts=r.ts, is_error=True))
    return out


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
