from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PAI_SRC = ROOT.parent / "pai" / "src"
sys.path[:0] = [str(PAI_SRC), str(ROOT), str(ROOT / "lib")]

from drivers.approvals import driver  # noqa: E402


def _write_rejected(queue_dir: Path, rec_id: str) -> Path:
    path = queue_dir / f"{rec_id}.yaml"
    path.write_text(yaml.safe_dump({
        "id": rec_id,
        "channel": "imessage",
        "status": "rejected",
        "action": {"thread": "habib", "text": "hello"},
        "decided_by": "owner",
        "error": "No, no",
    }))
    return path


def test_rejection_notified_once_across_restarts(tmp_path, monkeypatch):
    """A rejected record still in the spool must not re-notify after a
    driver restart — the notified fact has to survive process memory."""
    monkeypatch.setattr(driver, "QUEUE_DIR", tmp_path)

    calls = []

    async def fake_notify(rec):
        calls.append(rec["id"])

    monkeypatch.setattr(driver, "_notify_rejected", fake_notify)
    path = _write_rejected(tmp_path, "20260705-183300-habib")

    asyncio.run(driver._process(path))
    assert calls == ["20260705-183300-habib"]

    # The notified fact must be persisted in the record itself.
    rec = yaml.safe_load(path.read_text())
    assert rec.get("notified_at")

    # Simulate a kernel/driver restart: in-memory dedupe state is gone.
    monkeypatch.setattr(driver, "_rejected_notified", set())

    asyncio.run(driver._process(path))
    assert calls == ["20260705-183300-habib"]


def test_rejection_dedupes_within_run(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "QUEUE_DIR", tmp_path)

    calls = []

    async def fake_notify(rec):
        calls.append(rec["id"])

    monkeypatch.setattr(driver, "_notify_rejected", fake_notify)
    monkeypatch.setattr(driver, "_rejected_notified", set())
    path = _write_rejected(tmp_path, "20260705-183300-engin")

    asyncio.run(driver._process(path))
    asyncio.run(driver._process(path))
    assert calls == ["20260705-183300-engin"]
