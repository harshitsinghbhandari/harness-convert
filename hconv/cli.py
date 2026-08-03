#!/usr/bin/env python3
"""hc: relocate a coding-agent session across harnesses.

Escape hatch: your harness hit a rate limit mid-task. Move the session to a
live harness and keep going. Reads transcripts off disk; the source does NOT
need to be running or your quota intact.

    hc                                 # interactive wizard (TTY)
    hc --from claude --to codex        # dry-run latest; TTY confirms write
    hc --from claude --to codex -y     # write without prompting
    hc --from claude --to codex --write
    hc list --from claude -n 5         # list; TTY: pick one and convert
    hc list --from claude | cat        # plain table (no prompts)

Flags always win. Missing pieces prompt on a TTY. Pipes / HC_NO_INTERACTIVE=1
stay non-interactive. Stdlib only.

cursor is read-only (--from only). grok is full R/W.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from hconv import convert, get, known, writable
from hconv import ui


RESUME = {
    "codex": "codex resume {sid}",
    "claude": "claude --resume {sid}",
    "opencode": "opencode import {dest} && opencode -s {sid}",
    "grok": "grok --resume {sid}",
}


def _fmt_mtime(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "?"


def _session_label(ref) -> str:
    title = (ref.title or "").replace("\n", " ").strip()
    if len(title) > 50:
        title = title[:47].rstrip() + "..."
    sid = ref.session_id
    short = sid if len(sid) <= 36 else sid[:8] + "…" + sid[-4:]
    line = f"{short}  {_fmt_mtime(ref.mtime)}"
    if title:
        line += f"  {title}"
    return line


def _print_preview(from_h: str, to_h: str, session, dest, dest_cwd: str) -> None:
    n_tool = sum(1 for r in session.records if type(r).__name__ == "ToolCall")
    title = session.extra.get("title") or ""
    print(f"{ui.dim('from')}    {ui.bold(from_h)}  {session.session_id}")
    print(f"{ui.dim('to')}      {ui.bold(to_h)}")
    print(f"{ui.dim('cwd')}     {dest_cwd}")
    print(f"{ui.dim('records')} {len(session.records)}  ({n_tool} tool calls)")
    if title:
        print(f"{ui.dim('title')}   {title}")
    print(f"{ui.dim('dest')}    {dest}")


def _print_resume(to_h: str, session, dest, dest_cwd: str) -> None:
    sid = session.extra.get("dest_session_id", session.session_id)
    cmd = RESUME[to_h].format(sid=sid, dest=dest)
    print()
    print(ui.green("WROTE.") + " resume with:")
    print(f"  cd {dest_cwd} && {ui.bold(cmd)}")


def _resolve_from(from_h: str | None, interactive: bool) -> str:
    if from_h:
        return from_h
    if not interactive:
        raise SystemExit("error: --from is required (non-interactive)")
    return ui.pick(known(), title=ui.dim("Source harness"))


def _resolve_to(to_h: str | None, interactive: bool, from_h: str) -> str:
    if to_h:
        if to_h not in writable():
            raise SystemExit(f"error: cannot convert into '{to_h}' (read-only)")
        return to_h
    if not interactive:
        raise SystemExit("error: --to is required (non-interactive)")
    dests = writable()
    # Put a different harness first when possible (escape hatch default bias).
    ordered = [d for d in dests if d != from_h] + [d for d in dests if d == from_h]
    return ui.pick(ordered, title=ui.dim("Destination harness"))


def _resolve_session(from_h: str, cwd: str, session_id: str | None,
                     interactive: bool, limit: int) -> str | None:
    """Return session_id or None (meaning: adapter default / latest)."""
    if session_id:
        return session_id
    if not interactive:
        return None
    adapter = get(from_h)
    refs = adapter.list_sessions(cwd, limit=limit)
    if not refs:
        raise SystemExit(f"no {from_h} sessions found for {cwd}")
    if len(refs) == 1:
        print(ui.dim(f"using only session: {refs[0].session_id}"))
        return refs[0].session_id
    labels = [_session_label(r) for r in refs]
    idx = ui.pick_index(labels, title=ui.dim(f"{from_h} sessions for {cwd}"))
    return refs[idx].session_id


def run_convert(from_h: str | None, to_h: str | None, cwd: str, dest_cwd: str | None,
                session_id: str | None, write: bool, yes: bool,
                interactive: bool, pick_limit: int = 15) -> None:
    """Shared convert path for `hc convert` and list→convert."""
    from_h = _resolve_from(from_h, interactive)
    session_id = _resolve_session(from_h, cwd, session_id, interactive, pick_limit)
    to_h = _resolve_to(to_h, interactive, from_h)
    dest_cwd = dest_cwd or cwd

    # Always dry-run first for a consistent preview (and so write reuses the
    # already-normalized Session).
    try:
        session, dest = convert(from_h, to_h, cwd, dest_cwd,
                                session_id=session_id, write=False)
    except SystemExit as e:
        raise SystemExit(e)

    _print_preview(from_h, to_h, session, dest, dest_cwd)

    do_write = bool(write or yes)
    if not do_write and interactive:
        do_write = ui.confirm("Write session?", default=False)
    if not do_write:
        print()
        print(ui.dim("(dry run; pass --write or -y to create it, or confirm when prompted)"))
        return

    dest = get(to_h).write(session, dest_cwd)
    _print_resume(to_h, session, dest, dest_cwd)


def cmd_convert(a):
    interactive = ui.can_interact(not a.no_interactive)
    run_convert(
        from_h=a.from_harness,
        to_h=a.to,
        cwd=a.cwd,
        dest_cwd=a.dest_cwd,
        session_id=a.session_id,
        write=a.write,
        yes=a.yes,
        interactive=interactive,
        pick_limit=a.n,
    )


def cmd_list(a):
    interactive = ui.can_interact(not a.no_interactive)
    from_h = a.from_harness
    if not from_h:
        if not interactive:
            raise SystemExit("error: --from is required (non-interactive)")
        from_h = ui.pick(known(), title=ui.dim("Source harness"))

    adapter = get(from_h)
    refs = adapter.list_sessions(a.cwd, limit=a.n)
    if not refs:
        print(f"no {from_h} sessions found for {a.cwd}")
        return

    # Non-interactive / piped: plain table (stable for scripts).
    if not interactive:
        print(f"{from_h} sessions for {a.cwd}  (newest first, {len(refs)} shown)")
        id_w = max(len(r.session_id) for r in refs)
        id_w = max(id_w, 2)
        for r in refs:
            title = (r.title or "").replace("\n", " ").strip()
            if len(title) > 60:
                title = title[:57].rstrip() + "..."
            line = f"  {r.session_id:<{id_w}}  {_fmt_mtime(r.mtime)}"
            if title:
                line += f"  {title}"
            print(line)
        return

    # Interactive: pick a session, then convert wizard (to + write confirm).
    labels = [_session_label(r) for r in refs]
    print(ui.dim(f"{from_h} sessions for {a.cwd}  (newest first, {len(refs)})"))
    idx = ui.pick_index(labels, title="")
    chosen = refs[idx]
    print(ui.dim(f"selected {chosen.session_id}"))
    run_convert(
        from_h=from_h,
        to_h=None,
        cwd=a.cwd,
        dest_cwd=a.dest_cwd,
        session_id=chosen.session_id,
        write=a.write,
        yes=a.yes,
        interactive=True,
        pick_limit=a.n,
    )


def main():
    ap = argparse.ArgumentParser(
        prog="hc", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd")

    def add_common(p, from_required: bool = False):
        p.add_argument("--from", dest="from_harness", required=from_required,
                       choices=known(), default=None, help="source harness")
        p.add_argument("--cwd", default=os.getcwd(),
                       help="source folder (default: pwd)")
        p.add_argument("--no-interactive", action="store_true",
                       help="never prompt (for scripts/CI)")

    c = sub.add_parser("convert", help="move a session to another harness")
    add_common(c)
    c.add_argument("--to", required=False, choices=writable(), default=None,
                   help="destination harness")
    c.add_argument("session_id", nargs="?", default=None,
                   help="session id (default: pick / latest for cwd)")
    c.add_argument("--dest-cwd", default=None,
                   help="destination folder (default: same as --cwd)")
    c.add_argument("--write", action="store_true",
                   help="write without asking")
    c.add_argument("-y", "--yes", action="store_true",
                   help="write without asking (alias: implies write)")
    c.add_argument("-n", type=int, default=15, metavar="N",
                   help="how many sessions to offer when picking (default: 15)")
    c.set_defaults(func=cmd_convert)

    l = sub.add_parser("list", help="list recent sessions; TTY can pick and convert")
    add_common(l)
    l.add_argument("-n", "--n", type=int, default=10, metavar="N",
                   help="how many newest sessions to show (default: 10)")
    l.add_argument("--dest-cwd", default=None,
                   help="when converting after pick: destination folder")
    l.add_argument("--write", action="store_true",
                   help="when converting after pick: write without asking")
    l.add_argument("-y", "--yes", action="store_true",
                   help="when converting after pick: write without asking")
    l.set_defaults(func=cmd_list)

    # bare `hc` == interactive convert; bare `hc --from X --to Y` == convert
    if len(sys.argv) == 1:
        sys.argv.append("convert")
    elif sys.argv[1] not in ("convert", "list", "-h", "--help"):
        sys.argv.insert(1, "convert")

    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        sys.exit(2)
    args.func(args)


if __name__ == "__main__":
    main()
