"""Stdlib TTY helpers for interactive hc.

No third-party deps. Color and pickers activate only when stdin+stdout are TTYs
and HC_NO_INTERACTIVE / --no-interactive are unset. Pipes and CI stay plain.
"""
from __future__ import annotations

import os
import sys
from typing import Sequence


def can_interact(enabled: bool = True) -> bool:
    """True when prompts/pickers are allowed."""
    if not enabled:
        return False
    if os.environ.get("HC_NO_INTERACTIVE", "").strip() in ("1", "true", "yes"):
        return False
    if os.environ.get("NO_COLOR", "") == "1" and os.environ.get("FORCE_COLOR", "") == "":
        pass  # NO_COLOR only kills color, not prompts
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def use_color() -> bool:
    if os.environ.get("NO_COLOR", "").strip() != "":
        return False
    if os.environ.get("HC_NO_COLOR", "").strip() in ("1", "true", "yes"):
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _c(code: str, text: str) -> str:
    if not use_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def dim(text: str) -> str:
    return _c("2", text)


def bold(text: str) -> str:
    return _c("1", text)


def green(text: str) -> str:
    return _c("32", text)


def red(text: str) -> str:
    return _c("31", text)


def cyan(text: str) -> str:
    return _c("36", text)


def confirm(prompt: str, default: bool = False) -> bool:
    """y/N or Y/n. EOF / empty uses default. Non-interactive callers should not call."""
    hint = "Y/n" if default else "y/N"
    try:
        raw = input(f"{prompt} [{hint}] ").strip().lower()
    except EOFError:
        return default
    if not raw:
        return default
    return raw in ("y", "yes")


def pick(options: Sequence[str], title: str = "", default: int = 0) -> str:
    """Pick one option; returns the string. Raises SystemExit on cancel/empty."""
    idx = pick_index(options, title=title, default=default)
    return options[idx]


def pick_index(options: Sequence[str], title: str = "", default: int = 0) -> int:
    """Pick an index. Arrow/jk when raw TTY works; else numbered prompt."""
    if not options:
        raise SystemExit("nothing to pick")
    default = max(0, min(default, len(options) - 1))
    if _raw_ok():
        try:
            return _pick_arrows(options, title=title, default=default)
        except _PickCancelled:
            raise SystemExit("cancelled")
        except Exception:
            pass  # fall through to numbered
    return _pick_numbered(options, title=title, default=default)


class _PickCancelled(Exception):
    pass


def _raw_ok() -> bool:
    if not can_interact():
        return False
    if not hasattr(sys.stdin, "fileno"):
        return False
    try:
        import termios  # noqa: F401
        import tty  # noqa: F401
        sys.stdin.fileno()
        return True
    except Exception:
        return False


def _pick_numbered(options: Sequence[str], title: str = "", default: int = 0) -> int:
    if title:
        print(title)
    width = len(str(len(options)))
    for i, opt in enumerate(options, 1):
        mark = dim("*") if i - 1 == default else " "
        print(f"  {mark} {str(i).rjust(width)}. {opt}")
    while True:
        try:
            raw = input(f"{dim('number')} [{default + 1}]: ").strip()
        except EOFError:
            raise SystemExit("cancelled")
        if not raw:
            return default
        if raw.lower() in ("q", "quit"):
            raise SystemExit("cancelled")
        try:
            n = int(raw)
        except ValueError:
            print(dim(f"enter 1-{len(options)}, or q"))
            continue
        if 1 <= n <= len(options):
            return n - 1
        print(dim(f"enter 1-{len(options)}, or q"))


def _pick_arrows(options: Sequence[str], title: str = "", default: int = 0) -> int:
    import termios
    import tty

    idx = default
    n = len(options)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    def render(first: bool) -> None:
        nonlocal idx
        if not first:
            # move cursor up over previous block
            lines = n + (2 if title else 1)  # options + hint (+ title)
            sys.stdout.write(f"\033[{lines}A")
        if title:
            sys.stdout.write(title + "\n")
        for i, opt in enumerate(options):
            if i == idx:
                prefix = green("▸")
                body = bold(opt)
            else:
                prefix = " "
                body = opt
            # clear to end of line so shorter options don't leave ghosts
            sys.stdout.write(f"  {prefix} {body}\033[K\n")
        sys.stdout.write(dim("  ↑↓/jk move · enter select · q quit") + "\033[K\n")
        sys.stdout.flush()

    try:
        tty.setcbreak(fd)
        render(first=True)
        while True:
            key = _read_key()
            if key == "up":
                idx = (idx - 1) % n
                render(first=False)
            elif key == "down":
                idx = (idx + 1) % n
                render(first=False)
            elif key == "enter":
                return idx
            elif key == "quit":
                raise _PickCancelled()
            elif isinstance(key, tuple) and key[0] == "digit":
                # accumulate single digit quick-select for small lists
                d = int(key[1])
                if 1 <= d <= n:
                    idx = d - 1
                    render(first=False)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_key():
    """Read one logical key from stdin in cbreak mode."""
    c = sys.stdin.read(1)
    if not c:
        return "quit"
    if c in ("\r", "\n"):
        return "enter"
    if c in ("q", "Q", "\x03"):  # q or Ctrl-C
        return "quit"
    if c in ("k", "K"):
        return "up"
    if c in ("j", "J"):
        return "down"
    if c == "\x1b":
        # CSI sequences: ESC [ A/B
        rest = sys.stdin.read(1)
        if rest == "[":
            arrow = sys.stdin.read(1)
            if arrow == "A":
                return "up"
            if arrow == "B":
                return "down"
        return None
    if c.isdigit() and c != "0":
        return ("digit", c)
    return None
