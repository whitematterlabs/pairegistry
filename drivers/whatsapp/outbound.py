"""WhatsApp outbound driver.

Watches communication/whatsapp/ for new bare lines in thread day-files.
A bare line (no `[HH:MM] sender:` prefix) is a send request from a PAI.
We write it to the outbox; whatsapp-in forwards it to the Baileys bridge.

On successful send, we append the canonical [HH:MM] me: line to the
day-file so it's recorded and suppressed on next tail. On failure, we
emit a whatsapp:send_failed event.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from boot import processes as P
from boot import paths

from tailer import Tailer

# ── paths ──────────────────────────────────────────────────────────
PAI_ROOT = Path(os.environ.get("PAI_ROOT", str(Path.home() / ".pai")))
MESSAGES_ROOT = paths.var_spool_communication() / "whatsapp"
PEOPLE_ROOT = paths.var_lib_memory() / "people"
OUTBOX_DIR = PAI_ROOT / "sys" / "drivers" / "whatsapp" / "outbox"

# ── phone slug detection ───────────────────────────────────────────
_PHONE_SLUG = re.compile(r"^\+?\d{7,}$")

# Bracketed lines are log records, not send requests
_BRACKET_LINE = re.compile(r"^\[")


def _owned(path: Path) -> bool:
    """True if this day file lives under our spool root."""
    if path.suffix != ".md":
        return False
    return path.parent.parent in (MESSAGES_ROOT, MESSAGES_ROOT.resolve())


def _resolve_handle(thread_slug: str) -> str:
    """Resolve thread slug to the WhatsApp phone number to send to.

    For phone-number slugs: use the slug directly (strip + if present
    but keep digits).
    For named slugs: look up WhatsApp handle in memory/people/.
    """
    if _PHONE_SLUG.match(thread_slug):
        # Strip + for Baileys JID format
        return thread_slug.lstrip("+")

    person_dir = PEOPLE_ROOT / thread_slug
    about_path = person_dir / "about.yaml"
    if about_path.exists():
        try:
            about = yaml.safe_load(about_path.read_text()) or {}
        except yaml.YAMLError:
            return thread_slug
        handles = about.get("handles", [])
        for h in handles:
            if isinstance(h, str) and _PHONE_SLUG.match(h):
                return h.lstrip("+")

    return thread_slug


def _write_outbox(thread: str, handle: str, text: str) -> Path:
    """Write a send request to the outbox for whatsapp-in to pick up."""
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1_000_000)
    outbox_file = OUTBOX_DIR / f"{ts}-{thread}.json"
    payload = {
        "type": "message",
        "direction": "out",
        "to": handle,
        "body": text,
        "thread": thread,
    }
    outbox_file.write_text(json.dumps(payload))
    return outbox_file


def _append_canonical(day_file: Path, text: str) -> None:
    """Append the canonical [HH:MM] me: line after a successful send."""
    hm = datetime.now().strftime("%H:%M")
    with day_file.open("a") as f:
        f.write(f"[{hm}] me: {text}\n")


async def _handle_line(line: str, day_file: Path) -> None:
    """Process one bare line from a WhatsApp thread day-file."""
    line = line.strip()
    if not line:
        return
    # Skip bracketed log lines — they're records, not requests
    if _BRACKET_LINE.match(line):
        return

    thread_slug = day_file.parent.name
    handle = _resolve_handle(thread_slug)

    print(f"[whatsapp-out] sending to {handle} ({thread_slug}): {line[:50]}...", flush=True)

    try:
        outbox_file = _write_outbox(thread_slug, handle, line)
        # Give the inbound driver a moment to pick up and forward
        # In practice we wait for confirmation; for v1 we optimistically
        # write the canonical line and rely on inbound to surface errors
        _append_canonical(day_file, line)
        print(f"[whatsapp-out] wrote outbox entry {outbox_file.name}", flush=True)
    except Exception as e:
        print(f"[whatsapp-out] send failed: {e!r}", flush=True)
        P.emit_event({
            "source": "whatsapp",
            "kind": "send_failed",
            "thread": thread_slug,
            "text": line,
            "reason": str(e),
        })


async def run() -> None:
    print("[whatsapp-out] starting", flush=True)

    tailer = Tailer(
        roots=[MESSAGES_ROOT],
        owned=_owned,
        on_line=_handle_line,
        name="whatsapp-out",
    )

    try:
        await tailer.run()
    except asyncio.CancelledError:
        print("[whatsapp-out] stopped", flush=True)
        raise
