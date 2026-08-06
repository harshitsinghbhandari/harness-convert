"""Hermetic round-trip + invariant check. Run: python3 tests/test_hconv.py

No dependency on real ~/.codex / ~/.claude data: we redirect the adapters' store
paths to a temp dir, write a synthetic session through each harness, read it back,
and assert the structural invariants that native resume actually requires.
"""
import io
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hconv
from hconv import (AssistantMessage, Session, ToolCall, ToolResult, UserMessage,
                   synthesize_missing_results)
from hconv.enrich import enrich
from hconv.adapters import claude as claude_mod
from hconv.adapters import codex as codex_mod
from hconv.adapters import opencode as opencode_mod
from hconv.adapters import cursor as cursor_mod
from hconv.adapters import grok as grok_mod
import hashlib
import sqlite3
import uuid

CWD = "/Users/x/proj"


def sample():
    # a session that died mid-tool-call (orphan c2), the escape-hatch normal case
    return Session("test", "1111-2222-3333-4444-5555", CWD, [
        UserMessage("fix the failing test", "2026-06-27T01:00:00Z"),
        AssistantMessage("looking now", "2026-06-27T01:00:01Z"),
        ToolCall("c1", "Bash", {"command": "pytest"}, "2026-06-27T01:00:02Z"),
        ToolResult("c1", "1 failed", "2026-06-27T01:00:03Z"),
        AssistantMessage("patching", "2026-06-27T01:00:04Z"),
        ToolCall("c2", "Edit", {"file": "x.py"}, "2026-06-27T01:00:05Z"),  # orphan
    ], git_branch="main", started_at="2026-06-27T01:00:00Z")


def test_tail_closed():
    recs = synthesize_missing_results(sample().records)
    orphans = {r.call_id for r in recs if isinstance(r, ToolCall)} - \
              {r.call_id for r in recs if isinstance(r, ToolResult)}
    assert not orphans, f"unclosed tool calls: {orphans}"
    print("PASS tail-closed: every ToolCall has a ToolResult")


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
    from hconv.common import truncate_payload, _pool, _est_freed
    recs, st = truncate_payload(_big_session().records, 20)
    assert st.freed_pct >= 20, f"freed {st.freed_pct:.1f}% < target 20%"
    # gentlest cap: one byte more would miss the target
    pool = _pool(_big_session().records)
    want = st.total * 20 / 100
    assert _est_freed(pool, st.cap) >= want, "cap must still free the target"
    assert _est_freed(pool, st.cap + 1) < want, "cap+1 must miss the target"
    # determinism: same inputs, same pct, same cap every time
    _, again = truncate_payload(_big_session().records, 20)
    assert again.cap == st.cap
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
    # pct=40 alone is met by the whale (100_000B) + image (55_000B, all-or-nothing)
    # without ever touching Write's 40_000B content field, so the gentlest-cap
    # search correctly leaves it untouched at pct=40; 60 forces the cap below
    # 40_000 so this test actually exercises dict-shape-preserving clipping.
    recs, _ = truncate_payload(_big_session().records, 60)
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


def test_est_freed_monotonic_and_search_cap_matches_bruteforce():
    """_est_freed must be non-increasing in cap (the precondition _search_cap's
    bisection needs), including right at each item's own cap == nbytes
    exclusion boundary: as cap approaches nbytes, TRIM_MARK's overhead can
    exceed the shrinking remainder, going negative, then jump back to 0 once
    the item is excluded outright, an increase that breaks monotonicity.
    Sweeps the FULL cap range (not a window carved away from the boundary)
    and checks _search_cap against a brute-force scan over every reachable
    `want`, not a few hand-picked values, on both single- and multi-item
    pools, including pools where several payloads share one crossing point."""
    from hconv.common import _est_freed, _search_cap

    def freed_table(pool, hi):
        # index i holds _est_freed(pool, i); index 0 unused, kept for 1-indexing.
        return [None] + [_est_freed(pool, cap) for cap in range(1, hi + 1)]

    def brute_best(table, want, hi):
        best = 1
        for cap in range(1, hi + 1):
            if table[cap] >= want:
                best = cap
        return best

    def check_pool(pool, label):
        hi = max(n for n, _, _, _ in pool) + 2   # past every item's own cutoff
        table = freed_table(pool, hi)
        prev = table[1]
        for cap in range(2, hi + 1):
            cur = table[cap]
            assert cur <= prev, \
                f"{label}: _est_freed increased at cap={cap}: {prev} -> {cur}"
            prev = cur
        assert table[hi] == 0, f"{label}: fully-excluded cap should free 0"
        for want in range(1, table[1] + 5):
            expected = brute_best(table, want, hi)
            got = _search_cap(pool, want)
            assert got == expected, \
                f"{label}: _search_cap(want={want}) = {got}, brute force = {expected}"

    # single item, small enough for a fast exhaustive sweep, but the sweep
    # still runs cap all the way past nbytes so the exclusion boundary itself
    # is exercised, not carved around.
    check_pool([(300, False, None, None)], "single-300")

    # the reviewer's exact multi-item repro: at want=97 the true largest
    # valid cap is 97 itself, not some smaller cap forced by a spurious dip.
    check_pool([(97, False, None, None), (222, False, None, None)],
               "reviewer-97-222")

    # several payloads sharing one exact crossing point: two items excluded
    # at the same cap simultaneously.
    check_pool([(97, False, None, None), (222, False, None, None),
                (222, False, None, None), (400, False, None, None)],
               "shared-crossing")

    print("PASS est-freed-monotonic: full-range sweep including the "
          "cap==nbytes boundary, _search_cap matches brute force over every "
          "reachable want, single- and multi-item pools with shared crossings")


def test_codex_write_invariants(tmp):
    codex_mod.SESSIONS = Path(tmp) / "codex"
    codex_mod.INDEX = Path(tmp) / "codex_index.jsonl"
    s = sample(); s.records = synthesize_missing_results(s.records)
    dest = codex_mod.CodexAdapter().write(s, CWD)
    lines = [json.loads(l) for l in dest.read_text().splitlines()]
    assert lines[0]["type"] == "session_meta", "first line must be session_meta"
    meta = lines[0]["payload"]
    assert meta["cwd"] == CWD, "cwd not rewritten"
    # codex >= 0.144 builds its threads-db row from session_meta; model_provider
    # is NOT NULL there and an absent value backfills as "" which kills TUI
    # resume with: Model provider `` not found
    assert meta.get("model_provider"), "session_meta needs a model_provider"
    assert meta.get("session_id") == meta["id"], "session_id must mirror id"
    assert meta.get("source") and meta.get("thread_source"), "0.144 identity fields missing"
    calls = [l["payload"]["call_id"] for l in lines if l["payload"].get("type") == "function_call"]
    outs = {l["payload"]["call_id"] for l in lines if l["payload"].get("type") == "function_call_output"}
    assert all(c in outs for c in calls), "unpaired function_call (resume would reject)"
    evs = [l for l in lines if l["type"] == "event_msg"]
    assert any(e["payload"]["type"] == "user_message" for e in evs), "no scrollback events"
    print(f"PASS codex-write: session_meta + {len(calls)} paired calls + {len(evs)} render events")


def test_codex_tool_cards(tmp):
    codex_mod.SESSIONS = Path(tmp) / "codex_cards"
    codex_mod.INDEX = Path(tmp) / "codex_cards_index.jsonl"
    s = sample(); s.records = synthesize_missing_results(s.records)
    dest = codex_mod.CodexAdapter().write(s, CWD)
    ev = [l["payload"]["type"] for l in
          (json.loads(x) for x in dest.read_text().splitlines())
          if l["type"] == "event_msg"]
    assert "exec_command_end" in ev, "Bash tool should render a shell card"
    assert "patch_apply_end" in ev, "Edit tool should render a patch card"
    print(f"PASS codex-cards: exec_command_end + patch_apply_end emitted ({ev})")


def test_claude_write_invariants(tmp):
    claude_mod.PROJECTS = Path(tmp) / "claude"
    s = sample(); s.records = synthesize_missing_results(s.records)
    dest = claude_mod.ClaudeAdapter().write(s, CWD)
    assert dest.stem == s.session_id, "filename stem must equal sessionId"
    rows = [json.loads(l) for l in dest.read_text().splitlines()]
    roots = [r for r in rows if r["parentUuid"] is None]
    assert len(roots) == 1, f"expected 1 tree root, got {len(roots)}"
    assert all(r["cwd"] == CWD and r["sessionId"] == s.session_id for r in rows), "identity not rewritten"
    uses, results = set(), set()
    for r in rows:
        c = r["message"]["content"]
        if isinstance(c, list):
            for b in c:
                if b.get("type") == "tool_use": uses.add(b["id"])
                if b.get("type") == "tool_result": results.add(b["tool_use_id"])
    assert uses <= results, "unmatched tool_use (resume would reject)"
    print(f"PASS claude-write: single-root tree, {len(uses)} tool_use all matched")


def test_roundtrip_preserves_conversation(tmp):
    # codex.write -> codex.read should preserve the visible conversation
    codex_mod.SESSIONS = Path(tmp) / "codex2"
    codex_mod.INDEX = Path(tmp) / "codex2_index.jsonl"
    s = sample(); s.records = synthesize_missing_results(s.records)
    a = codex_mod.CodexAdapter()
    back = a.read(a.write(s, CWD))
    texts_in = [r.text for r in s.records if isinstance(r, (UserMessage, AssistantMessage))]
    texts_out = [r.text for r in back.records if isinstance(r, (UserMessage, AssistantMessage))]
    assert texts_in == texts_out, f"text drift\n in={texts_in}\nout={texts_out}"
    print(f"PASS round-trip: {len(texts_in)} messages survived write->read intact")


def test_title_enrichment(tmp):
    # claude -> codex: title should land in the index the picker reads
    codex_mod.SESSIONS = Path(tmp) / "codex_t"
    codex_mod.INDEX = Path(tmp) / "codex_t_index.jsonl"
    s = sample(); s.extra["title"] = "Fix the failing test"
    enrich("claude", "codex", s)
    codex_mod.CodexAdapter().write(s, CWD)
    idx = [json.loads(l) for l in codex_mod.INDEX.read_text().splitlines()]
    assert any(e["thread_name"] == "Fix the failing test" and e["id"] == s.session_id
               for e in idx), f"title not in codex index: {idx}"

    # codex -> claude: title should land as an ai-title row
    claude_mod.PROJECTS = Path(tmp) / "claude_t"
    s2 = sample(); s2.extra["title"] = "Fix the failing test"
    enrich("codex", "claude", s2)
    dest = claude_mod.ClaudeAdapter().write(s2, CWD)
    rows = [json.loads(l) for l in dest.read_text().splitlines()]
    assert any(r.get("type") == "ai-title" and r.get("aiTitle") == "Fix the failing test"
               for r in rows), "title not in claude ai-title row"

    # a pair with NO enricher stays common-only (no surplus leaks)
    # codex->cursor: cursor is read-only and has no enricher registered into it,
    # unlike codex->codex, which this same task now registers on purpose.
    s3 = sample(); s3.extra["title"] = "should not appear"
    enrich("codex", "cursor", s3)  # unregistered pair
    assert "out" not in s3.extra, "surplus leaked for an unregistered pair"

    # claude -> grok: title lands as generated_title on summary.json
    grok_mod.SESSIONS = Path(tmp) / "grok_t"
    s4 = sample(); s4.extra["title"] = "Fix the failing test"
    enrich("claude", "grok", s4)
    dest = grok_mod.GrokAdapter().write(s4, CWD)
    summary = json.loads((dest / "summary.json").read_text())
    assert summary["generated_title"] == "Fix the failing test", summary
    assert summary["session_summary"] == "Fix the failing test", summary
    print("PASS title-enrichment: carried both ways, absent for unregistered pair")


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


def test_opencode_write_invariants(tmp):
    # write() emits the {info, messages} doc `opencode import` validates. Assert
    # the invariants that import (reverse-engineered) actually enforces.
    opencode_mod.IMPORTS = Path(tmp) / "oc_imports"
    s = sample(); s.records = synthesize_missing_results(s.records)
    dest = opencode_mod.OpenCodeAdapter().write(s, CWD)
    doc = json.loads(dest.read_text())
    # opencode requires ses_-prefixed session ids (`opencode -s` validates the
    # prefix; import does not, and an invalid id imports fine but can't resume)
    oc_id = doc["info"]["id"]
    assert oc_id.startswith("ses_"), f"opencode session id must be ses_-prefixed: {oc_id}"
    assert s.extra.get("dest_session_id") == oc_id, "cli needs the converted id for the resume hint"
    doc2 = json.loads(opencode_mod.OpenCodeAdapter().write(s, CWD).read_text())
    assert doc2["info"]["id"] == oc_id, "id must be deterministic so re-import upserts"
    assert doc["info"]["directory"] == CWD, "directory not rewritten to dest cwd"
    assert doc["info"]["summary"] == {"additions": 0, "deletions": 0, "files": 0}
    pids, tools = set(), []
    for m in doc["messages"]:
        info = m["info"]
        assert info["sessionID"] == oc_id
        if info["role"] == "assistant":
            assert "parentID" in info, "assistant message needs parentID (import rejects otherwise)"
        for p in m["parts"]:
            assert p["id"] not in pids, f"duplicate part id {p['id']}"
            pids.add(p["id"])
            assert p["messageID"] == info["id"], "part.messageID must match its message"
            if p["type"] == "tool":
                tools.append(p["state"]["status"])
    assert "completed" in tools, "the closed Bash call should be a completed tool part"
    assert "error" in tools, "the synthesized orphan result should be an error tool part"
    print(f"PASS opencode-write: id-preserving import doc, {len(tools)} tool parts ({tools})")


def _make_oc_db(path, sid, cwd):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE session(id TEXT, directory TEXT, title TEXT, time_created INT)")
    con.execute("CREATE TABLE message(id TEXT, session_id TEXT, data TEXT, time_created INT)")
    con.execute("CREATE TABLE part(id TEXT, message_id TEXT, session_id TEXT, data TEXT, time_created INT)")
    con.execute("INSERT INTO session VALUES(?,?,?,?)", (sid, cwd, "Fix the failing test", 1777000000000))
    con.execute("INSERT INTO message VALUES(?,?,?,?)", ("m1", sid, json.dumps({"role": "user"}), 1))
    con.execute("INSERT INTO message VALUES(?,?,?,?)", ("m2", sid, json.dumps({"role": "assistant"}), 2))
    con.execute("INSERT INTO part VALUES(?,?,?,?,?)", ("p1", "m1", sid,
                json.dumps({"type": "text", "text": "fix the failing test"}), 1))
    con.execute("INSERT INTO part VALUES(?,?,?,?,?)", ("p2", "m2", sid,
                json.dumps({"type": "reasoning", "text": "secret"}), 1))  # dropped
    con.execute("INSERT INTO part VALUES(?,?,?,?,?)", ("p3", "m2", sid,
                json.dumps({"type": "tool", "tool": "bash", "callID": "c1",
                            "state": {"status": "error", "input": {"command": "pytest"},
                                      "output": "boom", "time": {"start": 2, "end": 3}}}), 2))
    con.commit(); con.close()


def test_opencode_read(tmp):
    sid = "ses_readtest0000000000000001"
    db = Path(tmp) / "oc_read.db"
    _make_oc_db(str(db), sid, CWD)
    opencode_mod.DB = db
    a = opencode_mod.OpenCodeAdapter()
    s = a.read(a.locate(CWD))                      # locate by cwd, then read
    assert s.session_id == sid and s.cwd == CWD, "identity not read back"
    assert s.extra.get("title") == "Fix the failing test", "title not lifted to extra"
    kinds = [type(r).__name__ for r in s.records]
    assert kinds == ["UserMessage", "ToolCall", "ToolResult"], f"reasoning not dropped / tool not split: {kinds}"
    tc = next(r for r in s.records if isinstance(r, ToolCall))
    tr = next(r for r in s.records if isinstance(r, ToolResult))
    assert tc.call_id == tr.call_id == "c1" and tr.is_error and tr.output == "boom", "tool call/result mispaired"
    print(f"PASS opencode-read: {len(s.records)} records, reasoning dropped, error tool split correctly")


def _make_cursor_session(d, agent_id, messages, meta_json=None):
    """A Cursor session dir: content-addressed blobs + a protobuf root listing the
    child hashes in order, exactly as Cursor lays it out on disk."""
    d.mkdir(parents=True)
    if meta_json is not None:
        (d / "meta.json").write_text(json.dumps(meta_json))
    blobs = {}

    def put(data: bytes) -> str:
        h = hashlib.sha256(data).hexdigest()
        blobs[h] = data
        return h

    # root: repeated field 1 (wire type 2, always 32 bytes) = child sha256s
    root = b"".join(b"\x0a\x20" + bytes.fromhex(put(json.dumps(m).encode()))
                    for m in messages)
    root += b"\x50\x01"                            # field 10 varint: ignored surplus
    root_id = put(root)
    con = sqlite3.connect(d / "store.db")
    con.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    con.executemany("INSERT INTO blobs VALUES(?,?)", blobs.items())
    hdr = json.dumps({"agentId": agent_id, "latestRootBlobId": root_id,
                      "name": "Hello There", "createdAt": 1785695807732}).encode()
    con.execute("INSERT INTO meta VALUES('0',?)", (hdr.hex(),))
    con.commit(); con.close()


def test_cursor_read(tmp):
    chats = Path(tmp) / "cursor_chats"
    cursor_mod.CHATS = chats
    proj = chats / hashlib.md5(CWD.encode()).hexdigest()
    _make_cursor_session(proj / "aaaaaaaa-0000-0000-0000-000000000001", "sess-1", [
        {"role": "system", "content": "You are an AI coding assistant"},
        # the environment preamble: the only user turn stored as a bare string
        {"role": "user", "content": "<user_info>\nOS Version: darwin\n</user_info>"},
        {"role": "user", "content": [
            {"type": "text", "text": "<system_reminder>noise</system_reminder>"},
            {"type": "text", "text": "<timestamp>x</timestamp>\n<user_query>\n"
                                     "fix the failing test\n</user_query>"}]},
        {"role": "assistant", "content": [
            {"type": "reasoning", "text": "secret", "signature": "sig"},   # dropped
            {"type": "text", "text": "looking now"},
            {"type": "tool-call", "toolCallId": "call-1\nfc_1", "toolName": "Shell",
             "args": {"command": "pytest"}}]},
        {"role": "tool", "content": [
            {"type": "tool-result", "toolCallId": "call-1\nfc_1", "toolName": "Shell",
             "result": "1 failed"}]},
        {"role": "assistant", "content": [
            {"type": "redacted-reasoning", "data": "zzz"},                 # dropped
            {"type": "tool-call", "toolCallId": "call-2\nfc_2",
             "toolName": "CallMcpTool", "args": {"name": "codegraph"}}]},
        {"role": "tool", "content": [
            {"type": "tool-result", "toolCallId": "call-2\nfc_2",
             "toolName": "CallMcpTool", "result": "Error: Tool execution failed"}]},
    ], meta_json={"schemaVersion": 1, "createdAtMs": 1785695807732,
                  "hasConversation": True, "title": "Hello There",
                  "updatedAtMs": 1785695950093, "cwd": CWD})
    # a subagent: store.db but NO meta.json. Newer, so it would win on recency.
    sub = proj / "bbbbbbbb-0000-0000-0000-000000000002"
    _make_cursor_session(sub, "sess-2", [
        {"role": "user", "content": [{"type": "text",
                                      "text": "<user_query>\nsub task\n</user_query>"}]}])

    a = cursor_mod.CursorAdapter()
    d = a.locate(CWD)                              # locate by cwd, then read
    assert d.name.startswith("aaaaaaaa"), f"locate picked the subagent: {d}"
    s = a.read(d)
    assert s.cwd == CWD and s.session_id == "sess-1", "identity not read back"
    assert s.extra.get("title") == "Hello There", "title not lifted to extra"
    assert s.started_at == "2026-08-02T18:36:47.732000Z", s.started_at
    kinds = [type(r).__name__ for r in s.records]
    assert kinds == ["UserMessage", "AssistantMessage", "ToolCall", "ToolResult",
                     "ToolCall", "ToolResult"], f"system/preamble/reasoning kept: {kinds}"
    assert s.records[0].text == "fix the failing test", \
        f"<user_query> not unwrapped: {s.records[0].text!r}"
    assert s.records[2].name == "Bash", "Shell not mapped to Bash"
    assert s.records[2].call_id == s.records[3].call_id == "call-1\nfc_1", "mispaired"
    assert s.records[3].output == "1 failed" and not s.records[3].is_error
    assert s.records[5].is_error, "Error:-prefixed result should flag is_error"

    # a subagent transcript is unreachable through locate() but readable directly
    assert a.locate(CWD, "aaaaaaaa").name.startswith("aaaaaaaa"), "substring id lookup"
    try:
        a.locate(CWD, "bbbbbbbb"); raise AssertionError("locate found a subagent")
    except SystemExit:
        pass
    assert [r.text for r in a.read(sub).records] == ["sub task"], "subagent unreadable"

    # read-only: no write path exists, and --to can never offer it
    for call in (lambda: a.write(s, CWD), lambda: a.dest_path(s, CWD)):
        try:
            call(); raise AssertionError("read-only adapter must refuse to write")
        except SystemExit as e:
            assert "read-only" in str(e), e
    assert not a.writable and "cursor" in hconv.known() and "cursor" not in hconv.writable()
    print(f"PASS cursor-read: {len(s.records)} records, subagent excluded, write refused")


def test_opencode_no_duplicate_output(tmp):
    # write() used to double-emit a ToolResult: once fused into its ToolCall's
    # tool part (state.output), and again as a bare text part. Pin the fix, and
    # pin that the orphan case (no matching ToolCall) still surfaces as text.
    opencode_mod.IMPORTS = Path(tmp) / "oc_no_dup"
    s = Session("test", "dup-test-0001", CWD, [
        UserMessage("do it", "2026-06-27T02:00:00Z"),
        AssistantMessage("ok", "2026-06-27T02:00:01Z"),
        ToolCall("c1", "Bash", {"command": "ls"}, "2026-06-27T02:00:02Z"),
        ToolResult("c1", "file.txt", "2026-06-27T02:00:03Z"),
        ToolResult("c2", "orphan-output", "2026-06-27T02:00:04Z"),  # orphan: no ToolCall
        AssistantMessage("done", "2026-06-27T02:00:05Z"),
    ], git_branch="main", started_at="2026-06-27T02:00:00Z")

    dest = opencode_mod.OpenCodeAdapter().write(s, CWD)
    raw = dest.read_text()
    assert raw.count("file.txt") == 1, \
        f"'file.txt' should appear exactly once in the written doc, found {raw.count('file.txt')}"

    doc = json.loads(raw)
    tool_parts, text_parts = [], []
    for m in doc["messages"]:
        for p in m["parts"]:
            if p["type"] == "tool":
                tool_parts.append(p)
            elif p["type"] == "text":
                text_parts.append(p)

    matched = next((p for p in tool_parts if p["state"].get("output") == "file.txt"), None)
    assert matched is not None, "fused ToolResult output missing from its tool part's state.output"
    assert not any(p["text"] == "file.txt" for p in text_parts), \
        "ToolResult output leaked as a duplicate bare text part"
    assert any(p["text"] == "orphan-output" for p in text_parts), \
        "orphan ToolResult (no matching ToolCall) should still be emitted as a text part"
    print("PASS opencode-no-duplicate-output: fused output not duplicated, orphan result still surfaced")


def test_claude_title_and_timestamps(tmp):
    proj = Path(tmp) / "claude_titles"
    claude_mod.PROJECTS = proj
    a = claude_mod.ClaudeAdapter()
    uid = [0]

    def user_row(text, ts=None, ts_ms=None):
        uid[0] += 1
        row = {"type": "user", "uuid": f"u{uid[0]}", "cwd": CWD, "gitBranch": "main",
               "message": {"role": "user", "content": text}}
        if ts is not None:
            row["timestamp"] = ts
        if ts_ms is not None:
            row["timestamp_ms"] = ts_ms
        return row

    def write_transcript(sid, rows):
        d = proj / claude_mod.enc(CWD)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{sid}.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return p

    # custom-title beats ai-title
    p = write_transcript("sid-a", [
        {"type": "custom-title", "customTitle": "Custom Title"},
        {"type": "ai-title", "aiTitle": "AI Title"},
        user_row("hello world", ts="2026-01-01T00:00:00.000Z"),
    ])
    s = a.read(p)
    assert s.extra["title"] == "Custom Title", f"customTitle should win, got {s.extra.get('title')!r}"

    # ai-title alone is used
    p = write_transcript("sid-b", [
        {"type": "ai-title", "aiTitle": "AI Title Only"},
        user_row("hello world", ts="2026-01-01T00:00:00.000Z"),
    ])
    s = a.read(p)
    assert s.extra["title"] == "AI Title Only", f"aiTitle should be used, got {s.extra.get('title')!r}"

    # neither present: fall back to first non-empty line of first user message,
    # with a leading well-formed <system-reminder> wrapper stripped
    wrapped = "<system-reminder>ignore me</system-reminder>\nActual first line\nsecond line"
    p = write_transcript("sid-c", [user_row(wrapped, ts="2026-01-01T00:00:00.000Z")])
    s = a.read(p)
    assert s.extra["title"] == "Actual first line", \
        f"well-formed wrapper should be stripped from fallback title, got {s.extra.get('title')!r}"
    assert s.records[0].text == wrapped, \
        "stripping for the title must never mutate the stored message body"

    # malformed wrapper (unclosed tag): fail closed, strip NOTHING
    malformed = "<system-reminder>\nunclosed forever\nmore lines"
    p = write_transcript("sid-d", [user_row(malformed, ts="2026-01-01T00:00:00.000Z")])
    s = a.read(p)
    assert s.extra["title"] == "<system-reminder>", \
        f"unclosed wrapper must fail closed (strip nothing), got {s.extra.get('title')!r}"
    assert s.records[0].text == malformed, "message body must not be mutated even on the fail-closed path"

    # title > 120 chars gets truncated to <=120 chars, ending in '...'
    long_line = "X" * 150
    p = write_transcript("sid-e", [user_row(long_line, ts="2026-01-01T00:00:00.000Z")])
    s = a.read(p)
    title = s.extra["title"]
    assert len(title) <= 120, f"title should be capped at 120 chars, got {len(title)}"
    assert title.endswith("..."), f"truncated title should end with '...', got {title!r}"
    assert title[:117] == long_line[:117], "truncated title should preserve the original prefix"

    # no timestamp, but timestamp_ms present: derive ISO-8601 UTC from it
    ts_ms = 1700000000000
    p = write_transcript("sid-f", [user_row("hi there", ts=None, ts_ms=ts_ms)])
    s = a.read(p)
    rec = s.records[0]
    expected_dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    assert rec.ts.startswith(expected_dt.strftime("%Y-%m-%dT%H:%M:%S")), \
        f"timestamp_ms not converted to matching ISO-8601 UTC ts: {rec.ts!r}"
    assert rec.ts.endswith("Z"), f"derived ts should be UTC ('Z'-suffixed), got {rec.ts!r}"

    # neither timestamp nor timestamp_ms: fall back to the transcript file's mtime
    p = write_transcript("sid-g", [user_row("hi again", ts=None, ts_ms=None)])
    s = a.read(p)
    rec = s.records[0]
    assert rec.ts, "record with no timestamp/timestamp_ms should still get a non-empty fallback ts"
    datetime.fromisoformat(rec.ts.replace("Z", "+00:00"))  # raises if unparsable

    print("PASS claude-title-and-timestamps: title precedence, stripping, truncation, ts fallbacks all correct")


def _codex_rollout(base, y, m, d, sid, cwd, start_ts, mtime_epoch):
    p = (base / f"{y:04d}" / f"{m:02d}" / f"{d:02d}" /
         f"rollout-{y:04d}-{m:02d}-{d:02d}T00-00-00-{sid}.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    meta = {"timestamp": start_ts, "type": "session_meta",
            "payload": {"id": sid, "session_id": sid, "cwd": cwd, "timestamp": start_ts}}
    p.write_text(json.dumps(meta) + "\n")
    os.utime(p, (mtime_epoch, mtime_epoch))
    return p


def test_codex_locate_newest_by_mtime(tmp):
    # locate() ranks candidate rollouts by file mtime (last write), not by
    # session_meta.timestamp (session start), and early-exits at the first cwd
    # match instead of scanning the whole tree. Pin that deliberate ordering.
    base = Path(tmp) / "codex_locate"
    codex_mod.SESSIONS = base
    target, other = "/proj/target", "/proj/other"
    now = time.time()

    # fA: matches cwd, OLDER session start, but the NEWEST mtime among matches
    fA = _codex_rollout(base, 2026, 1, 1, "aaaa1111", target, "2026-01-01T01:00:00Z", now - 100)
    # fB: matches cwd, LATER session start than fA, but an OLDER mtime than fA
    fB = _codex_rollout(base, 2026, 1, 2, "bbbb2222", target, "2026-01-03T23:00:00Z", now - 200)
    # fC: newest mtime overall, but a non-matching cwd -> must be skipped
    fC = _codex_rollout(base, 2026, 1, 3, "cccc3333", other, "2026-01-02T12:00:00Z", now)

    a = codex_mod.CodexAdapter()
    got = a.locate(target)
    assert got == fA, f"expected newest-mtime cwd match {fA}, got {got}"

    meta_a, meta_b = a._meta(fA), a._meta(fB)
    assert meta_a["timestamp"] < meta_b["timestamp"], \
        "test setup must pin fA (chosen) as the OLDER session start than fB (not chosen)"

    try:
        a.locate("/nowhere/at/all")
        raise AssertionError("locate() should raise SystemExit when no cwd matches")
    except SystemExit:
        pass

    print("PASS codex-locate-newest-by-mtime: newest mtime wins over newer session start; not-found raises")


def test_grok_write_invariants(tmp):
    grok_mod.SESSIONS = Path(tmp) / "grok_write"
    s = sample()
    s.records = synthesize_missing_results(s.records)
    dest = grok_mod.GrokAdapter().write(s, CWD)
    assert dest.is_dir(), "grok dest is a session directory"
    for name in ("summary.json", "chat_history.jsonl", "updates.jsonl"):
        assert (dest / name).exists(), f"missing {name}"

    summary = json.loads((dest / "summary.json").read_text())
    sid = summary["info"]["id"]
    assert summary["info"]["cwd"] == CWD
    assert uuid.UUID(sid), "session id must be a UUID"
    assert s.extra.get("dest_session_id") == sid
    assert summary["chat_format_version"] == 1
    assert summary["current_model_id"]
    assert summary["num_chat_messages"] >= 1

    # Non-UUID source id maps deterministically.
    s2 = sample()
    s2.session_id = "not-a-uuid"
    s2.records = synthesize_missing_results(s2.records)
    a = grok_mod.GrokAdapter()
    d1 = a.write(s2, CWD)
    d2 = a.write(s2, CWD)
    assert d1 == d2 and d1.name == s2.extra["dest_session_id"]
    assert uuid.UUID(d1.name)

    rows = [json.loads(l) for l in (dest / "chat_history.jsonl").read_text().splitlines()]
    kinds = [r["type"] for r in rows]
    assert "user" in kinds and "assistant" in kinds and "tool_result" in kinds
    calls, results = set(), set()
    for r in rows:
        if r["type"] == "assistant":
            for tc in r.get("tool_calls") or []:
                calls.add(tc["id"])
                assert isinstance(tc["arguments"], str), "arguments must be a JSON string"
        if r["type"] == "tool_result":
            results.add(r["tool_call_id"])
    assert calls and calls <= results, f"unpaired tool calls: {calls - results}"

    updates = [json.loads(l) for l in (dest / "updates.jsonl").read_text().splitlines()]
    update_kinds = {u["params"]["update"]["sessionUpdate"] for u in updates}
    assert "user_message_chunk" in update_kinds
    assert "agent_message_chunk" in update_kinds
    assert "tool_call" in update_kinds
    assert "tool_call_update" in update_kinds
    assert all(u["params"]["sessionId"] == sid for u in updates)
    print(f"PASS grok-write: dir layout, UUID sid, {len(calls)} paired tools, "
          f"{len(updates)} update events")


def test_grok_roundtrip(tmp):
    grok_mod.SESSIONS = Path(tmp) / "grok_rt"
    s = sample()
    s.extra["title"] = "Fix the failing test"
    s.records = synthesize_missing_results(s.records)
    a = grok_mod.GrokAdapter()
    back = a.read(a.write(s, CWD))
    texts_in = [r.text for r in s.records if isinstance(r, (UserMessage, AssistantMessage))]
    texts_out = [r.text for r in back.records if isinstance(r, (UserMessage, AssistantMessage))]
    assert texts_in == texts_out, f"text drift\n in={texts_in}\nout={texts_out}"
    assert back.extra.get("title") == "Fix the failing test"
    assert back.cwd == CWD
    # tool names remapped outbound to the Claude-ish middle
    names = [r.name for r in back.records if isinstance(r, ToolCall)]
    assert "Bash" in names or "Edit" in names, names
    print(f"PASS grok-roundtrip: {len(texts_in)} messages, title preserved")


def test_grok_read_and_locate(tmp):
    base = Path(tmp) / "grok_loc"
    grok_mod.SESSIONS = base
    a = grok_mod.GrokAdapter()
    # Build two sessions under the same cwd via write, then tweak mtimes/titles.
    s_old = sample()
    s_old.session_id = "11111111-1111-1111-1111-111111111111"
    s_old.extra["title"] = "older"
    s_old.records = synthesize_missing_results(s_old.records)
    d_old = a.write(s_old, CWD)

    s_new = sample()
    s_new.session_id = "22222222-2222-2222-2222-222222222222"
    s_new.extra["title"] = "newer"
    s_new.records = [
        UserMessage("latest work", "2026-06-28T01:00:00Z"),
        AssistantMessage("ok", "2026-06-28T01:00:01Z"),
    ]
    s_new.started_at = "2026-06-28T01:00:00Z"
    d_new = a.write(s_new, CWD)

    # Force summary updated_at so locate prefers d_new.
    for d, ts in ((d_old, "2026-06-27T01:00:00Z"), (d_new, "2026-06-28T02:00:00Z")):
        summary = json.loads((d / "summary.json").read_text())
        summary["updated_at"] = ts
        summary["last_active_at"] = ts
        (d / "summary.json").write_text(json.dumps(summary))

    got = a.locate(CWD)
    assert got == d_new, f"locate should pick newest updated_at, got {got}"
    got_id = a.locate(CWD, "22222222-2222-2222-2222-222222222222")
    assert got_id == d_new

    # Synthetic chat_history with scaffolding + user_query + reasoning drop.
    sid = "33333333-3333-3333-3333-333333333333"
    d = base / grok_mod.enc(CWD) / sid
    d.mkdir(parents=True)
    (d / "summary.json").write_text(json.dumps({
        "info": {"id": sid, "cwd": CWD},
        "generated_title": "Hello There",
        "created_at": "2026-06-27T00:00:00Z",
        "updated_at": "2026-06-27T00:00:00Z",
        "num_messages": 0, "num_chat_messages": 0,
        "current_model_id": "grok-4.5", "chat_format_version": 1,
    }))
    chat = [
        {"type": "system", "content": "You are Grok"},
        {"type": "user", "content": [{"type": "text", "text": "<user_info>\nOS\n</user_info>"}]},
        {"type": "user", "content": [{"type": "text",
                                      "text": "<user_query>\nfix the failing test\n</user_query>"}],
         "prompt_index": 0},
        {"type": "reasoning", "id": "rs_x", "summary": [{"type": "summary_text", "text": "secret"}],
         "encrypted_content": "zzz"},
        {"type": "assistant", "content": "looking now",
         "tool_calls": [{"id": "call-1", "name": "run_terminal_command",
                         "arguments": "{\"command\": \"pytest\"}"}]},
        {"type": "tool_result", "tool_call_id": "call-1", "content": "1 failed"},
        {"type": "assistant", "content": "",
         "tool_calls": [{"id": "call-2", "name": "search_replace",
                         "arguments": "{\"file\": \"x.py\"}"}]},
        {"type": "tool_result", "tool_call_id": "call-2",
         "content": "Error: apply failed"},
    ]
    (d / "chat_history.jsonl").write_text("".join(json.dumps(r) + "\n" for r in chat))
    s = a.read(d)
    assert s.extra.get("title") == "Hello There"
    kinds = [type(r).__name__ for r in s.records]
    assert kinds == ["UserMessage", "AssistantMessage", "ToolCall", "ToolResult",
                     "ToolCall", "ToolResult"], kinds
    assert s.records[0].text == "fix the failing test"
    assert s.records[2].name == "Bash" and s.records[2].input.get("command") == "pytest"
    assert s.records[4].name == "Edit"
    assert s.records[5].is_error
    print("PASS grok-read-and-locate: newest wins, scaffolding/reasoning dropped, tools mapped")


def test_list_sessions(tmp):
    # Claude: multiple jsonl files, newest-first, limit respected, titles peeked.
    proj = Path(tmp) / "claude_list"
    claude_mod.PROJECTS = proj
    d = proj / claude_mod.enc(CWD)
    d.mkdir(parents=True)
    now = time.time()
    for i, (sid, title, age) in enumerate([
        ("sid-old", "Older title", 300),
        ("sid-mid", "Mid title", 200),
        ("sid-new", "Newest title", 100),
    ]):
        p = d / f"{sid}.jsonl"
        rows = [{"type": "custom-title", "customTitle": title},
                {"type": "user", "uuid": f"u{i}", "cwd": CWD,
                 "message": {"role": "user", "content": "hi"},
                 "timestamp": "2026-01-01T00:00:00.000Z"}]
        p.write_text("".join(json.dumps(r) + "\n" for r in rows))
        os.utime(p, (now - age, now - age))

    a = claude_mod.ClaudeAdapter()
    refs = a.list_sessions(CWD, limit=2)
    assert len(refs) == 2, refs
    assert [r.session_id for r in refs] == ["sid-new", "sid-mid"]
    assert refs[0].title == "Newest title"
    assert a.list_sessions(CWD, limit=0) == []
    assert a.list_sessions("/no/such/cwd", limit=5) == []
    # locate still returns the newest
    assert a.locate(CWD).stem == "sid-new"

    # Grok: list_sessions shares the same newest-first order as locate.
    grok_mod.SESSIONS = Path(tmp) / "grok_list"
    ga = grok_mod.GrokAdapter()
    for sid, title, ts in [
        ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "A", "2026-06-27T01:00:00Z"),
        ("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "B", "2026-06-28T01:00:00Z"),
        ("cccccccc-cccc-cccc-cccc-cccccccccccc", "C", "2026-06-29T01:00:00Z"),
    ]:
        s = sample()
        s.session_id = sid
        s.extra["title"] = title
        s.started_at = ts
        dest = ga.write(s, CWD)
        summary = json.loads((dest / "summary.json").read_text())
        summary["updated_at"] = ts
        summary["last_active_at"] = ts
        (dest / "summary.json").write_text(json.dumps(summary))
    grefs = ga.list_sessions(CWD, limit=2)
    assert [r.session_id for r in grefs] == [
        "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ]
    assert grefs[0].title == "C"
    print("PASS list-sessions: claude+grok newest-first, limit, titles")


def test_ui_helpers():
    from hconv import ui
    import io
    from unittest import mock

    # HC_NO_INTERACTIVE forces can_interact off even if TTYs look real.
    with mock.patch.dict(os.environ, {"HC_NO_INTERACTIVE": "1"}, clear=False):
        assert ui.can_interact(True) is False
    with mock.patch.dict(os.environ, {"HC_NO_INTERACTIVE": ""}, clear=False):
        with mock.patch.object(sys, "stdin") as stdin, mock.patch.object(sys, "stdout") as stdout:
            stdin.isatty.return_value = True
            stdout.isatty.return_value = True
            assert ui.can_interact(True) is True
            assert ui.can_interact(False) is False
            stdin.isatty.return_value = False
            assert ui.can_interact(True) is False

    # Numbered picker: empty input → default; "2" → index 1; "q" → SystemExit.
    opts = ["alpha", "beta", "gamma"]
    with mock.patch("builtins.input", return_value=""):
        assert ui._pick_numbered(opts, default=1) == 1
    with mock.patch("builtins.input", return_value="2"):
        assert ui._pick_numbered(opts, default=0) == 1
    with mock.patch("builtins.input", return_value="q"):
        try:
            ui._pick_numbered(opts, default=0)
            raise AssertionError("expected SystemExit on q")
        except SystemExit:
            pass

    with mock.patch("builtins.input", return_value="y"):
        assert ui.confirm("go?", default=False) is True
    with mock.patch("builtins.input", return_value=""):
        assert ui.confirm("go?", default=False) is False
        assert ui.confirm("go?", default=True) is True

    # NO_COLOR disables color escapes.
    with mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False):
        assert ui.use_color() is False
        assert ui.bold("x") == "x"

    print("PASS ui-helpers: interact flags, numbered pick, confirm, NO_COLOR")


def test_cli_noninteractive_convert(tmp):
    """Fully-flagged convert still works with no TTY / no prompts."""
    from hconv.adapters import claude as claude_mod
    from hconv.adapters import codex as codex_mod
    import hconv.cli as cli_mod

    claude_mod.PROJECTS = Path(tmp) / "claude"
    codex_mod.SESSIONS = Path(tmp) / "codex"
    codex_mod.INDEX = Path(tmp) / "codex_index.jsonl"
    # Seed a claude session via write from sample.
    s = sample()
    s.records = synthesize_missing_results(s.records)
    s.extra["title"] = "Fix the failing test"
    claude_mod.ClaudeAdapter().write(s, CWD)

    buf = io.StringIO()
    with mock.patch.dict(os.environ, {"HC_NO_INTERACTIVE": "1"}, clear=False):
        with mock.patch("sys.stdout", buf):
            cli_mod.run_convert(
                from_h="claude", to_h="codex", cwd=CWD, dest_cwd=CWD,
                session_id=None, write=False, yes=False,
                interactive=False, pick_limit=10,
            )
    out = buf.getvalue()
    assert "from" in out and "codex" in out
    assert "dry run" in out
    assert "WROTE" not in out

    buf2 = io.StringIO()
    with mock.patch("sys.stdout", buf2):
        cli_mod.run_convert(
            from_h="claude", to_h="codex", cwd=CWD, dest_cwd=CWD,
            session_id=None, write=False, yes=True,
            interactive=False, pick_limit=10,
        )
    out2 = buf2.getvalue()
    assert "WROTE" in out2
    assert "codex resume" in out2
    print("PASS cli-noninteractive-convert: dry-run and -y write paths")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_tail_closed()
        test_truncate_meets_budget()
        test_truncate_preserves_structure()
        test_truncate_inputs_stay_dicts()
        test_truncate_images_dropped()
        test_truncate_shortfall_and_utf8()
        test_est_freed_monotonic_and_search_cap_matches_bruteforce()
        test_codex_write_invariants(tmp)
        test_codex_tool_cards(tmp)
        test_claude_write_invariants(tmp)
        test_roundtrip_preserves_conversation(tmp)
        test_title_enrichment(tmp)
        test_same_harness_title_survives()
        test_convert_truncate_new_session(tmp)
        test_opencode_write_invariants(tmp)
        test_opencode_read(tmp)
        test_cursor_read(tmp)
        test_opencode_no_duplicate_output(tmp)
        test_claude_title_and_timestamps(tmp)
        test_codex_locate_newest_by_mtime(tmp)
        test_grok_write_invariants(tmp)
        test_grok_roundtrip(tmp)
        test_grok_read_and_locate(tmp)
        test_list_sessions(tmp)
        test_ui_helpers()
        test_cli_noninteractive_convert(tmp)
    print("\nALL PASS")
