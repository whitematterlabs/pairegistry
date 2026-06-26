"""Unit tests for the mailv2 email driver.

Covers the two behaviors mailv2 adds on top of the macmail driver:
  1. The nested `YYYY/MM/DD` partition — `write_message_yaml` writes into it
     and `find_message_by_id` walks it newest-first (write → dedup → `.prev`).
  2. Header-only stubs — `_build_stub_msg_dict` / `ingest_row_stub` produce a
     dedupable, nested yaml from a body-less Envelope-Index row, and the
     in-memory `seen` index keeps the backfill O(n) (no per-insert disk scan).

The mailv2 source is canonical in this repo; these import it directly rather
than via an installed copy, so they run in the pairegistry suite pre-install.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PAI_SRC = ROOT.parent / "pai" / "src"
sys.path[:0] = [str(PAI_SRC), str(ROOT)]

from drivers.mailv2 import shared  # noqa: E402
from drivers.mailv2.macmail import accounts as A  # noqa: E402
from drivers.mailv2.macmail import inbound  # noqa: E402


def _msg(message_id: str, subject: str, received: str, references=None) -> dict:
    return {
        "message_id": message_id,
        "references": references or [],
        "thread_slug": "t",
        "subject": subject,
        "received_at": received,
    }


# ---------- nested tree: write / find / dedup / prev -----------------------

def test_write_message_yaml_uses_nested_partition(tmp_path):
    acc = tmp_path / "owner@example.com"
    path, created = shared.write_message_yaml(
        acc, _msg("<a@x>", "Hello there", "2026-06-25T10:00:00-07:00")
    )
    assert created
    assert path == acc / "2026" / "06" / "25" / "hello-there.yaml"
    assert path.exists()


def test_find_message_by_id_walks_nested_tree(tmp_path):
    acc = tmp_path / "owner@example.com"
    p1, _ = shared.write_message_yaml(acc, _msg("<a@x>", "First", "2026-06-24T09:00:00-07:00"))
    p2, _ = shared.write_message_yaml(acc, _msg("<b@x>", "Second", "2026-06-25T09:00:00-07:00"))
    assert shared.find_message_by_id(acc, "<a@x>") == p1
    assert shared.find_message_by_id(acc, "<b@x>") == p2
    assert shared.find_message_by_id(acc, "<missing@x>") is None


def test_write_message_yaml_dedups_by_message_id(tmp_path):
    acc = tmp_path / "owner@example.com"
    p1, c1 = shared.write_message_yaml(acc, _msg("<a@x>", "Subject", "2026-06-25T10:00:00-07:00"))
    p2, c2 = shared.write_message_yaml(acc, _msg("<a@x>", "Subject", "2026-06-25T11:00:00-07:00"))
    assert c1 is True and c2 is False
    assert p1 == p2


def test_dedup_can_be_disabled_for_backfill(tmp_path):
    acc = tmp_path / "owner@example.com"
    shared.write_message_yaml(acc, _msg("<a@x>", "Subject", "2026-06-25T10:00:00-07:00"))
    # dedup=False skips the find scan → a second write lands as a new file.
    p2, c2 = shared.write_message_yaml(
        acc, _msg("<a@x>", "Subject", "2026-06-25T10:30:00-07:00"), dedup=False
    )
    assert c2 is True
    assert "10-30" in p2.name  # same-day slug collision suffix


def test_link_prev_parents_across_nested_days(tmp_path):
    acc = tmp_path / "owner@example.com"
    pp, _ = shared.write_message_yaml(acc, _msg("<p@x>", "Plan", "2026-06-24T10:00:00-07:00"))
    cp, _ = shared.write_message_yaml(
        acc, _msg("<c@x>", "Re: Plan", "2026-06-25T10:00:00-07:00", references=["<p@x>"])
    )
    parent = shared.find_message_by_id(acc, "<p@x>")
    link = shared.link_prev(cp, parent)
    assert link is not None and link.is_symlink()
    assert link.resolve() == pp.resolve()


# ---------- header-only stubs ----------------------------------------------

def test_build_stub_msg_dict_is_dedupable_and_marked_absent():
    row = {
        "message_id": "<m@x>",
        "from_address": "Bob@X.com",
        "from_name": "Bob",
        "to": ["owner@example.com"],
        "cc": [],
        "references": ["<root@x>", "<p@x>"],
        "subject": "Re: Hi",
    }
    ts = datetime(2026, 6, 25, 10, 0, 0)
    d = inbound._build_stub_msg_dict(row, "inbound", ts, 7)
    assert d["message_id"] == "<m@x>"        # dedupable by Message-ID
    assert d["body_state"] == "absent"
    assert d["content"] == ""
    assert d["from"] == "bob@x.com"          # lowercased
    assert d["from_name"] == "Bob"
    assert d["references"] == ["<root@x>", "<p@x>"]
    assert d["bcc"] == []
    assert d["provider_thread_id"] == "7"
    assert d["received_at"].startswith("2026-06-25T10:00")
    assert d["thread_slug"]
    # key order mirrors the full builder (so stub and full yamls match shape)
    assert list(d)[:4] == ["message_id", "in_reply_to", "references", "thread_slug"]


def _stub_cfg() -> A.AccountsConfig:
    return A.AccountsConfig(
        accounts={
            "UUID": A.Account(uuid="UUID", addresses=["owner@example.com"], inbox_name="INBOX")
        }
    )


def _epoch(y, mo, d, h=10) -> int:
    return int(datetime(y, mo, d, h, 0, 0, tzinfo=timezone.utc).timestamp())


def test_ingest_row_stub_writes_nested_and_dedups_via_seen(tmp_path, monkeypatch):
    monkeypatch.setattr(inbound.paths, "PAI_ROOT", tmp_path)
    cfg = _stub_cfg()
    rec = {
        "rowid": 10,
        "url": "imap://UUID/INBOX",
        "date_received": _epoch(2026, 6, 25),
        "date_sent": _epoch(2026, 6, 25),
        "conversation_id": 1,
        "from_address": "bob@x.com",
        "from_name": "Bob",
        "to": ["owner@example.com"],
        "cc": [],
        "references": [],
        "subject": "Hello stub",
        "message_id": "<s@x>",
    }
    seen: dict = {}
    res = inbound.ingest_row_stub(rec, cfg, seen=seen)
    assert res and res.get("_stub") and res["_created"] is True

    acc_root = tmp_path / "var" / "spool" / "communication" / "email" / "owner@example.com"
    files = list(acc_root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/*.yaml"))
    assert len(files) == 1
    body = yaml.safe_load(files[0].read_text())
    assert body["message_id"] == "<s@x>"
    assert body["body_state"] == "absent"
    assert body["content"] == ""
    assert "<s@x>" in seen

    # Re-running the same row dedups via the seen index — no new file, not created.
    res2 = inbound.ingest_row_stub(rec, cfg, seen=seen)
    assert res2["_created"] is False
    files2 = list(acc_root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/*.yaml"))
    assert len(files2) == 1


# ---------- bounded backlog buckets ----------------------------------------

def test_bump_bucket_caps_samples_and_tracks_earliest():
    b = inbound._new_bucket("a@x")
    base = datetime(2026, 6, 25, 12, 0, 0)
    for i in range(8):
        inbound._bump_bucket(b, f"subj{i}", base.replace(minute=i))
    assert b["count"] == 8
    assert b["last_subject"] == "subj7"
    assert len(b["sample_subjects"]) == inbound.SAMPLE_SUBJECTS_CAP
    assert b["sample_subjects"] == ["subj3", "subj4", "subj5", "subj6", "subj7"]
    assert b["since"].startswith("2026-06-25T12:00")
