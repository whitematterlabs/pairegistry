#!/usr/bin/env python
"""mailv2 one-time backfill — hard-copy the entire Envelope Index into the tree.

The live `mailv2-in` driver only ingests rows past its cursor. This tool does
the *initial* population: it walks every message Mail.app has indexed for the
known inbox/sent/all-mail mailboxes and writes one yaml per message into the
nested `communication/email/<account>/YYYY/MM/DD/` tree — a full message when
the `.emlx` body is on disk, a header-only stub (built from the Envelope Index
joins, `body_state: absent`) when Mail.app has evicted the body.

Design notes
------------
* O(n), not O(n²): a one-time pre-scan of the on-disk tree builds an in-memory
  `{message_id: path}` map. Per-row dedup + parent resolution use that map, and
  writes pass `dedup=False`, so no per-insert full-tree scan happens. Over ~40k
  rows that is the difference between seconds and hours.
* Resumable: the last-processed ROWID is checkpointed to
  `sys/drivers/mailv2/backfill_progress.yaml`. A crashed run resumes from there;
  re-running a completed backfill processes zero rows.
* On completion it bootstraps the live cursor to MAX(ROWID) (same semantics as
  `inbound._bootstrap_cursor`) so the first `mailv2-in` start does NOT
  re-announce the whole archive as a backlog.

Run from the FHS root with the PAI venv:
    cd ~/.pai && usr/bin/python -m drivers.mailv2.macmail.backfill [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import yaml

from boot import paths

from . import accounts as A
from . import emlx as E
from . import inbound as IN


PROGRESS_PATH = IN.PARKED_DIR / "backfill_progress.yaml"
CHECKPOINT_EVERY = 500


# ---------- progress checkpoint --------------------------------------------

def _load_progress() -> int:
    """Return the last-processed ROWID, or 0 if no checkpoint exists."""
    if not PROGRESS_PATH.exists():
        return 0
    try:
        with PROGRESS_PATH.open() as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return 0
    try:
        return int(data.get("last_rowid") or 0)
    except (TypeError, ValueError):
        return 0


def _save_progress(last_rowid: int) -> None:
    IN._atomic_write_yaml(PROGRESS_PATH, {"last_rowid": int(last_rowid)})


# ---------- bulk index reads -----------------------------------------------

def _build_full_table_sql(cfg: A.AccountsConfig) -> tuple[str, list[str]]:
    """Enumerate every in-scope message past the resume bound, newest fields
    joined in. Mirrors `inbound._build_delta_sql` minus the `ROWID > ?`
    cursor (replaced by a resume bound) and extended with the sender /
    subject / message-id joins the stub builder needs."""
    patterns = [pat for pat, _role in cfg.url_like_patterns()]
    clause = "1 = 0" if not patterns else (
        "(" + " OR ".join(["mb.url LIKE ?"] * len(patterns)) + ")"
    )
    sql = f"""
SELECT
    m.ROWID AS rowid,
    m.date_received AS date_received,
    m.date_sent AS date_sent,
    m.conversation_id AS conversation_id,
    mb.url AS url,
    sndr.address AS from_address,
    sndr.comment AS from_name,
    m.subject_prefix AS subject_prefix,
    subj.subject AS subject_base,
    g.message_id_header AS message_id
FROM messages m
JOIN mailboxes mb ON mb.ROWID = m.mailbox
LEFT JOIN addresses sndr ON sndr.ROWID = m.sender
LEFT JOIN subjects subj ON subj.ROWID = m.subject
LEFT JOIN message_global_data g ON g.message_id = m.message_id
WHERE {clause}
  AND m.ROWID > ?
ORDER BY m.ROWID ASC
"""
    return sql, patterns


def _load_recipients(conn: sqlite3.Connection) -> dict[int, dict]:
    """{message_rowid: {"to": [addr...], "cc": [addr...]}}, in position order.
    type 0 = To, 1 = Cc (2 = Bcc is Sent-only and rarely present)."""
    out: dict[int, dict] = {}
    rows = conn.execute(
        "SELECT r.message AS m, r.type AS t, a.address AS addr "
        "FROM recipients r JOIN addresses a ON a.ROWID = r.address "
        "ORDER BY r.message, r.type, r.position"
    ).fetchall()
    for row in rows:
        bucket = out.setdefault(int(row["m"]), {"to": [], "cc": []})
        addr = (row["addr"] or "").strip().lower()
        if not addr:
            continue
        if row["t"] == 0:
            bucket["to"].append(addr)
        elif row["t"] == 1:
            bucket["cc"].append(addr)
    return out


def _load_references(conn: sqlite3.Connection) -> dict[int, list]:
    """{message_rowid: [Message-ID-header...]} in reference order (root-first).
    `message_references.reference` is the global-id hash; resolve it to the RFC
    header via `message_global_data`."""
    out: dict[int, list] = {}
    rows = conn.execute(
        "SELECT mr.message AS m, g.message_id_header AS hdr "
        "FROM message_references mr "
        "JOIN message_global_data g ON g.message_id = mr.reference "
        "WHERE g.message_id_header IS NOT NULL AND g.message_id_header != '' "
        "ORDER BY mr.message, mr.ROWID"
    ).fetchall()
    for row in rows:
        out.setdefault(int(row["m"]), []).append(row["hdr"])
    return out


# ---------- on-disk pre-scan (seen map) ------------------------------------

def _parse_message_id_line(line: str) -> Optional[str]:
    """Extract the Message-ID from a yaml `message_id:` first line."""
    if not line.startswith("message_id:"):
        return None
    val = line[len("message_id:"):].strip()
    if len(val) >= 2 and val[0] in "'\"" and val[-1] == val[0]:
        val = val[1:-1]
    return val or None


def _prescan_seen() -> dict[str, Path]:
    """Build {message_id: path} from the existing nested tree. message_id is
    the first line of every message yaml (sort_keys=False)."""
    seen: dict[str, Path] = {}
    root = paths.var_spool_email()
    if not root.exists():
        return seen
    for account_dir in root.iterdir():
        if not account_dir.is_dir():
            continue
        for yml in account_dir.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/*.yaml"):
            try:
                with yml.open() as f:
                    first = f.readline()
            except OSError:
                continue
            mid = _parse_message_id_line(first)
            if mid and mid not in seen:
                seen[mid] = yml
    return seen


# ---------- row → enriched record ------------------------------------------

def _enrich(row, recips: dict[int, dict], refs: dict[int, list]) -> dict:
    rowid = int(row["rowid"])
    rec = recips.get(rowid, {})
    subject = (row["subject_prefix"] or "") + (row["subject_base"] or "")
    return {
        "rowid": rowid,
        "url": row["url"] or "",
        "date_received": row["date_received"],
        "date_sent": row["date_sent"],
        "conversation_id": row["conversation_id"],
        "from_address": row["from_address"],
        "from_name": row["from_name"],
        "subject": subject,
        "message_id": (row["message_id"] or ""),
        "to": rec.get("to", []),
        "cc": rec.get("cc", []),
        "references": refs.get(rowid, []),
    }


def _emlx_present(rec: dict, cfg: A.AccountsConfig) -> bool:
    """True if the row's .emlx body is on disk (→ full ingest)."""
    url = rec["url"]
    account_uuid, role = IN._parse_url(url, cfg)
    if role is None:
        return False
    mailbox_name = unquote(urlparse(url).path.lstrip("/"))
    return E.emlx_path(account_uuid, mailbox_name, rec["rowid"]) is not None


# ---------- driver ---------------------------------------------------------

def run_backfill(args: argparse.Namespace) -> int:
    try:
        cfg = asyncio.run(A.refresh())
    except RuntimeError:
        cfg = A.load()
    if cfg.is_empty():
        print("backfill: no Mail.app accounts discovered (is Mail.app running "
              "and is automation permission granted?)", file=sys.stderr)
        return 2
    print(f"[backfill] accounts: {A.summarize(cfg)}", flush=True)

    if IN._index_access() != "ok":
        print(f"backfill: Envelope Index not readable at {IN.ENVELOPE_INDEX} "
              "(Full Disk Access?)", file=sys.stderr)
        return 2

    if args.reset and PROGRESS_PATH.exists():
        PROGRESS_PATH.unlink()
        print("[backfill] cleared progress checkpoint", flush=True)

    resume_from = _load_progress()
    if resume_from:
        print(f"[backfill] resuming from ROWID > {resume_from}", flush=True)

    seen = _prescan_seen()
    print(f"[backfill] pre-scan found {len(seen)} message(s) already on disk", flush=True)

    conn = IN._connect()
    try:
        recips = _load_recipients(conn)
        refs = _load_references(conn)
        print(f"[backfill] loaded recipients for {len(recips)} msgs, "
              f"references for {len(refs)} msgs", flush=True)
        sql, patterns = _build_full_table_sql(cfg)
        rows = conn.execute(sql, (*patterns, resume_from)).fetchall()
    finally:
        conn.close()

    total = len(rows)
    print(f"[backfill] {total} message(s) in scope to process", flush=True)

    stats = {"full": 0, "stub": 0, "skipped": 0, "error": 0}
    last_rowid = resume_from
    t0 = time.monotonic()

    for i, row in enumerate(rows, 1):
        rec = _enrich(row, recips, refs)
        rowid = rec["rowid"]
        mid = (rec["message_id"] or "").strip()

        if mid and mid in seen:
            stats["skipped"] += 1
            last_rowid = rowid
            continue

        if args.dry_run:
            stats["full" if _emlx_present(rec, cfg) else "stub"] += 1
            last_rowid = rowid
            continue

        try:
            result = IN.ingest_row(rec, cfg, seen=seen)
            kind = "full"
            if result is None:
                result = IN.ingest_row_stub(rec, cfg, seen=seen)
                kind = "stub"
            if result is None or result.get("_skip"):
                stats["error"] += 1
            else:
                stats[kind] += 1
        except Exception as e:  # one bad row must not abort the whole rebuild
            print(f"[backfill] error rowid={rowid}: {e!r}", file=sys.stderr, flush=True)
            stats["error"] += 1

        last_rowid = rowid
        if not args.dry_run and i % CHECKPOINT_EVERY == 0:
            _save_progress(last_rowid)
            rate = i / max(time.monotonic() - t0, 1e-6)
            print(f"[backfill] {i}/{total} (full={stats['full']} stub={stats['stub']} "
                  f"skip={stats['skipped']} err={stats['error']}) {rate:.0f}/s", flush=True)

    if not args.dry_run:
        _save_progress(last_rowid)
        last, _ = IN._bootstrap_cursor()
        print(f"[backfill] bootstrapped live cursor to ROWID={last} "
              "(first mailv2-in start won't re-announce the archive)", flush=True)

    elapsed = time.monotonic() - t0
    print(f"[backfill] {'DRY RUN ' if args.dry_run else ''}done in {elapsed:.1f}s: "
          f"full={stats['full']} stub={stats['stub']} skipped={stats['skipped']} "
          f"error={stats['error']}", flush=True)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="mailv2-backfill",
        description="One-time hard-copy of the Envelope Index into the nested "
                    "communication/email tree (full + header-only stubs).",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="count full vs stub without writing or moving the cursor")
    p.add_argument("--reset", action="store_true",
                   help="clear the resume checkpoint and start from ROWID 0")
    return p.parse_args(argv)


def main() -> None:
    sys.exit(run_backfill(parse_args(sys.argv[1:])))


if __name__ == "__main__":
    main()
