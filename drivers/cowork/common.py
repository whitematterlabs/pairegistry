"""Shared helpers for the cowork driver's tracker processes."""
import json
from datetime import datetime, timezone
from pathlib import Path

from boot import paths

STATE_DIR = paths.PAI_ROOT / "sys" / "drivers" / "cowork"
FREEZE_PATH = STATE_DIR / "capture.freeze"

NDJSON_TEXT_CAP = 100_000   # per-line safety cap in the on-disk log
EVENT_TEXT_CAP = 2_000      # cap for text carried inside a kernel event


def capture_enabled() -> bool:
    """Cowork Mode gate: the kernel projects `capabilities.cowork` into
    capture.freeze (presence = disabled). Cheap stat, checked per event."""
    return not FREEZE_PATH.exists()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_ndjson(path: Path, obj: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def event_text(s: str | None) -> tuple[str | None, bool]:
    """Truncate text destined for an event payload (prompt-bound)."""
    if s is None or len(s) <= EVENT_TEXT_CAP:
        return s, False
    return s[:EVENT_TEXT_CAP], True
