#!/usr/bin/env python
"""imessage-history — read iMessage history from chat.db in a date window.

READ-ONLY. Unlike `imessage-backfill`, this never writes day-files, never
replays through the driver, and never refuses to run while the live driver
is up — it just opens chat.db with `query_only = ON` and prints what it sees.

It exists because on a fresh OOBE the iMessage driver has backfilled nothing:
the day-files under the owner runtime messages tree
(~/.pai/home/pai/messages/, or /home/pai/messages/ from a PAI shell) are empty
for any message that predates the kernel's first boot. The "getting to know
you" onboarding pass needs the owner's *last month* of conversation, so it
reads chat.db directly.

Reuses the iMessage inbound driver's building blocks (the bounded SQL, the
attributedBody typedstream decoder, the mac-epoch date conversions) rather
than duplicating them; only the WHERE clause is rewritten from a ROWID cursor
to a date window.

Usage:
    imessage-history                       # last 30 days through today
    imessage-history --since 2026-05-18    # from a date through today
    imessage-history --since 2026-05-18 --until 2026-05-25
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime, time, timedelta, timezone

import yaml

from drivers.imessage.inbound import (
    CHAT_DB,
    DELTA_SQL,
    MAC_EPOCH,
    _decode_attributed_body,
    _mac_date_to_iso,
)

# Window-scan variant of the driver's bounded delta query: same columns and
# joins, but selected by date range instead of a ROWID cursor.
WINDOW_SQL = DELTA_SQL.replace("WHERE m.ROWID > ?", "WHERE m.date >= ? AND m.date < ?")

DEFAULT_WINDOW_DAYS = 30


def _local_day_to_mac_range(start_d: date, end_d: date) -> tuple[int, int]:
    """[start, end) in chat.db nanos-since-2001-UTC for an inclusive local-date
    range. Mirrors imessage_backfill._local_day_to_mac_range."""
    start_local = datetime.combine(start_d, time.min).astimezone()
    end_local = datetime.combine(end_d + timedelta(days=1), time.min).astimezone()
    start_ns = int((start_local.astimezone(timezone.utc) - MAC_EPOCH).total_seconds() * 1e9)
    end_ns = int((end_local.astimezone(timezone.utc) - MAC_EPOCH).total_seconds() * 1e9)
    return start_ns, end_ns


def _connect() -> sqlite3.Connection:
    # Opened read-write but PRAGMA query_only = ON (rejects writes). Read-only
    # mode=ro can't touch the WAL index and would miss messages Messages.app
    # hasn't checkpointed yet; see drivers/imessage/inbound._connect.
    conn = sqlite3.connect(str(CHAT_DB))
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _thread_label(row) -> str:
    """A stable, human-readable handle for the conversation a row belongs to.

    Group chats key on chat_guid (1:1 chats reuse the peer's handle so they
    collapse to one thread)."""
    chat_guid = row["chat_guid"] or ""
    if chat_guid and int(row["participant_count"] or 0) > 1:
        return chat_guid
    return row["handle"] or "(unknown)"


def collect(start_d: date, end_d: date) -> list[dict]:
    if not CHAT_DB.exists():
        raise SystemExit(f"chat.db not found at {CHAT_DB} (Full Disk Access granted?)")
    start_ns, end_ns = _local_day_to_mac_range(start_d, end_d)
    conn = _connect()
    try:
        rows = conn.execute(WINDOW_SQL, (start_ns, end_ns)).fetchall()
    finally:
        conn.close()

    messages: list[dict] = []
    for row in rows:
        text = row["text"]
        if text is None:
            text = _decode_attributed_body(row["attributed_body"])
        if text is None or not (row["handle"] or "").strip():
            continue
        messages.append({
            "date": _mac_date_to_iso(int(row["mac_date"])),
            "thread": _thread_label(row),
            "sender": "me" if bool(row["is_from_me"]) else row["handle"],
            "text": text,
        })
    return messages


def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="imessage-history",
        description="Read iMessage history from chat.db in a date window (read-only).",
    )
    parser.add_argument(
        "--since",
        type=_parse_date,
        help=f"start date YYYY-MM-DD (inclusive); default {DEFAULT_WINDOW_DAYS} days ago",
    )
    parser.add_argument(
        "--until",
        type=_parse_date,
        help="end date YYYY-MM-DD (inclusive); default today",
    )
    args = parser.parse_args(argv)

    end_d = args.until or date.today()
    start_d = args.since or (end_d - timedelta(days=DEFAULT_WINDOW_DAYS))
    if end_d < start_d:
        print(f"error: --until {end_d} is before --since {start_d}", file=sys.stderr)
        return 2

    messages = collect(start_d, end_d)
    print(
        f"# iMessage history {start_d.isoformat()}..{end_d.isoformat()} "
        f"— {len(messages)} messages",
    )
    # Block-style YAML keeps multi-line message bodies readable and greppable.
    yaml.safe_dump(messages, sys.stdout, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
