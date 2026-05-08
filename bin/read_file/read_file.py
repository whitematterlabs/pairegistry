#!/usr/bin/env python
"""read_file — bounded file reader.

Wraps `sed -n A,Bp` / `head` / `tail` and refuses to dump >100 lines
without an explicit slice flag. Mirrors the coder prompt rule
("never cat files >100 lines"); enforces it instead of relying on
posture.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


LINE_LIMIT = 100


def _parse_range(spec: str) -> tuple[int, int]:
    if ":" not in spec:
        print(f"error: --range expects A:B, got {spec!r}", file=sys.stderr)
        raise SystemExit(5)
    a_str, b_str = spec.split(":", 1)
    try:
        a, b = int(a_str), int(b_str)
    except ValueError:
        print(f"error: --range expects integers, got {spec!r}", file=sys.stderr)
        raise SystemExit(5)
    if a < 1 or b < a:
        print(f"error: --range A:B requires 1 <= A <= B, got {spec!r}", file=sys.stderr)
        raise SystemExit(5)
    return a, b


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="read_file",
        description=(
            f"Bounded file read. Files over {LINE_LIMIT} lines require an "
            "explicit slice (--range A:B, --head N, or --tail N)."
        ),
    )
    p.add_argument("path", help="file to read (symlinks resolved)")
    p.add_argument("--range", dest="range_spec", help="line range A:B (1-indexed, inclusive)")
    p.add_argument("--head", type=int, help="first N lines")
    p.add_argument("--tail", type=int, help="last N lines")
    p.add_argument(
        "--lines",
        action="store_true",
        help="prefix each line with its 1-indexed number (cat -n style)",
    )
    args = p.parse_args(argv)

    slice_flags = sum(x is not None for x in (args.range_spec, args.head, args.tail))
    if slice_flags > 1:
        print("error: --range / --head / --tail are mutually exclusive", file=sys.stderr)
        return 5

    target = Path(args.path).resolve()
    if not target.exists():
        print(f"error: {args.path}: no such file", file=sys.stderr)
        return 2
    if target.is_dir():
        print(f"error: {args.path}: is a directory", file=sys.stderr)
        return 2

    try:
        text = target.read_text()
    except UnicodeDecodeError:
        print(f"error: {args.path}: binary file; refusing to read", file=sys.stderr)
        return 6

    lines = text.splitlines(keepends=True)
    n = len(lines)

    if args.range_spec is not None:
        a, b = _parse_range(args.range_spec)
        b = min(b, n)
        a = min(a, n + 1)
        out = lines[a - 1 : b]
        first = a
    elif args.head is not None:
        if args.head < 1:
            print("error: --head must be >= 1", file=sys.stderr)
            return 5
        out = lines[: args.head]
        first = 1
    elif args.tail is not None:
        if args.tail < 1:
            print("error: --tail must be >= 1", file=sys.stderr)
            return 5
        start = max(0, n - args.tail)
        out = lines[start:]
        first = start + 1
    else:
        if n > LINE_LIMIT:
            print(
                f"error: {args.path} has {n} lines (> {LINE_LIMIT}); "
                "pass --range A:B, --head N, or --tail N",
                file=sys.stderr,
            )
            return 4
        out = lines
        first = 1

    if args.lines:
        width = len(str(first + len(out) - 1))
        for i, line in enumerate(out):
            sys.stdout.write(f"{first + i:>{width}}\t{line}")
            if not line.endswith("\n"):
                sys.stdout.write("\n")
    else:
        sys.stdout.writelines(out)

    return 0


if __name__ == "__main__":
    sys.exit(main())
