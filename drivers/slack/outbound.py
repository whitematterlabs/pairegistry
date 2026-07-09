"""Slack outbound driver — the owner-gated send path.

Tails threads with `channel: slack` in their meta.yaml. PAI signals a send by
appending a *bare* line (no `[HH:MM] sender:` prefix) to a day-file: we check
the owner's `slack_send` capability, send via `chat.postMessage` if permitted,
then append the canonical `[HH:MM] me: <text>` record. That canonical line is
one-shot suppressed on the tailer so we don't re-read it.

Bracketed lines (`[HH:MM] sender: ...`) are *log entries only* — never sends.
The spool defaults to DENY: with no `slack_send` grant the freeze file is
present and a bare line is consumed with a `kernel: send frozen` note, never
delivered.

Structural note: a Slack send is a stateless HTTP call (not socket-bound), so —
unlike whatsapp — this runs as its own `slack-out` process (the imessage
layout), and an `ask`-mode approval is delivered *inline* by the approvals
driver (it can call `_send` directly), with no token'd outbox hand-off.

Recipient resolution: the thread's meta.yaml carries the Slack `slack_channel`
id, `channel_type`, and the `thread_ts` a reply should thread into — all
written by inbound. slack-out reads them to address chat.postMessage; a thread
with no `slack_channel` isn't owned (inbound never saw it) and is left alone.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from boot import config
from boot import paths
from boot import processes as P

from drivers.approvals import queue as approvals_queue
from drivers.slack import tokens as slack_tokens

from tailer import Tailer


# Watch the canonical slack spool directly (shared across the fleet; each PAI's
# home view is a symlink into it).
MESSAGES_ROOT = paths.var_spool_communication() / "slack"
STATE_DIR = paths.PAI_ROOT / "sys" / "drivers" / "slack"
FREEZE_PATH = STATE_DIR / "outbound.freeze"

# Bracketed prefix — log entries (inbound, canonical me:, kernel notes). Never
# treated as send requests; only bare lines are.
BRACKET_LINE = re.compile(r"^\[")


def _sends_frozen() -> bool:
    """Frozen unless the owner granted `slack_send: yes` AND no freeze file is
    present. Capability mode is the live source of truth (matching the other
    send channels), so a `yes`→`ask`/`no` downgrade in config.yaml takes effect
    on the very next send. The freeze file is a fail-closed backstop: a stray
    freeze on disk still stops sends even if the config read momentarily
    disagrees."""
    if config.capability_modes().get("slack_send", "no") != "yes":
        return True
    return FREEZE_PATH.exists()


def _freeze_reason() -> str:
    if FREEZE_PATH.exists():
        source = str(FREEZE_PATH)
        try:
            detail = FREEZE_PATH.read_text(encoding="utf-8").strip().splitlines()[0]
        except (FileNotFoundError, IndexError, OSError):
            detail = ""
    else:
        source = "capabilities.slack_send"
        detail = f"mode={config.capability_modes().get('slack_send', 'no')}"
    if detail:
        return f"Slack sends frozen by {source}: {detail}"
    return f"Slack sends frozen by {source}"


def _load_meta(day_file: Path) -> Optional[dict]:
    meta_path = day_file.parent / "meta.yaml"
    if not meta_path.exists():
        return None
    try:
        with meta_path.open() as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError:
        return None


def _owned(path: Path) -> bool:
    if path.suffix != ".md":
        return False
    if path.parent.parent != MESSAGES_ROOT.resolve() and path.parent.parent != MESSAGES_ROOT:
        return False
    meta = _load_meta(path)
    if not meta:
        return False
    return meta.get("channel") == "slack"


def _client():
    """Build a Slack WebClient, or None if the SDK or bot token is missing."""
    tok = slack_tokens.bot_token()
    if not tok:
        return None
    try:
        from slack_sdk.web import WebClient
    except Exception:  # noqa: BLE001 — SDK not installed
        return None
    return WebClient(token=tok)


async def _send(meta: dict, text: str) -> None:
    """Post one message via chat.postMessage. Raises on any failure."""
    channel = str(meta.get("slack_channel") or "").strip()
    if not channel:
        raise RuntimeError("thread meta.yaml missing slack_channel")
    web = _client()
    if web is None:
        raise RuntimeError("slack_sdk not installed or bot token missing")
    kwargs: dict = {"channel": channel, "text": text}
    thread_ts = meta.get("thread_ts")
    if thread_ts:
        kwargs["thread_ts"] = str(thread_ts)
    # chat.postMessage is a blocking HTTP call — keep it off the event loop.
    resp = await asyncio.to_thread(lambda: web.chat_postMessage(**kwargs))
    if not resp.get("ok", False):
        raise RuntimeError(resp.get("error") or "chat.postMessage returned not-ok")


def _append_kernel_note(day_file: Path, note: str) -> None:
    hm = datetime.now().strftime("%H:%M")
    day_file.parent.mkdir(parents=True, exist_ok=True)
    with day_file.open("a", encoding="utf-8") as f:
        f.write(f"[{hm}] kernel: {note}\n")


def _append_canonical(day_file: Path, text: str) -> str:
    """Append `[HH:MM] me: <text>` and return the exact line (for suppression)."""
    hm = datetime.now().strftime("%H:%M")
    line = f"[{hm}] me: {text}"
    day_file.parent.mkdir(parents=True, exist_ok=True)
    with day_file.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return line


def _emit_send_failed(thread: str, text: str, reason: str) -> None:
    P.emit_event({
        "source": "slack-out",
        "kind": "send_failed",
        "thread": thread,
        "text": text,
        "reason": reason,
    })


def _emit_sent(thread: str, text: str) -> None:
    P.emit_event({
        "source": "slack-out",
        "kind": "sent",
        "thread": thread,
        "text": text,
    })


async def _process_send(path: Path, text: str) -> bool:
    """Send `text` out through the meta for `path`'s thread. Returns True on
    success, False on freeze/failure/queued-approval (note + event already
    handled)."""
    meta = _load_meta(path)
    if not meta or meta.get("channel") != "slack":
        return False
    thread = path.parent.name

    if _sends_frozen():
        mode = config.capability_modes().get("slack_send", "no")
        if mode == "ask":
            approvals_queue.stage_pending("slack", {"thread": thread, "text": text})
            print(f"[slack-out] queued for owner approval: {thread}: {text[:80]}", flush=True)
            try:
                _append_kernel_note(path, "queued for owner approval")
            except Exception as note_err:  # noqa: BLE001
                print(f"[slack-out] could not append kernel note: {note_err}", flush=True)
            return False
        reason = _freeze_reason()
        print(f"[slack-out] send frozen for {thread}: {text[:80]}", flush=True)
        try:
            _append_kernel_note(path, f"send frozen — not sent — {reason}")
        except Exception as note_err:  # noqa: BLE001
            print(f"[slack-out] could not append kernel note: {note_err}", flush=True)
        _emit_send_failed(thread, text, reason)
        return False

    try:
        await _send(meta, text)
    except Exception as e:  # noqa: BLE001
        reason = str(e)
        print(f"[slack-out] send failed to {thread}: {reason}", flush=True)
        try:
            _append_kernel_note(path, f"send failed — {reason}")
        except Exception as note_err:  # noqa: BLE001
            print(f"[slack-out] could not append kernel note: {note_err}", flush=True)
        _emit_send_failed(thread, text, reason)
        return False

    print(f"[slack-out] sent to {thread}: {text[:80]}", flush=True)
    _emit_sent(thread, text)
    return True


def build() -> Tailer:
    tailer: Tailer

    async def on_line(path: Path, line: str) -> None:
        if BRACKET_LINE.match(line):
            return  # log entry — inbound, canonical me:, kernel note, etc.
        text = line.rstrip()
        if not text:
            return
        ok = await _process_send(path, text)
        if not ok:
            return
        canonical = _append_canonical(path, text)
        tailer.suppress_next(path, canonical)

    tailer = Tailer(
        name="slack-out",
        roots=[MESSAGES_ROOT],
        owned=_owned,
        on_line=on_line,
    )
    return tailer


async def run() -> None:
    await build().run()
