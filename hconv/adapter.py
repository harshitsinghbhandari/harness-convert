"""Adapter contract + registry. One adapter per harness.

The whole conversion pipeline is:

    src.locate(cwd, id?)  ->  path
    src.read(path)        ->  Session            (transcript -> common interface)
    normalize(session)    ->  Session            (close ragged tails, shared)
    enrich(src, dst, s)   ->  Session            (N^2 surplus, optional)
    dst.write(session)    ->  path               (common interface -> transcript)

The common path (locate/read/write over the four records) guarantees any pair
works. Enrichment only adds surplus and is allowed to be missing.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from .common import (Session, synthesize_missing_results, truncate_payload,
                     truncated_id)


@dataclass(frozen=True)
class SessionRef:
    """A convertible session as the list/locate pickers see it.

    Sorted newest-first by `mtime` (epoch seconds of last activity / last write,
    harness-specific: see each adapter). `title` is best-effort and may be empty.
    """
    path: Path
    session_id: str
    mtime: float
    title: str = ""


class Adapter(ABC):
    """A harness's reader/writer/locator. Subclasses set `name` and register().

    locate/read are mandatory; dest_path/write are not. A harness whose store we
    can only safely READ (cursor) sets `writable = False` and inherits the
    refusing defaults below, so it can be a --from and never a --to.
    """

    name: str
    writable: bool = True

    @abstractmethod
    def locate(self, cwd: str, session_id: str | None = None) -> Path:
        """Resolve a transcript file. With session_id: that session. Without:
        the most recent session for `cwd` in this harness's store (the fast-path
        default that makes `hc --from X --to Y` need no id)."""

    def list_sessions(self, cwd: str, limit: int = 10) -> list[SessionRef]:
        """Newest-first convertible sessions for `cwd`, up to `limit`.

        Default falls back to a single locate() hit so older adapters keep
        working; harnesses that can cheaply enumerate override this.
        """
        if limit < 1:
            return []
        try:
            p = self.locate(cwd)
        except SystemExit:
            return []
        return [SessionRef(path=p, session_id=p.stem, mtime=p.stat().st_mtime)]

    @abstractmethod
    def read(self, path: Path) -> Session:
        """Parse a transcript file into the common Session. Drops private
        reasoning; stashes harness surplus into Session.extra for enrichment."""

    def dest_path(self, session: Session, dest_cwd: str) -> Path:
        """Where a converted Session WOULD be written under dest_cwd. Pure; no IO.
        Used for dry-run and so write() and the dry-run path never disagree."""
        raise SystemExit(f"{self.name} sessions are read-only; "
                         f"hc cannot convert INTO {self.name}")

    def write(self, session: Session, dest_cwd: str) -> Path:
        """Materialize a Session as this harness's transcript at dest_path(),
        rewriting identity (id/cwd) so the result is self-consistent and natively
        resumable. Returns the written path."""
        raise SystemExit(f"{self.name} sessions are read-only; "
                         f"hc cannot convert INTO {self.name}")


_REGISTRY: dict[str, Adapter] = {}


def register(adapter: Adapter) -> Adapter:
    _REGISTRY[adapter.name] = adapter
    return adapter


def get(name: str) -> Adapter:
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none registered)"
        raise SystemExit(f"unknown harness '{name}'. known: {known}")


def known() -> list[str]:
    return sorted(_REGISTRY)


def writable() -> list[str]:
    """Harnesses hc can convert INTO. Read-only ones (cursor) are --from only."""
    return sorted(n for n, a in _REGISTRY.items() if a.writable)


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
        # Guard against resolving the destination back onto the source: same
        # harness plus same dest_cwd plus new_id left off does exactly that,
        # and a caller forgetting new_id=True must not silently destroy the
        # un-truncated original. dest_path is documented as pure/no IO, so
        # this check is safe to run on both the dry-run and write paths, and
        # it compares resolved paths (not flags) so it holds no matter how
        # harness/cwd/new_id were combined to get here.
        dest_check = dst.dest_path(session, dest_cwd)
        if dest_check.resolve() == path.resolve():
            raise SystemExit(
                f"refusing to truncate {path} onto itself, which would destroy "
                "the original; pass new_id=True or a different --dest-cwd")
    enrich(src_name, dst_name, session)
    if not write:
        return session, dst.dest_path(session, dest_cwd)
    return session, dst.write(session, dest_cwd)
