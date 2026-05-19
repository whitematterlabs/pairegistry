#!/usr/bin/env python
"""memorize — ask librarian-pai to commit a fact to memory on behalf of the caller."""

from __future__ import annotations

import argparse
import os
import sys

from boot import processes as P


def _resolve_librarian_pid() -> int | None:
    for slug, spec in P._iter_pai_specs():
        if slug == "librarian-pai":
            pid = spec.get("pid")
            if isinstance(pid, int):
                return pid
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="memorize",
        description="Ask librarian-pai to commit a fact to memory on behalf of the calling PAI.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--shared", action="store_true", help="write to shared memory (visible to whole fleet)")
    mode.add_argument("--private", action="store_true", help="write to the caller's private memory only")
    parser.add_argument("--content", required=True, help="memory text")
    args = parser.parse_args(argv)

    sender_raw = os.environ.get("PAI_PID")
    if not sender_raw:
        print(
            "error: $PAI_PID not set — memorize must be invoked from a PAI turn",
            file=sys.stderr,
        )
        return 1
    try:
        sender_pid = int(sender_raw)
    except ValueError:
        print(f"error: $PAI_PID={sender_raw!r} is not an int", file=sys.stderr)
        return 1

    librarian_pid = _resolve_librarian_pid()
    if librarian_pid is None:
        print(
            "error: librarian-pai is not in the fleet — run `paiadd librarian-pai`",
            file=sys.stderr,
        )
        return 1

    mode_str = "shared" if args.shared else "private"
    P.emit_event({
        "source": "memorize",
        "kind": "pai_message",
        "target_pid": librarian_pid,
        "sender_pid": sender_pid,
        "text": f"[memorize:{mode_str}] {args.content}",
    })
    print(f"requested {mode_str} memory write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
