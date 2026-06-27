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
from pathlib import Path

from .common import Session, synthesize_missing_results


class Adapter(ABC):
    """A harness's reader/writer/locator. Subclasses set `name` and register()."""

    name: str

    @abstractmethod
    def locate(self, cwd: str, session_id: str | None = None) -> Path:
        """Resolve a transcript file. With session_id: that session. Without:
        the most recent session for `cwd` in this harness's store (the fast-path
        default that makes `hc --from X --to Y` need no id)."""

    @abstractmethod
    def read(self, path: Path) -> Session:
        """Parse a transcript file into the common Session. Drops private
        reasoning; stashes harness surplus into Session.extra for enrichment."""

    @abstractmethod
    def dest_path(self, session: Session, dest_cwd: str) -> Path:
        """Where a converted Session WOULD be written under dest_cwd. Pure; no IO.
        Used for dry-run and so write() and the dry-run path never disagree."""

    @abstractmethod
    def write(self, session: Session, dest_cwd: str) -> Path:
        """Materialize a Session as this harness's transcript at dest_path(),
        rewriting identity (id/cwd) so the result is self-consistent and natively
        resumable. Returns the written path."""


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


def convert(src_name: str, dst_name: str, cwd: str, dest_cwd: str,
            session_id: str | None = None, write: bool = False):
    """Run the full pipeline. Returns (session, dest_path). Writes only if asked."""
    from .enrich import enrich

    src, dst = get(src_name), get(dst_name)
    path = src.locate(cwd, session_id)
    session = src.read(path)
    session.records = synthesize_missing_results(session.records)
    enrich(src_name, dst_name, session)
    if not write:
        return session, dst.dest_path(session, dest_cwd)
    return session, dst.write(session, dest_cwd)
