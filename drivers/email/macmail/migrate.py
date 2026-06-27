#!/usr/bin/env python
"""email migration — flat `<account>/YYYY-MM-DD/` → nested `<account>/YYYY/MM/DD/`.

The old email/macmail driver wrote a flat per-day tree; email uses a nested
`YYYY/MM/DD` partition. This is a one-time, in-place migration of an existing
archive, run as the first step of cutover (before `backfill`). It:

  1. Snapshots the whole `communication/email` tree to a `.tar.gz` aside — the
     data rollback artifact (the nested tree is unreadable by the flat driver,
     so this snapshot is REQUIRED to revert).
  2. Moves every flat `<account>/YYYY-MM-DD/<slug>.yaml` to its nested location,
     preserving already-captured bodies so they don't regress to stubs when the
     backfill runs.
  3. Rebuilds every `threads/<slug>/` and `.prev` symlink. The move changes each
     message's path depth, which would break the old relpath-based links, so the
     thread index and one-hop `.prev` parenting are regenerated from scratch
     (derivable from each yaml's thread_slug / references).

Snapshot + move + rebuild only — it never touches Mail.app or the cursor; run
`backfill` afterwards to fill history gaps.

Run from the FHS root with the PAI venv:
    cd ~/.pai && usr/bin/python -m drivers.email.macmail.migrate [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from boot import paths

from . import inbound as IN
from .. import shared


_FLAT_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _snapshot(email_root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = IN.PARKED_DIR / f"pre-migration-email-{stamp}.tar.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(email_root, arcname="email")
    return dest


def _account_dirs(email_root: Path) -> list[Path]:
    return [
        p for p in sorted(email_root.iterdir())
        if p.is_dir() and p.name not in ("drafts",)
    ]


def _flat_message_files(account_dir: Path) -> list[Path]:
    """Yaml files sitting in flat `YYYY-MM-DD/` dirs (not the nested tree)."""
    out: list[Path] = []
    for d in account_dir.iterdir():
        if d.is_dir() and _FLAT_DATE.fullmatch(d.name):
            out.extend(d.glob("*.yaml"))
    return out


def _move_flat_to_nested(account_dir: Path, dry_run: bool) -> int:
    """Move flat <date>/<slug>.yaml → <YYYY>/<MM>/<DD>/<slug>.yaml. Drops the
    stale `.prev` symlinks and flat date dirs (links are rebuilt afterwards)."""
    moved = 0
    for d in list(account_dir.iterdir()):
        if not (d.is_dir() and _FLAT_DATE.fullmatch(d.name)):
            continue
        y, m, day = d.name.split("-")
        nested = account_dir / y / m / day
        for f in d.glob("*.yaml"):
            target = nested / f.name
            if target.exists():
                print(f"[migrate] skip (target exists): {target}", flush=True)
                continue
            if dry_run:
                moved += 1
                continue
            nested.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(target))
            moved += 1
        if not dry_run:
            # Old `.prev` symlinks + the now-empty (or symlink-only) flat dir.
            shutil.rmtree(d, ignore_errors=True)
    return moved


def _rebuild_links(account_dir: Path, dry_run: bool) -> tuple[int, int]:
    """Regenerate threads/ + .prev for every nested message yaml.

    Old links are derivable, so we drop `threads/` wholesale and rebuild from
    the moved files. An in-memory {message_id: path} map keeps parent lookups
    O(1) (no per-message disk scan)."""
    threads_dir = account_dir / "threads"
    if threads_dir.exists() and not dry_run:
        shutil.rmtree(threads_dir, ignore_errors=True)

    yamls = sorted(
        account_dir.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/*.yaml")
    )
    seen: dict[str, Path] = {}
    parsed: list[tuple[Path, dict]] = []
    for y in yamls:
        try:
            with y.open() as f:
                msg = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(msg, dict):
            continue
        mid = (msg.get("message_id") or "").strip()
        if mid:
            seen[mid] = y
        parsed.append((y, msg))

    threads = prevs = 0
    for path, msg in parsed:
        ts_raw = msg.get("received_at") or msg.get("sent_at")
        slug = msg.get("thread_slug")
        if ts_raw and slug:
            ts = ts_raw if isinstance(ts_raw, datetime) else datetime.fromisoformat(str(ts_raw))
            if not dry_run:
                shared.link_thread(account_dir, path, slug, ts)
            threads += 1
        refs = msg.get("references") or []
        parent_id = (msg.get("in_reply_to") or "").strip() or (refs[-1] if refs else None)
        if parent_id and parent_id in seen:
            if not dry_run:
                shared.link_prev(path, seen[parent_id])
            prevs += 1
    return threads, prevs


def run_migrate(args: argparse.Namespace) -> int:
    email_root = paths.var_spool_email()
    if not email_root.exists():
        print(f"migrate: no email tree at {email_root}; nothing to do", file=sys.stderr)
        return 0

    if not args.no_snapshot and not args.dry_run:
        snap = _snapshot(email_root)
        print(f"[migrate] snapshot → {snap}", flush=True)
    elif args.dry_run:
        print("[migrate] DRY RUN — no snapshot, no changes", flush=True)

    total_moved = total_threads = total_prevs = 0
    for account_dir in _account_dirs(email_root):
        flat = _flat_message_files(account_dir)
        if not flat and not (account_dir / "threads").exists():
            continue
        moved = _move_flat_to_nested(account_dir, args.dry_run)
        threads, prevs = _rebuild_links(account_dir, args.dry_run)
        total_moved += moved
        total_threads += threads
        total_prevs += prevs
        print(f"[migrate] {account_dir.name}: moved={moved} "
              f"threads={threads} prev={prevs}", flush=True)

    print(f"[migrate] {'DRY RUN ' if args.dry_run else ''}done: "
          f"moved={total_moved} thread_links={total_threads} prev_links={total_prevs}",
          flush=True)
    if not args.dry_run:
        print("[migrate] next: run the backfill to fill history gaps "
              "(python -m drivers.email.macmail.backfill)", flush=True)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="email-migrate",
        description="Migrate the flat email tree to email's nested YYYY/MM/DD "
                    "partition (snapshot + move + rebuild links).",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="report what would move without touching anything")
    p.add_argument("--no-snapshot", action="store_true",
                   help="skip the tar.gz rollback snapshot (NOT recommended)")
    return p.parse_args(argv)


def main() -> None:
    sys.exit(run_migrate(parse_args(sys.argv[1:])))


if __name__ == "__main__":
    main()
