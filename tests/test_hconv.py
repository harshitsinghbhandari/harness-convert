"""Hermetic round-trip + invariant check. Run: python3 tests/test_hconv.py

No dependency on real ~/.codex / ~/.claude data: we redirect the adapters' store
paths to a temp dir, write a synthetic session through each harness, read it back,
and assert the structural invariants that native resume actually requires.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hconv
from hconv import (AssistantMessage, Session, ToolCall, ToolResult, UserMessage,
                   synthesize_missing_results)
from hconv.enrich import enrich
from hconv.adapters import claude as claude_mod
from hconv.adapters import codex as codex_mod
from hconv.adapters import opencode as opencode_mod
import sqlite3

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
    s3 = sample(); s3.extra["title"] = "should not appear"
    enrich("codex", "codex", s3)  # unregistered pair
    assert "out" not in s3.extra, "surplus leaked for an unregistered pair"
    print("PASS title-enrichment: carried both ways, absent for unregistered pair")


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


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_tail_closed()
        test_codex_write_invariants(tmp)
        test_codex_tool_cards(tmp)
        test_claude_write_invariants(tmp)
        test_roundtrip_preserves_conversation(tmp)
        test_title_enrichment(tmp)
        test_opencode_write_invariants(tmp)
        test_opencode_read(tmp)
    print("\nALL PASS")
