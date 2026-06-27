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

CWD = "/Users/x/proj"


def sample():
    # a session that died mid-tool-call (orphan c2) — the escape-hatch normal case
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
    assert lines[0]["payload"]["cwd"] == CWD, "cwd not rewritten"
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


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_tail_closed()
        test_codex_write_invariants(tmp)
        test_codex_tool_cards(tmp)
        test_claude_write_invariants(tmp)
        test_roundtrip_preserves_conversation(tmp)
        test_title_enrichment(tmp)
    print("\nALL PASS")
