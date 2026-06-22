from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAI_SRC = ROOT.parent / "pai" / "src"
sys.path[:0] = [str(PAI_SRC), str(ROOT)]

from drivers.whatsapp import inbound  # noqa: E402


def test_emit_backlog_reports_touched_day_files(monkeypatch) -> None:
    emitted: list[dict] = []
    monkeypatch.setattr(inbound.P, "emit_event", emitted.append)

    inbound._emit_backlog(
        [
            {
                "slug": "tuba-amerika",
                "text": "first",
                "day_file": "var/spool/communication/whatsapp/tuba-amerika/2026-06-21.md",
            },
            {
                "slug": "tuba-amerika",
                "text": "latest",
                "day_file": "var/spool/communication/whatsapp/tuba-amerika/2026-05-24.md",
            },
            {
                "slug": "tuba-amerika",
                "text": "duplicate file",
                "day_file": "var/spool/communication/whatsapp/tuba-amerika/2026-06-21.md",
            },
            {
                "slug": "alper-amerika",
                "text": "other",
                "day_file": "var/spool/communication/whatsapp/alper-amerika/2026-06-03.md",
            },
        ]
    )

    assert len(emitted) == 1
    event = emitted[0]
    assert event["source"] == "whatsapp"
    assert event["kind"] == "backlog"
    assert event["total"] == 4

    threads = {thread["thread"]: thread for thread in event["threads"]}
    assert threads["tuba-amerika"]["inbound"] == 3
    assert threads["tuba-amerika"]["last_text"] == "duplicate file"
    assert threads["tuba-amerika"]["day_files"] == [
        "var/spool/communication/whatsapp/tuba-amerika/2026-05-24.md",
        "var/spool/communication/whatsapp/tuba-amerika/2026-06-21.md",
    ]
    assert threads["alper-amerika"]["day_files"] == [
        "var/spool/communication/whatsapp/alper-amerika/2026-06-03.md",
    ]
