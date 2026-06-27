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
                                  "[no output — source session ended here]",
                                  ts=r.ts, is_error=True))
    return out
