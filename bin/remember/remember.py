#!/usr/bin/env python
"""remember - ask librarian-pai to retrieve memory/context for the caller."""

from __future__ import annotations

import argparse
import os
import sys
import uuid

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
        prog="remember",
        description=(
            "Ask librarian-pai to search memory/context and reply to the "
            "calling PAI. The answer arrives asynchronously via send-message."
        ),
    )
    parser.add_argument(
        "query",
        nargs="+",
        help="question or context request to send to librarian-pai",
    )
    args = parser.parse_args(argv)

    sender_raw = os.environ.get("PAI_PID")
    if not sender_raw:
        print(
            "error: $PAI_PID not set — remember must be invoked from a PAI turn",
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

    request_id = uuid.uuid4().hex[:12]
    query = " ".join(args.query).strip()
    P.emit_event({
        "source": "remember",
        "kind": "pai_message",
        "target_pid": librarian_pid,
        "sender_pid": sender_pid,
        "text": f"[remember:{request_id}] {query}",
    })
    print(f"requested memory context lookup ({request_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
