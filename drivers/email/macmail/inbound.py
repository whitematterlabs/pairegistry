"""macOS Mail.app inbound driver.

Watches ~/Library/Mail/V10/MailData/Envelope Index{,-wal,-shm} via kqueue.
On each VNODE event, runs a bounded ROWID-delta query against the index
and emits one `new_email` event per new row whose mailbox is INBOX or a
Sent folder. Sent rows produce outbound yamls — Mail.app's Sent folder is
the canonical record of "what got sent", regardless of whether PAI's
draft pipeline or Arda originated it.

kqueue (not watchdog/FSEvents) for the same reason as imessage: SQLite
modifies the WAL in place via mmap, FSEvents coalesces aggressively.

Requires Full Disk Access. Same Privacy & Security toggle as imessage.
"""

from __future__ import annotations

import asyncio
import os
import pwd
import select
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import yaml

from boot import paths
from boot import processes as P

from .. import shared
from . import accounts as A
from . import emlx as E

# The real macOS user's home, not $HOME — the kernel overrides $HOME per-PAI
# for sandboxing (see boot/nudge.py), so Path.home() would resolve into the
# PAI-local sandbox instead of ~/Library/Mail.
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
ENVELOPE_DIR = REAL_HOME / "Library" / "Mail" / "V10" / "MailData"
ENVELOPE_INDEX = ENVELOPE_DIR / "Envelope Index"

CURSOR_DIR = P.HOME_DIR / "tmp" / "drivers" / "email"
CURSOR_PATH = CURSOR_DIR / "cursor.yaml"

PARKED_DIR = paths.PAI_ROOT / "sys" / "drivers" / "email"
PARKED_PATH = PARKED_DIR / "parked.yaml"

# ~30 minutes at 60s tick. Mail.app finishes downloads well within that;
# anything still parked past this point is almost certainly a row whose
# body Mail abandoned (.partial.emlx orphaned, or message deleted before
# full download). Drop it so the parked map doesn't grow without bound.
MAX_PARK_ATTEMPTS = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _build_delta_sql(cfg: A.AccountsConfig) -> tuple[str, list[str]]:
    """Build the cursor-bounded delta query for the current account list.

    Each known (account, role) pair contributes one `mb.url LIKE ?`
    clause. Roles are derived from Mail.app's own `inbox` / `sent mailbox`
    references — locale-independent.
    """
    patterns = [pat for pat, _role in cfg.url_like_patterns()]
    if not patterns:
        # No accounts known yet (Mail.app not enumerated). Match nothing —
        # `1=0` keeps the query well-formed but returns zero rows.
        clause = "1 = 0"
    else:
        clause = "(" + " OR ".join(["mb.url LIKE ?"] * len(patterns)) + ")"
    sql = f"""
SELECT
    m.ROWID AS rowid,
    m.date_received AS date_received,
    m.conversation_id AS conversation_id,
    mb.url AS url
FROM messages m
JOIN mailboxes mb ON mb.ROWID = m.mailbox
WHERE m.ROWID > ?
  AND {clause}
ORDER BY m.ROWID ASC
"""
    return sql, patterns


def _connect() -> sqlite3.Connection:
    # Same rationale as imessage: open read-write so SQLite can update the
    # wal-index, then use PRAGMA query_only to forbid writes. Read-only
    # mode would only see checkpointed data — we'd miss live mail.
    conn = sqlite3.connect(str(ENVELOPE_INDEX))
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _load_cursor() -> Optional[tuple[int, int]]:
    """Return (last_rowid, last_announced_rowid) or None if no cursor.

    Cursor files written before the announce-tracking change have only
    `last_rowid`. We default `last_announced_rowid` to 0 in that case —
    that's the migration trigger that re-announces the existing on-disk
    backlog exactly once on the next boot.
    """
    if not CURSOR_PATH.exists():
        return None
    with CURSOR_PATH.open() as f:
        data = yaml.safe_load(f) or {}
    val = data.get("last_rowid")
    if val is None:
        return None
    last_announced = data.get("last_announced_rowid", 0)
    return int(val), int(last_announced)


def _atomic_write_yaml(path: Path, data, **dump_kwargs) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(data, f, **dump_kwargs)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _save_cursor(last_rowid: int, last_announced_rowid: int) -> None:
    _atomic_write_yaml(
        CURSOR_PATH,
        {"last_rowid": last_rowid, "last_announced_rowid": last_announced_rowid},
    )


def _bootstrap_cursor() -> tuple[int, int]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(ROWID), 0) AS m FROM messages"
        ).fetchone()
        count = int(
            conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
        )
    last = int(row["m"])
    # Fresh install: nothing to announce; align both fields to MAX(ROWID).
    # Everything already in the index is silently skipped by the live sync,
    # so say so loudly — a missed backfill otherwise surfaces weeks later as
    # "the archive only goes back to install day" (2026-07-07).
    if count:
        print(
            f"[email-in] fresh cursor at ROWID={last}: {count} already-indexed "
            "message(s) are NOT in the archive until the one-time backfill "
            "runs (usr/bin/python -m drivers.email.macmail.backfill)",
            flush=True,
        )
    _save_cursor(last, last)
    return last, last


def _load_parked() -> dict[int, dict]:
    if not PARKED_PATH.exists():
        return {}
    with PARKED_PATH.open() as f:
        data = yaml.safe_load(f) or {}
    out: dict[int, dict] = {}
    for k, v in data.items():
        try:
            out[int(k)] = dict(v) if isinstance(v, dict) else {"first_seen": _now_iso(), "attempts": 0}
        except (TypeError, ValueError):
            continue
    return out


def _save_parked(parked: dict[int, dict]) -> None:
    _atomic_write_yaml(
        PARKED_PATH,
        {int(k): v for k, v in parked.items()},
        sort_keys=True,
    )


def _query_rowids_in(rowids: list[int], cfg: A.AccountsConfig) -> Optional[list]:
    """Fetch a specific set of rowids (used to retry the parked map)."""
    if not rowids:
        return []
    patterns = [pat for pat, _role in cfg.url_like_patterns()]
    if not patterns:
        return []
    placeholders = ",".join(["?"] * len(rowids))
    url_clause = "(" + " OR ".join(["mb.url LIKE ?"] * len(patterns)) + ")"
    sql = f"""
SELECT
    m.ROWID AS rowid,
    m.date_received AS date_received,
    m.conversation_id AS conversation_id,
    mb.url AS url
FROM messages m
JOIN mailboxes mb ON mb.ROWID = m.mailbox
WHERE m.ROWID IN ({placeholders})
  AND {url_clause}
ORDER BY m.ROWID ASC
"""
    try:
        conn = _connect()
    except sqlite3.OperationalError as e:
        print(f"[email-in] cannot open Envelope Index: {e}", flush=True)
        return None
    try:
        return conn.execute(sql, (*rowids, *patterns)).fetchall()
    except sqlite3.OperationalError as e:
        print(f"[email-in] parked retry query failed: {e}", flush=True)
        return None
    finally:
        conn.close()


def _mac_date_to_dt(secs: int) -> datetime:
    # Despite the column name lineage, Mail.app V10 stores date_received as
    # Unix epoch seconds, not Mac epoch (2001-01-01). Treating it as Mac
    # epoch shifts every timestamp forward by 31 years.
    return datetime.fromtimestamp(int(secs), tz=timezone.utc).astimezone()


# ---------- mailbox URL → (account_uuid, direction) -----------------------

def _parse_url(url: str, cfg: A.AccountsConfig) -> tuple[str, Optional[str]]:
    """Return (account_uuid, direction) for a `mailboxes.url` value.

    Account UUID is the netloc (minus any user@ prefix). Direction is
    derived from `accounts.role_for_url`, which checks the URL against
    Mail.app's own concept of inbox/sent — works regardless of locale.
    Returns (uuid, None) if the URL doesn't match any known role
    (caller skips the row).
    """
    parsed = urlparse(url)
    netloc = parsed.netloc
    if "@" in netloc:
        netloc = netloc.split("@", 1)[0]
    return netloc, cfg.role_for_url(url)


# ---------- per-message build + write --------------------------------------

def _ensure_account_meta(account_dir: Path, account_address: str, account_uuid: str) -> None:
    meta_path = account_dir / "meta.yaml"
    if meta_path.exists():
        return
    meta = {
        "account": account_address,
        "provider": "macmail",
        "account_uuid": account_uuid,
        "created": datetime.now().date().isoformat(),
    }
    _atomic_write_yaml(meta_path, meta, sort_keys=False)
    print(f"[email-in] auto-created meta.yaml for {account_address}", flush=True)


def _build_msg_dict(msg, direction: str, ts: datetime, conversation_id: int) -> dict:
    message_id = (msg.get("Message-ID") or msg.get("Message-Id") or "").strip()
    in_reply_to = (msg.get("In-Reply-To") or "").strip() or None
    references = E.header_id_list(msg.get("References"))
    from_addrs = E.extract_addresses(msg.get("From"))
    to_addrs = E.extract_addresses(msg.get("To"))
    cc_addrs = E.extract_addresses(msg.get("Cc"))
    bcc_addrs = E.extract_addresses(msg.get("Bcc"))
    subject = str(msg.get("Subject") or "")
    content = E.extract_text(msg)

    out: dict = {
        "message_id": message_id,
        "in_reply_to": in_reply_to,
        "references": references,
        "thread_slug": shared.thread_slug(subject, references, message_id),
        "from": from_addrs[0]["address"] if from_addrs else "",
        "from_name": from_addrs[0].get("name") if from_addrs else None,
        "to": [a["address"] for a in to_addrs],
        "cc": [a["address"] for a in cc_addrs],
        "bcc": [a["address"] for a in bcc_addrs],
        "subject": subject,
        "direction": direction,
        "content": content,
        "body_state": "present",
        "attachments": [],
        "provider_thread_id": str(conversation_id) if conversation_id else None,
    }
    if direction == "outbound":
        out["sent_at"] = ts.isoformat(timespec="seconds")
    else:
        out["received_at"] = ts.isoformat(timespec="seconds")
    return out


def _build_stub_msg_dict(row, direction: str, ts: datetime, conversation_id: int) -> dict:
    """Header-only message dict built from the Envelope Index — no .emlx body.

    Mirrors `_build_msg_dict`'s key order so a stub yaml is structurally
    identical to a full one except `content` is empty and `body_state` is
    "absent". `message_id` comes from `message_global_data.message_id_header`
    so a later full ingest dedups against the stub by Message-ID.

    `row` is a plain mapping (not a raw sqlite3.Row) carrying the scalar
    columns plus pre-resolved list/string fields the backfill assembles from
    the `addresses` / `recipients` / `message_references` joins:
        rowid, url, date_received, date_sent, conversation_id   (scalars)
        message_id          str   — RFC Message-ID header (may be "")
        from_address        str
        from_name           str | None
        to                  list[str]
        cc                  list[str]
        references          list[str]  — RFC Message-IDs, root-first
        subject             str
    """
    message_id = (row.get("message_id") or "").strip()
    references = list(row.get("references") or [])
    from_addr = (row.get("from_address") or "").strip().lower()
    from_name = row.get("from_name") or None
    to_addrs = list(row.get("to") or [])
    cc_addrs = list(row.get("cc") or [])
    subject = str(row.get("subject") or "")

    out: dict = {
        "message_id": message_id,
        # In-Reply-To isn't stored as such; parenting falls back to
        # references[-1] (the immediate parent) in ingest_row_stub.
        "in_reply_to": None,
        "references": references,
        "thread_slug": shared.thread_slug(subject, references, message_id),
        "from": from_addr,
        "from_name": from_name,
        "to": to_addrs,
        "cc": cc_addrs,
        "bcc": [],
        "subject": subject,
        "direction": direction,
        "content": "",
        "body_state": "absent",
        "attachments": [],
        "provider_thread_id": str(conversation_id) if conversation_id else None,
    }
    if direction == "outbound":
        out["sent_at"] = ts.isoformat(timespec="seconds")
    else:
        out["received_at"] = ts.isoformat(timespec="seconds")
    return out


def _direction_for_message(
    role: str,
    msg,
    cfg: A.AccountsConfig,
    account_uuid: str,
    *,
    all_mail: bool,
) -> str:
    """Return inbound/outbound for a parsed message.

    Gmail can store Inbox-visible messages under `[Gmail]/All Mail` in
    Mail.app's Envelope Index. That mailbox can also contain sent mail, so
    classify All Mail rows by the message's From address.
    """
    if not all_mail:
        return role
    from_addrs = E.extract_addresses(msg.get("From"))
    from_addr = from_addrs[0]["address"] if from_addrs else ""
    return "outbound" if cfg.owns_address(account_uuid, from_addr) else "inbound"


def _persist_msg(
    account_dir: Path,
    msg_dict: dict,
    ts: datetime,
    address: str,
    direction: str,
    *,
    seen: Optional[dict] = None,
    stub: bool = False,
) -> dict:
    """Write the yaml + thread/prev links, return the event payload.

    Shared tail of `ingest_row` and `ingest_row_stub`. When `seen` is None
    (live driver) dedup + parent resolution go through the on-disk
    `find_message_by_id` scan exactly as before. When `seen` is an in-memory
    `{message_id: Path}` map (backfill), it is used for both — keeping the
    bulk rebuild O(n) — and the freshly written path is recorded into it.
    """
    mid = (msg_dict.get("message_id") or "").strip()
    if seen is not None and mid and mid in seen:
        msg_path = seen[mid]
        created = False
    else:
        msg_path, created = shared.write_message_yaml(
            account_dir, msg_dict, dedup=(seen is None)
        )
        if seen is not None and mid:
            seen[mid] = msg_path

    shared.link_thread(account_dir, msg_path, msg_dict["thread_slug"], ts)

    parent_id = msg_dict.get("in_reply_to") or (
        msg_dict["references"][-1] if msg_dict["references"] else None
    )
    if parent_id:
        if seen is not None:
            parent_path = seen.get(parent_id)
        else:
            parent_path = shared.find_message_by_id(account_dir, parent_id)
        if parent_path:
            shared.link_prev(msg_path, parent_path)

    payload = {
        "account": address,
        "thread_slug": msg_dict["thread_slug"],
        "subject": msg_dict["subject"],
        "from": msg_dict["from"],
        "direction": direction,
        "path": shared.home_view_path(str(msg_path.relative_to(paths.PAI_ROOT))),
        "_created": created,
    }
    if stub:
        payload["_stub"] = True
    return payload


def _prepare_full(row, cfg: A.AccountsConfig) -> Optional[dict]:
    """Parse one row's `.emlx` body into a persist-ready bundle — no disk writes.

    This is the expensive half of `ingest_row` (emlx resolve + read + MIME/HTML
    parse) split out so the backfill can run it across a process pool while
    `_persist_prepared` runs serially. Returns the persist bundle, or None when
    the body is unavailable (caller parks the cursor / falls back to a stub), or
    `{"_skip": True}` on an unrecoverable row. The returned dict is picklable
    (Path + datetime + plain dict) so it crosses process boundaries cleanly.
    """
    rowid = int(row["rowid"])
    url = row["url"] or ""
    account_uuid, role = _parse_url(url, cfg)
    if role is None:
        # URL didn't match any known inbox/sent role — shouldn't happen
        # for rows the SQL filter accepts, but skip defensively.
        print(f"[email-in] no role for url={url!r}; skipping rowid={rowid}", flush=True)
        return {"_skip": True}
    all_mail = cfg.is_all_mail_url(url)

    # Mailbox-name path component, used by emlx layout. Mail.app stores
    # `.mbox` directories on disk by the same name shown in the URL.
    mailbox_name = unquote(urlparse(url).path.lstrip("/"))

    path = E.emlx_path(account_uuid, mailbox_name, rowid)
    if path is None:
        # Either the body is still .partial.emlx or Mail hasn't flushed
        # the file yet. Park; next WAL kick will retry.
        return None

    try:
        data = path.read_bytes()
    except OSError as e:
        print(f"[email-in] cannot read {path}: {e}", flush=True)
        return None
    try:
        msg = E.parse_emlx(data)
    except ValueError as e:
        print(f"[email-in] parse failed rowid={rowid}: {e}", flush=True)
        # Bad framing — advance past it; we won't recover by retrying.
        return {"_skip": True}

    address = cfg.address_for_uuid(account_uuid)
    if address is None:
        # AppleScript discovery hasn't enumerated this account yet (rare;
        # account added since last refresh). Skip; the next refresh will
        # pick it up and a future kqueue tick will retry via boot scan.
        print(f"[email-in] no canonical address for uuid={account_uuid}; skipping rowid={rowid}", flush=True)
        return {"_skip": True}

    ts = _mac_date_to_dt(int(row["date_received"] or 0))
    direction = _direction_for_message(role, msg, cfg, account_uuid, all_mail=all_mail)
    msg_dict = _build_msg_dict(msg, direction, ts, int(row["conversation_id"] or 0))

    return {
        "rowid": rowid,
        "account_dir": paths.var_spool_email() / address,
        "account_uuid": account_uuid,
        "address": address,
        "direction": direction,
        "ts": ts,
        "msg_dict": msg_dict,
    }


def _prepare_stub(row, cfg: A.AccountsConfig) -> Optional[dict]:
    """Build the header-only persist bundle for a row whose `.emlx` body is
    unavailable. Disk-write-free counterpart of `_prepare_full` for stubs;
    returns the bundle or `{"_skip": True}`."""
    rowid = int(row["rowid"])
    url = row["url"] or ""
    account_uuid, role = _parse_url(url, cfg)
    if role is None:
        print(f"[email-in] no role for url={url!r}; skipping stub rowid={rowid}", flush=True)
        return {"_skip": True}
    all_mail = cfg.is_all_mail_url(url)

    address = cfg.address_for_uuid(account_uuid)
    if address is None:
        print(f"[email-in] no canonical address for uuid={account_uuid}; skipping stub rowid={rowid}", flush=True)
        return {"_skip": True}

    ts = _mac_date_to_dt(int(row.get("date_received") or row.get("date_sent") or 0))
    from_addr = (row.get("from_address") or "").strip().lower()
    if all_mail:
        direction = "outbound" if cfg.owns_address(account_uuid, from_addr) else "inbound"
    else:
        direction = role
    msg_dict = _build_stub_msg_dict(row, direction, ts, int(row.get("conversation_id") or 0))

    return {
        "rowid": rowid,
        "account_dir": paths.var_spool_email() / address,
        "account_uuid": account_uuid,
        "address": address,
        "direction": direction,
        "ts": ts,
        "msg_dict": msg_dict,
    }


def _persist_prepared(prep: dict, *, seen: Optional[dict] = None, stub: bool = False) -> dict:
    """Serial tail: ensure account meta, then write yaml + thread/prev links for
    a bundle produced by `_prepare_full` / `_prepare_stub`. Must run
    single-threaded (it mutates `seen` and resolves slug collisions / parents
    against the on-disk tree)."""
    _ensure_account_meta(prep["account_dir"], prep["address"], prep["account_uuid"])
    return _persist_msg(
        prep["account_dir"], prep["msg_dict"], prep["ts"],
        prep["address"], prep["direction"], seen=seen, stub=stub,
    )


def ingest_row(row, cfg: A.AccountsConfig, *, seen: Optional[dict] = None) -> Optional[dict]:
    """Process one delta row. Returns an event-payload dict on success or
    None if we should leave the cursor parked (e.g. partial emlx).

    Public so the `backfill` tool can reuse the full row-to-yaml pipeline
    (and fall back to `ingest_row_stub` when this returns None).
    """
    prep = _prepare_full(row, cfg)
    if prep is None:
        return None
    if prep.get("_skip"):
        return {"_skip": True}
    return _persist_prepared(prep, seen=seen)


def ingest_row_stub(row, cfg: A.AccountsConfig, *, seen: Optional[dict] = None) -> Optional[dict]:
    """Header-only ingest for a row whose `.emlx` body is unavailable
    (Mail.app evicted it, or it was never downloaded).

    `row` is the enriched mapping `_build_stub_msg_dict` documents — the
    backfill assembles it from the Envelope Index joins. Writes a stub yaml
    (empty `content`, `body_state: absent`) via the same `write_message_yaml`
    / `link_thread` / `link_prev` path as a full ingest, so the stub dedups
    by Message-ID and threads correctly. Returns the same payload shape as
    `ingest_row` (with `_stub: True`), or `{"_skip": True}`.
    """
    prep = _prepare_stub(row, cfg)
    if prep is None or prep.get("_skip"):
        return {"_skip": True}
    return _persist_prepared(prep, seen=seen, stub=True)


# ---------- live + catchup drains ------------------------------------------

def _query_rows(last_rowid: int, cfg: A.AccountsConfig) -> Optional[list]:
    try:
        conn = _connect()
    except sqlite3.OperationalError as e:
        print(f"[email-in] cannot open Envelope Index: {e}", flush=True)
        return None
    sql, patterns = _build_delta_sql(cfg)
    try:
        return conn.execute(sql, (last_rowid, *patterns)).fetchall()
    except sqlite3.OperationalError as e:
        print(f"[email-in] query failed: {e}", flush=True)
        return None
    finally:
        conn.close()


_last_live_log: tuple[int, int] | None = None

# Boot anchor + stale-bucketing state. Anything whose `date_received` is older
# than (kernel boot - STALE_GRACE) is coalesced into one `email_backlog` event
# the same shape `_drain_catchup` emits, instead of N live `new_email`s. This
# absorbs the "Mail.app indexed an old batch after kernel start" case without
# requiring the owner to pre-warm Mail.app before booting the kernel.
STALE_GRACE = timedelta(minutes=5)
DEBOUNCE_SECONDS = 20.0

# How many recent subjects each backlog account bucket carries. The full
# list lives on disk; the SKILL tells the PAI to rg the date dirs for it.
# Capped so the event payload stays small regardless of backlog size.
SAMPLE_SUBJECTS_CAP = 5

_kernel_started_at: Optional[datetime] = None
_stale_buffer: dict[str, dict] = {}
_stale_earliest: Optional[datetime] = None
_stale_last_add: float = 0.0


def _is_stale(ts: datetime) -> bool:
    if _kernel_started_at is None:
        return False
    return ts < (_kernel_started_at - STALE_GRACE)


def _new_bucket(account: str) -> dict:
    return {
        "account": account,
        "count": 0,
        "last_subject": "",
        "sample_subjects": [],
        "since": None,
    }


def _bump_bucket(bucket: dict, subject: str, ts: datetime) -> None:
    """Fold one message into an account backlog bucket: bump count, track the
    last + a capped tail of recent subjects, and the account's earliest ts."""
    bucket["count"] += 1
    bucket["last_subject"] = subject
    samples = bucket.setdefault("sample_subjects", [])
    samples.append(subject)
    if len(samples) > SAMPLE_SUBJECTS_CAP:
        del samples[:-SAMPLE_SUBJECTS_CAP]  # keep the most recent N
    cur = bucket.get("since")
    if cur is None or ts < datetime.fromisoformat(cur):
        bucket["since"] = ts.isoformat(timespec="seconds")


def _bucket_stale(result: dict, ts: datetime) -> None:
    global _stale_earliest, _stale_last_add
    acc = result["account"]
    bucket = _stale_buffer.setdefault(acc, _new_bucket(acc))
    _bump_bucket(bucket, result["subject"], ts)
    if _stale_earliest is None or ts < _stale_earliest:
        _stale_earliest = ts
    _stale_last_add = time.monotonic()


def _flush_stale() -> None:
    global _stale_buffer, _stale_earliest
    if not _stale_buffer:
        return
    accounts = list(_stale_buffer.values())
    total = sum(b["count"] for b in accounts)
    since = _stale_earliest.isoformat(timespec="seconds") if _stale_earliest else None
    P.emit_event({
        "source": "email",
        "kind": "email_backlog",
        "since": since,
        "accounts": accounts,
        "total": total,
    })
    print(
        f"[email-in] emitted stale backlog (total={total}, accounts={len(accounts)})",
        flush=True,
    )
    _stale_buffer = {}
    _stale_earliest = None


def _park_failure(parked: dict[int, dict], rowid: int) -> None:
    """Bump retry counter for a rowid whose body isn't on disk yet.
    Drops the entry once attempts hit the cap."""
    entry = parked.setdefault(rowid, {"first_seen": _now_iso(), "attempts": 0})
    entry["attempts"] = int(entry.get("attempts", 0)) + 1
    if entry["attempts"] >= MAX_PARK_ATTEMPTS:
        print(
            f"[email-in] giving up on rowid={rowid} after {entry['attempts']} attempts "
            f"(likely deleted before full download)",
            flush=True,
        )
        parked.pop(rowid, None)


def _retry_parked(parked: dict[int, dict], cfg: A.AccountsConfig) -> list[tuple[int, dict, object]]:
    """Re-run ingest_row against currently-parked rowids.

    Returns a list of (rowid, result, row) tuples for rowids that were
    successfully processed (result not None). Successful rowids are
    removed from `parked`; failures bump their attempt counter via
    `_park_failure`. Rowids that no longer appear in the index (deleted
    before completion) are dropped.
    """
    if not parked:
        return []
    rowids = sorted(parked.keys())
    rows = _query_rowids_in(rowids, cfg)
    if rows is None:
        return []
    seen = {int(r["rowid"]): r for r in rows}
    # Rowids that vanished from the index entirely — Mail discarded them.
    for rid in list(parked.keys()):
        if rid not in seen:
            print(f"[email-in] parked rowid={rid} no longer in index; dropping", flush=True)
            parked.pop(rid, None)

    successes: list[tuple[int, dict, object]] = []
    for rid, row in seen.items():
        result = ingest_row(row, cfg)
        if result is None:
            _park_failure(parked, rid)
            continue
        parked.pop(rid, None)
        successes.append((rid, result, row))
    return successes


def _checkpoint(parked: dict[int, dict], last_rowid: int, last_announced: int) -> None:
    """Persist the parked map + cursor together, as one logical step.

    Called immediately after every `emit_event` so a crash/restart can
    never re-announce an email whose event already went out: the on-disk
    `last_announced_rowid` is never more than one emit behind reality.
    Parked is saved first so the cursor never advances past a rowid that
    isn't recorded somewhere.
    """
    _save_parked(parked)
    _save_cursor(last_rowid, last_announced)


def _drain_live(last_rowid: int, cfg: A.AccountsConfig, last_announced: int) -> tuple[int, int]:
    global _last_live_log
    parked = _load_parked()
    orig_announced = last_announced

    # Retry any previously-parked rowids first. These are below `last_rowid`
    # by definition, so they don't affect cursor advancement.
    retry_results = _retry_parked(parked, cfg)
    for rid, result, row in retry_results:
        if result.get("_skip"):
            continue
        if not result.get("_created", True):
            continue
        ts = _mac_date_to_dt(int(row["date_received"] or 0))
        payload = {k: v for k, v in result.items() if not k.startswith("_")}
        if _is_stale(ts):
            _bucket_stale(result, ts)
            print(
                f"[email-in] bucketed (unparked,stale) rowid={rid} → {result['account']} ({result['direction']})",
                flush=True,
            )
        else:
            payload = {"source": "email", "kind": "new_email", **payload}
            P.emit_event(payload)
            print(
                f"[email-in] emitted (unparked) rowid={rid} → {result['account']} ({result['direction']})",
                flush=True,
            )
        if rid > last_announced:
            last_announced = rid
        # Parked rowids are below last_rowid, so the cursor's first field
        # is unchanged here — only last_announced moves.
        _checkpoint(parked, last_rowid, last_announced)

    rows = _query_rows(last_rowid, cfg)
    if rows is None:
        _save_parked(parked)
        if last_announced != orig_announced:
            _save_cursor(last_rowid, last_announced)
        return last_rowid, last_announced
    if rows:
        sig = (last_rowid, len(rows))
        if sig != _last_live_log:
            print(f"[email-in] live drain: {len(rows)} rows since rowid={last_rowid}", flush=True)
            _last_live_log = sig

    max_processed = last_rowid
    for row in rows:
        rowid = int(row["rowid"])
        result = ingest_row(row, cfg)
        if result is None:
            # Body not on disk yet (.partial.emlx). Park it for retry but
            # let the cursor advance — write_message_yaml is idempotent
            # and the parked map handles the retry on subsequent ticks.
            _park_failure(parked, rowid)
            max_processed = max(max_processed, rowid)
            continue
        max_processed = max(max_processed, rowid)
        if result.get("_skip"):
            continue
        if not result.get("_created", True):
            # Already on disk from a prior pass — don't re-emit.
            continue
        ts = _mac_date_to_dt(int(row["date_received"] or 0))
        payload = {k: v for k, v in result.items() if not k.startswith("_")}
        if _is_stale(ts):
            _bucket_stale(result, ts)
            print(
                f"[email-in] bucketed (stale) rowid={rowid} → {result['account']} ({result['direction']})",
                flush=True,
            )
        else:
            payload = {"source": "email", "kind": "new_email", **payload}
            P.emit_event(payload)
            print(f"[email-in] emitted rowid={rowid} → {result['account']} ({result['direction']})", flush=True)
        if rowid > last_announced:
            last_announced = rowid
        # rows is ordered ASC, so max_processed == rowid here and no
        # not-yet-processed row sits below it. Persisting the cursor now
        # means a crash before the loop ends won't re-announce this row.
        _checkpoint(parked, max(max_processed, last_rowid), last_announced)

    _save_parked(parked)
    new_last = max(max_processed, last_rowid)
    if new_last != last_rowid or last_announced != orig_announced:
        _save_cursor(new_last, last_announced)
    return new_last, last_announced


def _drain_catchup(last_rowid: int, cfg: A.AccountsConfig, last_announced: int) -> tuple[int, int]:
    """Boot-time pass — coalesce all missed mail into ONE backlog event so
    PAI gets a single nudge instead of N. Announcement is gated on
    `last_announced_rowid`, not the on-disk write idempotency, so a crash
    between yaml-write and event-emit recovers on the next boot.
    """
    parked = _load_parked()
    summaries: dict[str, dict] = {}
    earliest: Optional[datetime] = None

    def _record(result: dict, row, rowid: int) -> None:
        nonlocal earliest
        if result.get("_skip"):
            return
        if rowid <= last_announced:
            return  # already announced in a prior boot
        acc = result["account"]
        bucket = summaries.setdefault(acc, _new_bucket(acc))
        ts = _mac_date_to_dt(int(row["date_received"] or 0))
        _bump_bucket(bucket, result["subject"], ts)
        if earliest is None or ts < earliest:
            earliest = ts

    # Retry parked rowids first — these are <= last_rowid but may now
    # have their body on disk. Successful ones still count toward the
    # backlog announcement if they're above last_announced.
    for rid, result, row in _retry_parked(parked, cfg):
        _record(result, row, rid)

    rows = _query_rows(last_rowid, cfg)
    if rows is None:
        _save_parked(parked)
        return last_rowid, last_announced

    if rows:
        print(f"[email-in] catchup: {len(rows)} rows since rowid={last_rowid}", flush=True)

    max_processed = last_rowid
    new_announced = last_announced
    for row in rows:
        rowid = int(row["rowid"])
        result = ingest_row(row, cfg)
        if result is None:
            _park_failure(parked, rowid)
            max_processed = max(max_processed, rowid)
            continue
        max_processed = max(max_processed, rowid)
        _record(result, row, rowid)
        if not result.get("_skip") and rowid > new_announced:
            new_announced = rowid

    new_last = max(max_processed, last_rowid)
    final_announced = max(new_announced, last_announced)

    if summaries:
        total = sum(b["count"] for b in summaries.values())
        P.emit_event({
            "source": "email",
            "kind": "email_backlog",
            "since": earliest.isoformat(timespec="seconds") if earliest else None,
            "accounts": list(summaries.values()),
            "total": total,
        })
        # Persist immediately after the emit — a crash here must not let the
        # next boot re-announce this same backlog.
        _checkpoint(parked, new_last, final_announced)
        print(f"[email-in] emitted backlog (total={total}, accounts={len(summaries)})", flush=True)
    else:
        _save_parked(parked)
        if new_last != last_rowid or final_announced != last_announced:
            _save_cursor(new_last, final_announced)

    if parked:
        print(f"[email-in] catchup: {len(parked)} rowid(s) parked for retry", flush=True)
    return new_last, final_announced


# ---------- kqueue watcher (lifted from imessage/inbound.py) ---------------

WATCHED_NAMES = ("Envelope Index-wal",)

VNODE_FLAGS = (
    select.KQ_NOTE_WRITE
    | select.KQ_NOTE_EXTEND
    | select.KQ_NOTE_DELETE
    | select.KQ_NOTE_RENAME
)


class _KqueueWatcher:
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        self.loop = loop
        self.queue = queue
        self._kq: Optional[select.kqueue] = None
        self._fds: dict[int, str] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop_fd_r: Optional[int] = None
        self._stop_fd_w: Optional[int] = None

    def _open_target(self, name: str) -> Optional[int]:
        path = ENVELOPE_DIR / name
        try:
            return os.open(str(path), os.O_RDONLY)
        except FileNotFoundError:
            return None
        except PermissionError:
            # Full Disk Access was lost after run()'s preflight passed. Skip
            # the target rather than crash the driver with a raw EPERM
            # traceback; run() surfaces the actionable hint on (re)start.
            print(_FDA_HINT, flush=True)
            return None

    def _register(self, fd: int, name: str) -> None:
        assert self._kq is not None
        kev = select.kevent(
            fd,
            filter=select.KQ_FILTER_VNODE,
            flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
            fflags=VNODE_FLAGS,
        )
        self._kq.control([kev], 0)
        self._fds[fd] = name

    def start(self) -> None:
        self._kq = select.kqueue()
        r, w = os.pipe()
        self._stop_fd_r, self._stop_fd_w = r, w
        stop_kev = select.kevent(
            r,
            filter=select.KQ_FILTER_READ,
            flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
        )
        self._kq.control([stop_kev], 0)

        for name in WATCHED_NAMES:
            fd = self._open_target(name)
            if fd is not None:
                self._register(fd, name)
                print(f"[email-in] kqueue watching {name} fd={fd}", flush=True)
            else:
                print(f"[email-in] kqueue target {name} missing — skipping", flush=True)

        self._thread = threading.Thread(target=self._loop, name="email-in-kq", daemon=True)
        self._thread.start()

    def _reopen(self, old_fd: int, name: str) -> None:
        try:
            os.close(old_fd)
        except OSError:
            pass
        self._fds.pop(old_fd, None)
        fd = self._open_target(name)
        if fd is not None:
            self._register(fd, name)
            print(f"[email-in] kqueue re-opened {name} fd={fd}", flush=True)

    def _loop(self) -> None:
        assert self._kq is not None
        assert self._stop_fd_r is not None
        try:
            while True:
                events = self._kq.control([], 16, None)
                stop = False
                for ev in events:
                    if ev.ident == self._stop_fd_r:
                        stop = True
                        continue
                    fd = ev.ident
                    name = self._fds.get(fd, f"fd={fd}")
                    fflags = ev.fflags
                    self.loop.call_soon_threadsafe(self.queue.put_nowait, None)
                    if fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME):
                        print(f"[email-in] kq {name} rotated, reopening", flush=True)
                        self._reopen(fd, name)
                if stop:
                    return
        except Exception as e:
            print(f"[email-in] kq-loop crashed: {e!r}", flush=True)

    def stop(self) -> None:
        if self._stop_fd_w is not None:
            try:
                os.write(self._stop_fd_w, b"x")
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        for fd in list(self._fds):
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()
        for fd in (self._stop_fd_r, self._stop_fd_w):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._stop_fd_r = self._stop_fd_w = None
        if self._kq is not None:
            self._kq.close()
            self._kq = None


# Surfaced when the Mail store exists but macOS refuses the read. The only
# cause is a missing Full Disk Access grant, and there is no system prompt for
# it — so we log an actionable hint instead of a raw EPERM traceback or the
# misleading "not found" (`Path.exists()` returns False under a TCC denial,
# which used to mask a permission wall as an absent mailbox).
_FDA_HINT = (
    "[email-in] cannot read Mail: PAI is missing Full Disk Access. Grant it in "
    "System Settings → Privacy & Security → Full Disk Access, then restart "
    "PAI. Driver idle until granted."
)


def _index_access() -> str:
    """Classify access to the Envelope Index without raising.

    Returns 'ok' (exists and readable), 'denied' (exists but the open was
    refused — Full Disk Access missing), or 'absent' (no Mail store here).
    Unlike ``Path.exists()``, a TCC denial is reported as 'denied' rather than
    silently collapsing to "absent".
    """
    try:
        fd = os.open(str(ENVELOPE_INDEX), os.O_RDONLY)
    except PermissionError:
        return "denied"
    except FileNotFoundError:
        return "absent"
    except OSError:
        # Any other open failure (e.g. an unreadable parent directory) is far
        # more likely a permission wall than a genuinely absent store — surface
        # the actionable FDA hint rather than going idle as if Mail is unset.
        return "denied"
    os.close(fd)
    return "ok"


async def run() -> None:
    global _kernel_started_at
    access = _index_access()
    if access == "denied":
        print(_FDA_HINT, flush=True)
        return
    if access == "absent":
        print(f"[email-in] Envelope Index not found at {ENVELOPE_INDEX}; driver idle", flush=True)
        return

    _kernel_started_at = datetime.now(timezone.utc).astimezone()

    cfg = await A.refresh()
    print(f"[email-in] discovered accounts: {A.summarize(cfg)}", flush=True)

    cursor = _load_cursor()
    if cursor is None:
        try:
            cursor = _bootstrap_cursor()
            print(f"[email-in] bootstrap cursor last_rowid={cursor[0]}", flush=True)
        except sqlite3.OperationalError as e:
            print(f"[email-in] cannot bootstrap (FDA granted?): {e}", flush=True)
            return
    last_rowid, last_announced = cursor

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    watcher = _KqueueWatcher(loop, queue)
    watcher.start()
    print(
        f"[email-in] started, last_rowid={last_rowid}, last_announced_rowid={last_announced}",
        flush=True,
    )

    last_rowid, last_announced = await asyncio.to_thread(
        _drain_catchup, last_rowid, cfg, last_announced
    )

    # Periodic safety-net poll: kqueue catches WAL writes, but a row stuck on
    # `.partial.emlx` only unparks when Mail finishes the download — which may
    # or may not touch the WAL again before some unrelated activity does. A
    # cheap 60s tick guarantees we retry parked rows in bounded time.
    # The same tick refreshes the AppleScript-derived accounts config so
    # newly-added Mail.app accounts are picked up without a kernel restart.
    POLL_INTERVAL = 60.0
    ACCOUNTS_REFRESH_EVERY = 60  # ticks → 1h

    async def _ticker() -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            queue.put_nowait(None)

    async def _stale_flusher() -> None:
        while True:
            await asyncio.sleep(5)
            if _stale_buffer and (time.monotonic() - _stale_last_add) > DEBOUNCE_SECONDS:
                _flush_stale()

    ticker_task = asyncio.create_task(_ticker())
    flusher_task = asyncio.create_task(_stale_flusher())
    ticks = 0

    try:
        while True:
            await queue.get()
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            ticks += 1
            if ticks % ACCOUNTS_REFRESH_EVERY == 0:
                fresh = await A.refresh()
                if fresh.accounts and fresh.accounts != cfg.accounts:
                    print(f"[email-in] accounts refreshed: {A.summarize(fresh)}", flush=True)
                    cfg = fresh
            last_rowid, last_announced = await asyncio.to_thread(
                _drain_live, last_rowid, cfg, last_announced
            )
    except asyncio.CancelledError:
        raise
    finally:
        ticker_task.cancel()
        flusher_task.cancel()
        watcher.stop()
        print("[email-in] stopped", flush=True)
