#!/usr/bin/env python3
"""hc: relocate a coding-agent session across harnesses.

Escape hatch: your harness hit a wall (rate limit, outage) mid-task. Move the
session to a live harness and keep going. Reads transcripts off disk; the source
harness does NOT need to be running or your quota intact.

    hc --from claude --to codex            # move latest Claude session here -> Codex
    hc --from codex  --to claude <id>      # a specific session
    hc --from claude --to codex --cwd DIR  # source/dest folder (default: pwd)
    hc list --from codex                   # newest convertible sessions here
    hc list --from claude -n 5             # newest 5 (id / time / title)

cursor is read-only: it can be a --from, never a --to. grok is full R/W.

By default prints what it WOULD do; pass --write to actually create the file.
"""
import argparse
import os
import sys
from datetime import datetime, timezone

from hconv import convert, get, known, writable


def cmd_convert(a):
    session, dest = convert(a.from_harness, a.to, a.cwd, a.dest_cwd or a.cwd,
                            session_id=a.session_id, write=a.write)
    n_tool = sum(1 for r in session.records if type(r).__name__ == "ToolCall")
    print(f"from   : {a.from_harness}  ({session.session_id})")
    print(f"to     : {a.to}")
    print(f"cwd    : {a.dest_cwd or a.cwd}")
    print(f"records: {len(session.records)}  ({n_tool} tool calls)")
    print(f"dest   : {dest}")
    if a.write:
        # a dest harness may map the id to its own format (opencode: ses_...)
        sid = session.extra.get("dest_session_id", session.session_id)
        cwd = a.dest_cwd or a.cwd
        resume = {"codex": f"codex resume {sid}",
                  "claude": f"claude --resume {sid}",
                  "opencode": f"opencode import {dest} && opencode -s {sid}",
                  "grok": f"grok --resume {sid}"}[a.to]
        print(f"\nWROTE. resume with:\n  cd {cwd} && {resume}")
    else:
        print("\n(dry run; pass --write to create it)")


def _fmt_mtime(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "?"


def cmd_list(a):
    adapter = get(a.from_harness)
    refs = adapter.list_sessions(a.cwd, limit=a.n)
    if not refs:
        print(f"no {a.from_harness} sessions found for {a.cwd}")
        return
    print(f"{a.from_harness} sessions for {a.cwd}  (newest first, {len(refs)} shown)")
    id_w = max(len(r.session_id) for r in refs)
    id_w = max(id_w, 2)
    for r in refs:
        title = r.title.replace("\n", " ").strip()
        if len(title) > 60:
            title = title[:57].rstrip() + "..."
        line = f"  {r.session_id:<{id_w}}  {_fmt_mtime(r.mtime)}"
        if title:
            line += f"  {title}"
        print(line)


def main():
    ap = argparse.ArgumentParser(prog="hc", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    def add_common(p):
        p.add_argument("--from", dest="from_harness", required=True,
                       choices=known(), help="source harness")
        p.add_argument("--cwd", default=os.getcwd(), help="source folder (default: pwd)")

    c = sub.add_parser("convert", help="move a session to another harness")
    add_common(c)
    # --to is writable-only: read-only harnesses (cursor) fail at argparse rather
    # than after a successful read.
    c.add_argument("--to", required=True, choices=writable(),
                   help="destination harness")
    c.add_argument("session_id", nargs="?", help="session id (default: latest for cwd)")
    c.add_argument("--dest-cwd", help="destination folder (default: same as --cwd)")
    c.add_argument("--write", action="store_true", help="actually write the file")
    c.set_defaults(func=cmd_convert)

    l = sub.add_parser("list", help="list recent convertible sessions for a cwd")
    add_common(l)
    l.add_argument("-n", "--n", type=int, default=10, metavar="N",
                   help="how many newest sessions to show (default: 10)")
    l.set_defaults(func=cmd_list)

    # bare `hc --from X --to Y` == `hc convert ...`
    if len(sys.argv) > 1 and sys.argv[1] not in ("convert", "list", "-h", "--help"):
        sys.argv.insert(1, "convert")

    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help(); sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
