#!/usr/bin/env python
"""screenshot — capture the desktop or a window by exact title to a PNG.

Usage:
  screenshot                          # whole desktop
  screenshot "Spotify – Home"         # window with that exact title
  screenshot 12345                    # numeric → window id
  screenshot "Foo" -o ~/shots/        # into a directory (auto-named)
  screenshot "Foo" -o /tmp/x.png      # explicit file path

Output path resolution:
  -o omitted        → ./<slug>-<ts>.png
  -o is a dir       → <dir>/<slug>-<ts>.png
  -o is a file path → used as-is

On success prints a markdown image ref `![screenshot](<abs path>)`. The kernel's
expand_image_refs() turns that into a base64 Anthropic image block, so the model
sees the screenshot in the same turn. Save inside PAI_ROOT for it to expand.

Exit codes:
  0  ok
  1  no window matches the title
  2  multiple windows match the title (candidates printed to stderr)
  3  capture failed
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

import Quartz


def list_windows() -> list[dict]:
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    return list(Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or [])


def find_window_id_by_title(title: str) -> tuple[int | None, list[tuple[int, str, str]]]:
    matches: list[tuple[int, str, str]] = []
    for w in list_windows():
        name = w.get("kCGWindowName") or ""
        if name == title:
            wid = int(w.get("kCGWindowNumber", 0))
            owner = w.get("kCGWindowOwnerName") or ""
            matches.append((wid, name, owner))
    if len(matches) == 1:
        return matches[0][0], matches
    return None, matches


def slugify(s: str) -> str:
    s = re.sub(r"[^\w.-]+", "_", s).strip("_")
    return s or "screenshot"


def resolve_output(out: str | None, slug: str) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    default_name = f"{slug}-{ts}.png"
    if out is None:
        return Path.cwd() / default_name
    p = Path(out).expanduser()
    if out.endswith("/") or p.is_dir():
        p.mkdir(parents=True, exist_ok=True)
        return p / default_name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def capture(window_id: int | None, out_path: Path) -> int:
    cmd = ["/usr/sbin/screencapture", "-x", "-t", "png"]
    if window_id is not None:
        cmd += ["-l", str(window_id), "-o"]
    cmd.append(str(out_path))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr or "screencapture failed\n")
        return 3
    if not out_path.exists():
        sys.stderr.write(f"screencapture exited 0 but {out_path} does not exist\n")
        return 3
    # Emit a markdown image ref so the kernel's expand_image_refs() base64-encodes
    # the PNG and sends it to the model as a real image block in the same turn.
    # The path must resolve inside PAI_ROOT to expand; otherwise it passes through
    # as literal text (which still tells the caller where the file landed).
    print(f"![screenshot]({out_path.resolve()})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="screenshot", description=__doc__.splitlines()[0])
    ap.add_argument("target", nargs="?", help="exact window title, or numeric window id; omit for desktop")
    ap.add_argument("-o", "--output", help="output file or directory")
    args = ap.parse_args()

    if args.target is None:
        out = resolve_output(args.output, "desktop")
        return capture(None, out)

    if args.target.isdigit():
        wid = int(args.target)
        out = resolve_output(args.output, f"window-{wid}")
        return capture(wid, out)

    wid, matches = find_window_id_by_title(args.target)
    if wid is None and not matches:
        sys.stderr.write(f"no window with exact title {args.target!r}\n")
        return 1
    if wid is None:
        sys.stderr.write(f"{len(matches)} windows match title {args.target!r}:\n")
        for mid, name, owner in matches:
            sys.stderr.write(f"  id={mid} owner={owner!r} title={name!r}\n")
        sys.stderr.write("re-invoke with the numeric window id to disambiguate\n")
        return 2

    out = resolve_output(args.output, slugify(args.target))
    return capture(wid, out)


if __name__ == "__main__":
    sys.exit(main())
