#!/usr/bin/env python3
"""hc: relocate a coding-agent session across harnesses.

Escape hatch: your harness hit a wall (rate limit, outage) mid-task. Move the
session to a live harness and keep going. Reads transcripts off disk; the source
harness does NOT need to be running or your quota intact.

    hc --from claude --to codex            # move latest Claude session here -> Codex
    hc --from codex  --to claude <id>      # a specific session
    hc --from claude --to codex --cwd DIR  # source/dest folder (default: pwd)
    hc list --from codex                   # what's convertible for this cwd

By default prints what it WOULD do; pass --write to actually create the file.
"""
import argparse
import os
import sys

from hconv import convert, get, known


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
        sid, cwd = session.session_id, a.dest_cwd or a.cwd
        resume = {"codex": f"codex resume {sid}",
                  "claude": f"claude --resume {sid}",
                  "opencode": f"opencode import {dest} && opencode -s {sid}"}[a.to]
        print(f"\nWROTE. resume with:\n  cd {cwd} && {resume}")
    else:
        print("\n(dry run; pass --write to create it)")


def cmd_list(a):
    adapter = get(a.from_harness)
    try:
        p = adapter.locate(a.cwd)
        print(f"latest {a.from_harness} session for {a.cwd}:\n  {p}")
    except SystemExit as e:
        print(e)


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
    c.add_argument("--to", required=True, choices=known(), help="destination harness")
    c.add_argument("session_id", nargs="?", help="session id (default: latest for cwd)")
    c.add_argument("--dest-cwd", help="destination folder (default: same as --cwd)")
    c.add_argument("--write", action="store_true", help="actually write the file")
    c.set_defaults(func=cmd_convert)

    l = sub.add_parser("list", help="show the latest convertible session for a cwd")
    add_common(l)
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
