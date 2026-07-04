"""approvals driver — owner-gated outbound delivery (draft & approve).

Watches `var/spool/approvals/*.yaml`. A PAI under a send capability in `ask`
mode sends normally; the outbound driver (email/imessage) detects the mode
and stages a record here (status: pending) instead of delivering. The owner
approves it in the web console (status: approved); this driver then delivers
it through the channel's native send path.

Trust model — PAIs are cooperative, not adversarial. The gate stops an
eager PAI from sending on its own, not a PAI deliberately circumventing via
raw shell (it runs as the same unix user). The mechanical backstop is the
per-driver outbound *freeze*/capability-mode check the outbound driver itself
applies, so the PAI's own `action: send` / bare line never leaves. This
driver carries an *approved* email through via a secret token it owns under
`sys/drivers/approvals/grant.token` — written fresh each boot, never exposed
to a PAI's prompt or home view. Only a record the owner moved to `approved`
is ever delivered.

Email reuses the mature macmail-out path: this driver writes a normal
send-draft carrying the token, and the email driver's send gate honors the
token regardless of mode. iMessage has no equivalent draft artifact (its
outbound driver tails day-files, not a drop directory), so it delivers
inline: this driver sends via the same AppleScript helpers the imessage-out
driver uses, then appends the canonical `[HH:MM] me: <text>` line itself.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from datetime import datetime
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from boot import paths
from boot import processes as P
from drivers.approvals import queue as approvals_queue
from drivers.email.macmail import outbound as email_outbound
from drivers.imessage import outbound as imessage_outbound


QUEUE_DIR = paths.var_spool_approvals()
STATE_DIR = paths.PAI_ROOT / "sys" / "drivers" / "approvals"
TOKEN_PATH = STATE_DIR / "grant.token"

# Records the owner hasn't decided on yet.
PENDING = "pending"
# Owner decisions / our outcomes.
APPROVED = "approved"
TERMINAL = {"rejected", "dispatched", "sent", "failed"}

# Per-boot secret the email driver checks to let an approved send through the
# freeze. Set in run(); never logged.
_TOKEN = ""
# Records we've already announced as pending, so a re-read doesn't re-notify.
_announced: set[str] = set()
# Records we've already told the originating channel about, so a re-read
# doesn't re-notify the PAI a second time for the same rejection.
_rejected_notified: set[str] = set()

_atomic_dump = approvals_queue.atomic_dump
_load = approvals_queue.load


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_token() -> str:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_hex(16)
    tmp = TOKEN_PATH.with_suffix(".token.tmp")
    tmp.write_text(tok + "\n")
    os.replace(tmp, TOKEN_PATH)
    try:
        TOKEN_PATH.chmod(0o600)
    except OSError:
        pass
    return tok


def _emit(kind: str, rec: dict, **extra) -> None:
    P.emit_event({
        "source": "approvals",
        "kind": kind,
        "id": rec.get("id"),
        "channel": rec.get("channel"),
        **extra,
    })


def _is_queue_record(path: Path) -> bool:
    return path.suffix == ".yaml" and path.parent.resolve() == QUEUE_DIR.resolve()


def _fail(path: Path, rec: dict, reason: str) -> None:
    rec["status"] = "failed"
    rec["error"] = reason
    rec["decided_at"] = rec.get("decided_at") or _now()
    _atomic_dump(path, rec)
    _emit("failed", rec, reason=reason)
    print(f"[approvals] {rec.get('id')} failed: {reason}", flush=True)


async def _deliver_email(path: Path, rec: dict) -> None:
    action = rec.get("action") or {}
    if not action.get("from"):
        _fail(path, rec, "email record missing `from`")
        return
    if not action.get("to") and not action.get("in_reply_to"):
        _fail(path, rec, "email record has neither `to` nor `in_reply_to`")
        return
    draft: dict = {
        "from": action.get("from"),
        "to": action.get("to") or [],
        "cc": action.get("cc") or [],
        "content": action.get("content") or "",
        "action": "send",
        # The macmail-out send gate lets this through the freeze.
        "approved_token": _TOKEN,
        "approved_id": rec.get("id"),
    }
    if action.get("subject"):
        draft["subject"] = action["subject"]
    if action.get("in_reply_to"):
        draft["in_reply_to"] = action["in_reply_to"]

    drafts_dir = paths.var_spool_email_drafts()
    drafts_dir.mkdir(parents=True, exist_ok=True)
    draft_path = drafts_dir / f"approved-{rec.get('id')}.yaml"
    _atomic_dump(draft_path, draft)

    rec["status"] = "dispatched"
    rec["dispatched_at"] = _now()
    rec["draft_ref"] = str(draft_path.relative_to(paths.PAI_ROOT))
    _atomic_dump(path, rec)
    # No delivery-confirmation event: the email driver's own outbound send
    # nudges the PAI when the message actually leaves. A second "approval
    # dispatched" nudge here is redundant.
    print(
        f"[approvals] {rec.get('id')} approved → handed to email driver "
        f"({draft_path.name})",
        flush=True,
    )


async def _deliver_imessage(path: Path, rec: dict) -> None:
    action = rec.get("action") or {}
    thread = str(action.get("thread") or "").strip()
    text = str(action.get("text") or "")
    if not thread:
        _fail(path, rec, "imessage record missing `thread`")
        return
    if not text.strip():
        _fail(path, rec, "imessage record has empty `text`")
        return

    thread_dir = imessage_outbound.MESSAGES_ROOT / thread
    day_file = thread_dir / f"{datetime.now().date().isoformat()}.md"
    meta = imessage_outbound._load_meta(day_file)
    if not meta or meta.get("channel") != "imessage":
        _fail(path, rec, f"no imessage thread {thread!r} (missing/invalid meta.yaml)")
        return

    try:
        await imessage_outbound._send(meta, text)
    except Exception as e:
        _fail(path, rec, f"send failed: {e}")
        return

    day_file.parent.mkdir(parents=True, exist_ok=True)
    imessage_outbound._append_canonical(day_file, text)

    rec["status"] = "sent"
    rec["dispatched_at"] = _now()
    _atomic_dump(path, rec)
    # No delivery-confirmation event: the appended `me:` line echoes back
    # through chat.db and nudges the PAI as an outbound message. A second
    # "approval sent" nudge here is redundant.
    print(f"[approvals] {rec.get('id')} approved → sent to imessage:{thread}", flush=True)


async def _notify_rejected(rec: dict) -> None:
    """Tell the originating channel a queued send was rejected, so the PAI
    hears about it the same way it hears about any other send failure — no
    new event kind to learn."""
    reason = str(rec.get("error") or "rejected by owner")
    action = rec.get("action") or {}
    channel = str(rec.get("channel") or "")
    if channel == "imessage":
        thread = str(action.get("thread") or "").strip()
        text = str(action.get("text") or "")
        if thread:
            day_file = imessage_outbound.MESSAGES_ROOT / thread / f"{datetime.now().date().isoformat()}.md"
            try:
                imessage_outbound._append_kernel_note(day_file, f"send rejected by owner — {reason}")
            except Exception as e:
                print(f"[approvals] could not append rejection note: {e}", flush=True)
            imessage_outbound._emit_send_failed(thread, text, reason)
    elif channel == "email":
        source_ref = rec.get("source_ref")
        if source_ref:
            draft_path = paths.PAI_ROOT / str(source_ref)
            draft = _load(draft_path)
            if draft is not None:
                draft["draft_state"] = "failed"
                draft["draft_error"] = f"rejected by owner — {reason}"
                _atomic_dump(draft_path, draft)
                account = str(draft.get("from") or "")
                email_outbound._emit_failed(account, draft_path, f"rejected by owner — {reason}")
    _emit("rejected", rec, reason=reason)
    print(f"[approvals] {rec.get('id')} rejected by owner: {reason}", flush=True)


async def _process(path: Path) -> None:
    if not _is_queue_record(path) or not path.exists():
        return
    rec = _load(path)
    if rec is None:
        return
    rec.setdefault("id", path.stem)
    status = str(rec.get("status") or PENDING)
    if status == "rejected":
        if rec["id"] not in _rejected_notified:
            _rejected_notified.add(rec["id"])
            await _notify_rejected(rec)
        return
    if status in TERMINAL:
        return
    if status == PENDING:
        # Announce once so the web surface can badge it. The owner decides.
        if rec["id"] not in _announced:
            _announced.add(rec["id"])
            _emit("pending", rec)
        return
    if status != APPROVED:
        return

    channel = str(rec.get("channel") or "")
    if channel == "email":
        await _deliver_email(path, rec)
    elif channel == "imessage":
        await _deliver_imessage(path, rec)
    else:
        _fail(path, rec, f"channel {channel!r} is not deliverable in approve mode yet")


def _scan_existing() -> list[Path]:
    if not QUEUE_DIR.exists():
        return []
    return [f for f in QUEUE_DIR.glob("*.yaml") if _is_queue_record(f)]


class _Handler(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: "asyncio.Queue[Path]"):
        self.loop = loop
        self.queue = queue

    def _enqueue(self, raw: str) -> None:
        p = Path(raw)
        if p.suffix == ".yaml":
            self.loop.call_soon_threadsafe(self.queue.put_nowait, p)

    def on_created(self, event) -> None:  # type: ignore[override]
        if not event.is_directory:
            self._enqueue(event.src_path)

    def on_modified(self, event) -> None:  # type: ignore[override]
        if not event.is_directory:
            self._enqueue(event.src_path)

    def on_moved(self, event) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        dest = getattr(event, "dest_path", None)
        if dest:
            self._enqueue(dest)


async def run() -> None:
    global _TOKEN

    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    _TOKEN = _ensure_token()
    loop = asyncio.get_running_loop()
    queue: "asyncio.Queue[Path]" = asyncio.Queue()

    observer = Observer()
    observer.schedule(_Handler(loop, queue), str(QUEUE_DIR), recursive=False)
    observer.start()
    print(f"[approvals] watching {QUEUE_DIR}", flush=True)

    # Boot scan: re-evaluate anything already queued (idempotent — terminal
    # records are skipped, pending re-announced only if not seen this run).
    for f in _scan_existing():
        await _process(f)

    try:
        while True:
            path = await queue.get()
            seen = {path}
            while not queue.empty():
                try:
                    seen.add(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            for f in seen:
                await _process(f)
    except asyncio.CancelledError:
        raise
    finally:
        observer.stop()
        observer.join(timeout=2)
        print("[approvals] stopped", flush=True)
