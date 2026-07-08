"""Shared helpers for the cowork driver's tracker processes."""
import json
from datetime import datetime, timezone
from pathlib import Path

from boot import paths

STATE_DIR = paths.PAI_ROOT / "sys" / "drivers" / "cowork"

# One freeze file per capture facet: the kernel projects the matching
# `capabilities.cowork_<facet>` flag into it (presence = that facet disabled).
# The pre-split single capture.freeze is gone — the kernel removes it on
# every projection pass.
FREEZE_PATHS = {
    "window": STATE_DIR / "window.freeze",
    "clipboard": STATE_DIR / "clipboard.freeze",
    "files": STATE_DIR / "files.freeze",
}

NDJSON_TEXT_CAP = 100_000   # per-line safety cap in the on-disk log
EVENT_TEXT_CAP = 2_000      # cap for text carried inside a kernel event


def capture_enabled(facet: str) -> bool:
    """Per-facet gate ('window' | 'clipboard' | 'files'). Cheap stat,
    checked per event."""
    return not FREEZE_PATHS[facet].exists()


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
