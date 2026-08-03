"""Cursor adapter. READ-ONLY: hc can move a Cursor session out, never in.

Storage is ~/.cursor/chats/<md5(cwd)>/<session-uuid>/store.db, a content-addressed
blob store: blobs(id = sha256(data), data) plus meta(key, value) whose single
value is a HEX-encoded JSON header carrying `latestRootBlobId`. The db is usually
mostly-WAL (a 4K store.db next to a 1M -wal); `mode=ro` reads the wal fine.

`latestRootBlobId` is the only protobuf in the layout: a flat, ORDERED list of
32-byte child hashes at field 1. Each child is a plain-JSON message in Vercel AI
SDK vocabulary ({"role", "content":[{"type":"text"|"reasoning"|"tool-call"|
"tool-result"}]}). Walking the root is mandatory, not a nicety: the blob table is
content-addressed and retains superseded revisions, so scraping it directly gives
stale duplicates in no particular order.

meta.json (cwd/title/createdAtMs) sits next to store.db on real user sessions
only. A session spawned as a SUBAGENT gets a store.db and no meta.json, so the
presence of meta.json is what locate() filters on -- the same policy as claude.py
dropping isSidechain rows. read() still works on a subagent dir directly.

Read-only because Cursor owns this store live (WAL + checkpointing + a
blobEncryptionKey in the header we don't produce); writing into it would be a
corruption bet with no upside.
"""
from __future__ import annotations

import binascii
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..adapter import Adapter, SessionRef, register
from ..common import (AssistantMessage, Session, ToolCall, ToolResult,
                      UserMessage)

CHATS = Path(os.path.expanduser("~/.cursor/chats"))

# Cursor tool vocab -> Claude's, which is the common-ish middle the other
# adapters already translate through. Unmapped names pass through untouched.
INBOUND_NAMES = {"Shell": "Bash", "ReadFile": "Read", "StrReplace": "Edit"}

# What the human actually typed; the rest of a user turn is harness scaffolding
# (<timestamp>, <system_reminder>, the <user_info> environment preamble).
_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)


def _iso(ms) -> str:
    try:
        return (datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
                .isoformat().replace("+00:00", "Z"))
    except (TypeError, ValueError, OSError):
        return ""


def _varint(b: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        c = b[i]; i += 1
        n |= (c & 0x7F) << shift
        if not c & 0x80:
            return n, i
        shift += 7


def _root_children(blob: bytes) -> list[str]:
    """Protobuf wire walk of the root blob -> hex sha256 of every child, in order.

    Only field 1 is decoded; the rest of the root (model config, cwd, timezone,
    a parallel field-8 blob list) is surplus the common records don't need.
    """
    out: list[str] = []
    i = 0
    try:
        while i < len(blob):
            key, i = _varint(blob, i)
            wire = key & 7
            if wire == 2:
                ln, i = _varint(blob, i)
                if key >> 3 == 1 and ln == 32:
                    out.append(blob[i:i + ln].hex())
                i += ln
            elif wire == 0:
                _, i = _varint(blob, i)
            elif wire == 1:
                i += 8
            elif wire == 5:
                i += 4
            else:
                break                      # groups (3/4): this store never emits them
    except IndexError:
        pass                               # truncated tail; keep what we decoded
    return out


class CursorAdapter(Adapter):
    name = "cursor"
    writable = False

    # locate() returns the SESSION DIRECTORY (store.db + meta.json live in it).
    def _candidates(self, cwd: str) -> list[tuple[float, Path, dict]]:
        """(mtime_epoch, session_dir, meta) for real user sessions on this cwd."""
        if not CHATS.is_dir():
            return []
        fast = CHATS / hashlib.md5(cwd.encode()).hexdigest()
        # No meta.json => subagent transcript (or a pre-schemaVersion-1 session);
        # globbing meta.json is what keeps both out of the picker.
        metas = list(fast.glob("*/meta.json")) or list(CHATS.glob("*/*/meta.json"))
        found = []
        for mp in metas:
            try:
                m = json.loads(mp.read_text())
            except (OSError, ValueError):
                continue
            if m.get("cwd") == cwd and m.get("hasConversation"):
                ms = m.get("updatedAtMs") or m.get("createdAtMs") or 0
                found.append((ms / 1000 if ms else 0.0, mp.parent, m))
        return found

    def list_sessions(self, cwd: str, limit: int = 10) -> list[SessionRef]:
        if limit < 1:
            return []
        found = sorted(self._candidates(cwd), key=lambda t: t[0], reverse=True)
        out = []
        for mtime, d, m in found[:limit]:
            sid = m.get("agentId") or d.name
            title = m.get("title") or ""
            if title == "New Agent":
                title = ""
            out.append(SessionRef(path=d, session_id=sid, mtime=mtime, title=title))
        return out

    def locate(self, cwd: str, session_id: str | None = None) -> Path:
        if not CHATS.is_dir():
            raise SystemExit(f"no Cursor chats directory at {CHATS}")
        fast = CHATS / hashlib.md5(cwd.encode()).hexdigest()
        found = self._candidates(cwd)
        if session_id:
            found = [f for f in found if session_id in f[1].name]
            if not found:
                raise SystemExit(f"no Cursor session {session_id} for cwd {cwd} "
                                 f"(looked in {CHATS})")
            return max(found, key=lambda t: t[0])[1]
        if not found:
            raise SystemExit(f"no Cursor sessions found for cwd {cwd} (looked in {fast})")
        return max(found, key=lambda t: t[0])[1]

    def read(self, path: Path) -> Session:
        d = Path(path)
        db = d / "store.db"
        if not db.exists():
            raise SystemExit(f"no Cursor store.db in {d}")
        try:                               # absent on subagent dirs; db header covers us
            meta = json.loads((d / "meta.json").read_text())
        except (OSError, ValueError):
            meta = {}
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT value FROM meta LIMIT 1").fetchone()
            hdr = json.loads(binascii.unhexlify(row[0])) if row else {}
            blobs = dict(con.execute("SELECT id, data FROM blobs"))
        finally:
            con.close()

        # ponytail: no per-message timestamps anywhere in the store, so every
        # record carries the session start. Upgrade path: the field-8 protobuf
        # blob list, which looks like a parallel per-turn index, if anyone needs
        # real per-record times.
        ts = _iso(meta.get("createdAtMs") or hdr.get("createdAt"))
        records = []
        for h in _root_children(blobs.get(hdr.get("latestRootBlobId") or "", b"")):
            try:
                msg = json.loads(blobs[h])
            except (KeyError, ValueError, TypeError):
                continue
            role, content = msg.get("role"), msg.get("content")
            # The system prompt and the <user_info> environment preamble are the
            # only messages stored with a bare-string content; conversation turns
            # are always block lists. That is the user-vs-preamble discriminator.
            if role == "system" or not isinstance(content, list):
                continue
            if role == "user":
                text = "\n".join(b.get("text", "") for b in content
                                 if isinstance(b, dict) and b.get("type") == "text")
                qs = _QUERY.findall(text)
                text = "\n\n".join(qs) if qs else text
                if text.strip():
                    records.append(UserMessage(text, ts))
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    if b.get("text", "").strip():
                        records.append(AssistantMessage(b["text"], ts))
                elif t == "tool-call":
                    name = b.get("toolName") or "tool"
                    records.append(ToolCall(b.get("toolCallId", ""),
                                            INBOUND_NAMES.get(name, name),
                                            b.get("args") or {}, ts))
                elif t == "tool-result":
                    out = b.get("result")
                    if not isinstance(out, str):
                        out = json.dumps(out)   # MCP results come back as objects
                    # ponytail: Cursor stores no isError flag, so an error is
                    # whatever the tool layer prefixed with "Error:". Ceiling: a
                    # tool whose real output starts that way reads as failed.
                    records.append(ToolResult(b.get("toolCallId", ""), out, ts,
                                              out.startswith("Error:")))
                # reasoning / redacted-reasoning -> dropped (provider-owned)

        s = Session("cursor", meta.get("agentId") or hdr.get("agentId") or d.name,
                    meta.get("cwd", ""), records, started_at=ts)
        title = meta.get("title") or hdr.get("name") or ""
        if title and title != "New Agent":
            s.extra["title"] = title
        return s


register(CursorAdapter())
