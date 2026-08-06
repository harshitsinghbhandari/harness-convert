"""N^2 enrichment map. Keyed by (source_harness, dest_harness).

Carries ONLY the surplus fields the common interface drops, and only for pairs
where both harnesses can represent the feature. An enricher reads what the source
adapter stashed in Session.extra and translates it into a form the destination
adapter's write() will pick up (also via Session.extra).

By design this is optional and sparse: a missing (src, dst) entry just means
"common-only conversion", which always works. The map never re-encodes the four
common records, only the extras.

Example (not yet wired): carry the session title both ways.

    @register("claude", "codex")
    def _(s: Session) -> None:
        if "title" in s.extra:
            s.extra["codex_thread_name"] = s.extra["title"]
"""
from __future__ import annotations

from typing import Callable

from .common import Session

# Mutates Session.extra in place so the destination adapter's write() can consume.
Enricher = Callable[[Session], None]

_MAP: dict[tuple[str, str], Enricher] = {}


def register(src: str, dst: str):
    def deco(fn: Enricher) -> Enricher:
        _MAP[(src, dst)] = fn
        return fn
    return deco


def enrich(src: str, dst: str, session: Session) -> None:
    fn = _MAP.get((src, dst))
    if fn:
        fn(session)


# --- the surplus map -------------------------------------------------------
# Both harnesses support a human-facing session title. The common interface
# drops it; these carry it for the two pairs that can represent it. Writers
# consume only Session.extra["out"], so a pair with no enricher stays common-only.

@register("claude", "codex")
def _claude_to_codex(s: Session) -> None:
    if s.extra.get("title"):
        s.extra.setdefault("out", {})["thread_name"] = s.extra["title"]


@register("codex", "claude")
def _codex_to_claude(s: Session) -> None:
    if s.extra.get("title"):
        s.extra.setdefault("out", {})["ai_title"] = s.extra["title"]


# OpenCode carries the title as a first-class session column. Into opencode the
# writer reads out["opencode_title"]; out of opencode we reuse the keys the
# claude/codex writers already consume (ai_title / thread_name).

@register("claude", "opencode")
@register("codex", "opencode")
def _to_opencode(s: Session) -> None:
    if s.extra.get("title"):
        s.extra.setdefault("out", {})["opencode_title"] = s.extra["title"]


@register("opencode", "claude")
def _opencode_to_claude(s: Session) -> None:
    if s.extra.get("title"):
        s.extra.setdefault("out", {})["ai_title"] = s.extra["title"]


@register("opencode", "codex")
def _opencode_to_codex(s: Session) -> None:
    if s.extra.get("title"):
        s.extra.setdefault("out", {})["thread_name"] = s.extra["title"]


# Grok carries title as generated_title / session_summary on summary.json.
# Into grok the writer reads out["grok_title"]; out of grok we reuse the keys
# the other writers already consume.

@register("claude", "grok")
@register("codex", "grok")
@register("opencode", "grok")
@register("cursor", "grok")
def _to_grok(s: Session) -> None:
    if s.extra.get("title"):
        s.extra.setdefault("out", {})["grok_title"] = s.extra["title"]


@register("grok", "claude")
def _grok_to_claude(s: Session) -> None:
    if s.extra.get("title"):
        s.extra.setdefault("out", {})["ai_title"] = s.extra["title"]


@register("grok", "codex")
def _grok_to_codex(s: Session) -> None:
    if s.extra.get("title"):
        s.extra.setdefault("out", {})["thread_name"] = s.extra["title"]


@register("grok", "opencode")
def _grok_to_opencode(s: Session) -> None:
    if s.extra.get("title"):
        s.extra.setdefault("out", {})["opencode_title"] = s.extra["title"]


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
