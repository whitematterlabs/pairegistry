#!/usr/bin/env python
"""inbox — bounded, count-first lister over the nested email archive.

email keeps a complete on-disk archive at
    communication/email/<account>/YYYY/MM/DD/<subject-slug>.yaml
so date-scoped queries are a glob away. `inbox` answers "what landed?" without
dumping hundreds of rows: it COUNTS everything in the date window first, prints
per-account totals, then a capped sample of the most recent messages. For the
full list or full-text search, the email SKILL points at `rg` over the same
date globs.

Examples:
    inbox                         # today, all accounts
    inbox --since 7d              # last 7 days
    inbox --since 2026-06-01
    inbox --day 2026-06-25 --account icloud
    inbox --direction inbound --limit 30

This is read-only — it never materializes or mutates mail.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml

from boot import paths


DEFAULT_SAMPLE = 20
MAX_SAMPLE = 200


def _parse_since(value: str) -> date:
    """`today` | `Nd` (N days back) | `YYYY-MM-DD` → a date lower bound."""
    v = value.strip().lower()
    if v == "today":
        return date.today()
    if v == "yesterday":
        return date.today() - timedelta(days=1)
    if v.endswith("d") and v[:-1].isdigit():
        return date.today() - timedelta(days=int(v[:-1]))
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"inbox: bad --since '{value}' "
                         "(use today, Nd, or YYYY-MM-DD)")


def _date_dirs(account_dir: Path, lo: date, hi: date) -> list[Path]:
    """Existing YYYY/MM/DD dirs in [lo, hi] for one account."""
    out: list[Path] = []
    d = lo
    while d <= hi:
        p = account_dir / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}"
        if p.is_dir():
            out.append(p)
        d += timedelta(days=1)
    return out


def _account_dirs(root: Path, account_filter: Optional[str]) -> list[Path]:
    if not root.exists():
        return []
    out = []
    for p in sorted(root.iterdir()):
        if not p.is_dir() or p.name in ("drafts", "threads"):
            continue
        if account_filter and account_filter.lower() not in p.name.lower():
            continue
        out.append(p)
    return out


def _read_head(path: Path) -> dict:
    """Parse a message yaml for projection fields. Cheap enough for the
    capped sample only — counting never parses."""
    try:
        with path.open() as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def run(args: argparse.Namespace) -> int:
    root = paths.var_spool_email()

    if args.day:
        try:
            lo = hi = date.fromisoformat(args.day)
        except ValueError:
            raise SystemExit(f"inbox: bad --day '{args.day}' (use YYYY-MM-DD)")
    else:
        hi = date.today()
        lo = _parse_since(args.since) if args.since else hi
    if lo > hi:
        lo, hi = hi, lo

    accounts = _account_dirs(root, args.account)
    if not accounts:
        where = f" matching {args.account!r}" if args.account else ""
        print(f"inbox: no accounts{where} under {root}", file=sys.stderr)
        return 0

    per_account: dict[str, int] = {}
    matched: list[Path] = []
    for acc in accounts:
        files: list[Path] = []
        for dd in _date_dirs(acc, lo, hi):
            files.extend(dd.glob("*.yaml"))
        if files:
            per_account[acc.name] = len(files)
            matched.extend(files)

    total = len(matched)
    span = f"{lo:%Y-%m-%d}" if lo == hi else f"{lo:%Y-%m-%d}..{hi:%Y-%m-%d}"
    dir_count = len(per_account)
    print(f"inbox {span} — {dir_count} account(s), {total} message(s)")
    for name in sorted(per_account, key=lambda n: -per_account[n]):
        print(f"  {name}: {per_account[name]}")

    if total == 0:
        return 0

    # Newest first: path sort descends YYYY/MM/DD, then filename.
    matched.sort(reverse=True)
    limit = min(max(args.limit, 0), MAX_SAMPLE)

    shown = 0
    lines: list[str] = []
    for path in matched:
        if shown >= limit:
            break
        msg = _read_head(path)
        if not msg:
            continue
        direction = msg.get("direction") or "?"
        if args.direction and direction != args.direction:
            continue
        ts = msg.get("received_at") or msg.get("sent_at") or ""
        ts_short = str(ts)[:16].replace("T", " ")
        who = msg.get("from") or ""
        subj = (msg.get("subject") or "").replace("\n", " ")[:80]
        tag = "in " if direction == "inbound" else "out" if direction == "outbound" else "?  "
        stub = " [stub]" if msg.get("body_state") == "absent" else ""
        lines.append(f"  {ts_short}  {tag}  {who}  {subj!r}{stub}")
        shown += 1

    if lines:
        scope = f"most recent {len(lines)}"
        print(f"sample ({scope} of {total}):")
        print("\n".join(lines))
    if total > shown:
        lo_glob = f"{lo:%Y/%m/%d}" if lo == hi else f"{lo:%Y}/**"
        print(f"(+{total - shown} more — rg communication/email/*/{lo_glob}/ for the rest)")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="inbox",
        description="Bounded, count-first lister over the nested email archive.",
    )
    p.add_argument("--since", help="lower bound: today | Nd | YYYY-MM-DD (default: today)")
    p.add_argument("--day", help="restrict to a single day YYYY-MM-DD")
    p.add_argument("--account", help="only accounts whose dir name contains this substring")
    p.add_argument("--direction", choices=["inbound", "outbound"],
                   help="filter the sample by direction")
    p.add_argument("--limit", type=int, default=DEFAULT_SAMPLE,
                   help=f"max sample rows (default {DEFAULT_SAMPLE}, max {MAX_SAMPLE})")
    return p.parse_args(argv)


def main() -> None:
    sys.exit(run(parse_args(sys.argv[1:])))


if __name__ == "__main__":
    main()
