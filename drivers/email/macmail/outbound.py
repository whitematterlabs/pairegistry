"""macOS Mail.app outbound driver — draft or send.

Watches `var/spool/communication/email/drafts/*.yaml` (single shared dir,
not per-account). Each draft yaml carries a required `from:` field naming
the Mail.app account that should own the message.

By default a yaml is handed to Mail.app via AppleScript `save` — it lands in
Mail's Drafts folder under the right account and the owner reviews + sends
by hand. If the yaml sets `action: send`, the driver instead uses AppleScript
`send` and the message leaves the machine.

Sending is gated by the owner's `capabilities.email_send` grant, read live
from `config.capability_modes()` on every draft (no/ask/yes):
  - `yes` (or a valid approvals token on this draft) — sends directly.
  - `ask`, no token yet — the draft is NOT sent or saved to Mail.app; instead
    it's staged into the owner's `var/spool/approvals/` queue
    (`draft_state: pending_approval`) for the owner to approve/reject in the
    web console. The approvals driver re-delivers an approved item by writing
    a fresh draft carrying a secret token, which this driver honors.
  - `no` — an `action: send` yaml is NOT sent — it's saved as a draft instead
    (content preserved), with `send_blocked` recording why, and a
    `draft_failed` event so the PAI learns the send didn't happen even
    without `--wait`.

Lifecycle (`draft_state` field):
  - missing / "pending"        → re-evaluate on next event
  - "pending_parent"           → reply parent not found yet; retry with backoff
  - "pending_approval"         → terminal until the owner decides; the
                                 approvals driver carries it forward, never
                                 this driver
  - "drafted"                  → terminal success; saved to Mail's Drafts
  - "sent"                     → terminal success; delivered via Mail.app
  - "failed"                   → terminal failure; draft_error explains why

Boot-time scan and watchdog events are equivalent — both trigger
"look at this file, draft/send it if it has no terminal state yet".
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from boot import config
from boot import paths
from boot import processes as P

from drivers.approvals import queue as approvals_queue

from .. import shared
from . import accounts as A


EMAIL_ROOT = paths.var_spool_email()
DRAFTS_DIR = paths.var_spool_email_drafts()


# Draft-and-approve bridge: under capability `ask`, a draft is staged into the
# approvals queue instead of being sent or drafted. An item the owner approves
# is delivered by the approvals driver, which writes a fresh send-draft
# carrying a secret token it owns under sys/drivers/approvals/. A draft whose
# `approved_token` matches that token is allowed through even though the mode
# is still `ask` — this is the approvals driver's own re-delivery, not the
# PAI's direct send. A PAI can't read the token (it's never in a PAI's prompt
# or home view), so it can't forge an approved send.
APPROVALS_TOKEN_PATH = paths.PAI_ROOT / "sys" / "drivers" / "approvals" / "grant.token"


def _approved_via_token(draft: dict) -> bool:
    tok = str(draft.get("approved_token") or "").strip()
    if not tok:
        return False
    try:
        return tok == APPROVALS_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return False


# Reply retry schedule (seconds). Mail may not have synced the parent
# message yet; back off and try again.
REPLY_RETRY_DELAYS = (5.0, 15.0, 30.0)
PARENT_NOT_FOUND_MARKER = "parent message not found"
ACCOUNTS_REFRESH_INTERVAL = 3600.0  # 1h — handles account-add without restart


# ---------- yaml read/write -----------------------------------------------

class _LoadError(Exception):
    """Draft yaml is unreadable or malformed."""


def _load(path: Path) -> Optional[dict]:
    try:
        with path.open() as f:
            return yaml.safe_load(f) or {}
    except OSError:
        return None
    except yaml.YAMLError as e:
        raise _LoadError(f"yaml parse error: {e}") from e


def _atomic_dump(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)


def _is_draft_path(path: Path) -> bool:
    """A file we own: under {EMAIL_ROOT}/drafts/*.yaml (top-level, single dir)."""
    if path.suffix != ".yaml":
        return False
    if path.name.endswith(".tmp"):
        return False
    try:
        rel = path.resolve().relative_to(EMAIL_ROOT.resolve())
    except (ValueError, OSError):
        return False
    parts = rel.parts
    # drafts/{name}.yaml
    return len(parts) == 2 and parts[0] == "drafts"


# ---------- AppleScript ----------------------------------------------------

def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _build_new_message_script(account: str, draft: dict, *, send: bool = False) -> str:
    sender = _esc(account)
    subject = _esc(str(draft.get("subject") or ""))
    content = _esc(str(draft.get("content") or ""))
    to_list = [_esc(a) for a in (draft.get("to") or []) if a]
    cc_list = [_esc(a) for a in (draft.get("cc") or []) if a]
    bcc_list = [_esc(a) for a in (draft.get("bcc") or []) if a]

    recipients = []
    for a in to_list:
        recipients.append(f'  make new to recipient at end of to recipients with properties {{address:"{a}"}}')
    for a in cc_list:
        recipients.append(f'  make new cc recipient at end of cc recipients with properties {{address:"{a}"}}')
    for a in bcc_list:
        recipients.append(f'  make new bcc recipient at end of bcc recipients with properties {{address:"{a}"}}')
    recipients_block = "\n".join(recipients)

    # `save` keeps it in Drafts; `send newMsg` delivers it. The send verb runs
    # after the recipients are attached, outside the inner `tell newMsg` block.
    tail = '  end tell\n  send newMsg\n' if send else '    save\n  end tell\n'

    # `sender` pins which Mail account owns the message. Without it, Mail
    # falls back to the default account regardless of where the yaml lives.
    return (
        'tell application "Mail"\n'
        '  set newMsg to make new outgoing message with properties '
        f'{{sender:"{sender}", subject:"{subject}", content:"{content}", visible:false}}\n'
        '  tell newMsg\n'
        f'{recipients_block}\n'
        f'{tail}'
        'end tell'
    )


def _build_reply_script(account: str, draft: dict, *, send: bool = False) -> str:
    """Reply-shaped message. Locates the parent by Message-ID and uses Mail's
    `reply` to inherit threading + recipients, then saves to Drafts (or sends).

    Note: macOS 15 (Sequoia) dropped the `opens window` parameter on `reply`
    — earlier versions accepted `without opens window` to keep the reply
    hidden, but that now errors with -2741. Reply window will briefly flash;
    we close it after `save`/`send`.
    """
    sender = _esc(account)
    # The archive stores RFC Message-IDs with angle brackets (`<abc@x>`),
    # but Mail's `message id` property is bare — strip them or the
    # `whose message id is` match can never succeed.
    parent = _esc(str(draft.get("in_reply_to") or "").strip().strip("<>"))
    content = _esc(str(draft.get("content") or ""))
    verb = "send replyMsg" if send else "save replyMsg"
    return (
        'tell application "Mail"\n'
        '  set parentMsgs to {}\n'
        '  repeat with mb in (every mailbox)\n'
        '    try\n'
        f'      set parentMsgs to (messages of mb whose message id is "{parent}")\n'
        '      if (count of parentMsgs) > 0 then exit repeat\n'
        '    end try\n'
        '  end repeat\n'
        '  if (count of parentMsgs) is 0 then\n'
        '    error "parent message not found"\n'
        '  end if\n'
        '  set parentMsg to item 1 of parentMsgs\n'
        '  set replyMsg to reply parentMsg\n'
        f'  set sender of replyMsg to "{sender}"\n'
        f'  set content of replyMsg to "{content}"\n'
        f'  {verb}\n'
        '  try\n'
        '    close (every window whose name starts with "Re:")\n'
        '  end try\n'
        'end tell'
    )


async def _run_osascript(script: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    return (
        proc.returncode if proc.returncode is not None else -1,
        stderr.decode("utf-8", errors="replace").strip(),
    )


# ---------- Mail.app account validation ------------------------------------

# AppleScript-derived account config (single source of truth, shared with
# macmail-in via `accounts.yaml`). Refreshed at boot + hourly; consulted by
# `_process` to fail-fast on a `from:` that doesn't match any configured
# Mail.app address (including legitimate iCloud Hide-My-Email aliases).
# Initialized at the top of `run()`.
_accounts_cfg: Optional["A.AccountsConfig"] = None


# ---------- driver core ----------------------------------------------------

def _emit_failed(account: str, path: Path, reason: str) -> None:
    rel_path = str(path.relative_to(paths.PAI_ROOT)) if path.is_absolute() else str(path)
    P.emit_event({
        "source": "email-out",
        "kind": "draft_failed",
        "account": account,
        "path": shared.home_view_path(rel_path),
        "reason": reason,
    })


def _emit_sent(account: str, path: Path) -> None:
    rel_path = str(path.relative_to(paths.PAI_ROOT)) if path.is_absolute() else str(path)
    P.emit_event({
        "source": "email-out",
        "kind": "sent",
        "account": account,
        "path": shared.home_view_path(rel_path),
    })


def _mark_failed(path: Path, draft: dict, account: str, reason: str) -> None:
    draft["draft_state"] = "failed"
    draft["draft_error"] = reason
    draft["drafted_at"] = datetime.now().isoformat(timespec="seconds")
    _atomic_dump(path, draft)
    _emit_failed(account, path, reason)
    print(f"[email-out] draft failed ({account or '?'}/{path.name}): {reason}", flush=True)


async def _process(path: Path) -> None:
    if not _is_draft_path(path):
        return
    if not path.exists():
        return
    try:
        draft = _load(path)
    except _LoadError as e:
        # Common cause: PAI wrote `subject: Re: foo` (unquoted — the `: `
        # makes YAML parse it as a nested mapping). Don't rewrite the file
        # (we'd clobber the user's content), but log + emit so this never
        # silently no-ops again.
        reason = str(e)
        print(f"[email-out] draft unreadable ({path.name}): {reason}", flush=True)
        _emit_failed("", path, reason)
        return
    if draft is None:
        return
    # `draft_state`: "drafted" / "sent" / "failed" / "pending_approval" are
    # terminal — never re-process (an approved item comes back as a *new*
    # draft yaml written by the approvals driver, not a state flip on this
    # one). "pending_parent" is transient; the retry timer re-enqueues it.
    state = draft.get("draft_state")
    if state in ("drafted", "sent", "failed", "pending_approval"):
        return
    if not draft.get("to") and not draft.get("in_reply_to"):
        # Nothing actionable yet (PAI may still be writing).
        return

    account = str(draft.get("from") or "").strip().lower()
    if not account:
        _mark_failed(path, draft, "", "draft is missing required `from:` field")
        return
    # Empty / unset config (AppleScript discovery failed or `_process`
    # invoked before `run()` initialized) → validation disabled, so we
    # don't reject every draft when Mail.app is briefly unavailable.
    cfg = _accounts_cfg
    if cfg is not None and not cfg.is_empty() and not cfg.accepts_from(account):
        known = ", ".join(cfg.all_addresses()) or "<none>"
        _mark_failed(
            path, draft, account,
            f"no Mail.app account for from: {account}; known: {known}",
        )
        return

    # Send only when the yaml explicitly opts in. Gated by the owner's live
    # `capabilities.email_send` mode (no/ask/yes) — a valid approvals token
    # bypasses the mode check entirely, since that's the approvals driver's
    # own re-delivery of an already-approved item, not the PAI's direct send.
    want_send = str(draft.get("action") or "").strip().lower() == "send"
    do_send = False
    blocked_reason: Optional[str] = None
    if want_send:
        if _approved_via_token(draft):
            do_send = True
            draft.pop("send_blocked", None)
        else:
            mode = config.capability_modes().get("email_send", "no")
            if mode == "yes":
                do_send = True
                draft.pop("send_blocked", None)
            elif mode == "ask":
                action: dict = {
                    "from": account,
                    "to": draft.get("to") or [],
                    "cc": draft.get("cc") or [],
                    "bcc": draft.get("bcc") or [],
                    "content": draft.get("content") or "",
                }
                if draft.get("subject"):
                    action["subject"] = draft["subject"]
                if draft.get("in_reply_to"):
                    action["in_reply_to"] = draft["in_reply_to"]
                approvals_queue.stage_pending(
                    "email", action,
                    created_by=draft.get("created_by"),
                    source_ref=str(path.relative_to(paths.PAI_ROOT)),
                )
                draft["draft_state"] = "pending_approval"
                _atomic_dump(path, draft)
                print(
                    f"[email-out] send requested but capability is ask — queued for "
                    f"owner approval ({account}/{path.name})",
                    flush=True,
                )
                return
            else:
                blocked_reason = f"email sends are off (capabilities.email_send={mode})"
                draft["send_blocked"] = blocked_reason
                print(
                    f"[email-out] send requested but capability is {mode} — saving as "
                    f"draft ({account}/{path.name}): {blocked_reason}",
                    flush=True,
                )
    else:
        draft.pop("send_blocked", None)

    if draft.get("in_reply_to"):
        script = _build_reply_script(account, draft, send=do_send)
    else:
        script = _build_new_message_script(account, draft, send=do_send)

    code, err = await _run_osascript(script)
    if code != 0:
        reason = err or f"osascript exit {code}"
        # Reply parent may not be synced yet — retry with backoff before
        # marking terminal.
        if PARENT_NOT_FOUND_MARKER in reason and draft.get("in_reply_to"):
            retries = int(draft.get("draft_retries") or 0)
            if retries < len(REPLY_RETRY_DELAYS):
                delay = REPLY_RETRY_DELAYS[retries]
                draft["draft_state"] = "pending_parent"
                draft["draft_retries"] = retries + 1
                draft["draft_error"] = reason
                _atomic_dump(path, draft)
                print(
                    f"[email-out] reply parent not found; retry {retries + 1}/"
                    f"{len(REPLY_RETRY_DELAYS)} in {delay}s ({account}/{path.name})",
                    flush=True,
                )
                _schedule_retry(path, delay)
                return
        _mark_failed(path, draft, account, reason)
        return

    draft["draft_state"] = "sent" if do_send else "drafted"
    draft.pop("draft_error", None)
    draft.pop("draft_retries", None)
    stamp = datetime.now().isoformat(timespec="seconds")
    if do_send:
        draft["sent_at"] = stamp
        _atomic_dump(path, draft)
        _emit_sent(account, path)
        print(f"[email-out] sent via Mail.app: {account}/{path.name}", flush=True)
    else:
        draft["drafted_at"] = stamp
        _atomic_dump(path, draft)
        if blocked_reason:
            _emit_failed(account, path, blocked_reason)
        print(f"[email-out] drafted to Mail.app: {account}/{path.name}", flush=True)


# ---------- retry scheduling ----------------------------------------------

# Set in `run()`; used by `_schedule_retry` to push the path back onto the
# queue after a delay without blocking the main loop.
_loop: Optional[asyncio.AbstractEventLoop] = None
_queue: Optional[asyncio.Queue] = None


def _schedule_retry(path: Path, delay: float) -> None:
    if _loop is None or _queue is None:
        return
    _loop.call_later(delay, _queue.put_nowait, path)


def _scan_existing() -> list[Path]:
    if not DRAFTS_DIR.exists():
        return []
    return [f for f in DRAFTS_DIR.glob("*.yaml") if _is_draft_path(f)]


# ---------- watchdog plumbing ---------------------------------------------

class _Handler(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[Path]):
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
    global _loop, _queue, _accounts_cfg

    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Path] = asyncio.Queue()
    _loop = loop
    _queue = queue

    _accounts_cfg = await A.refresh()
    if not _accounts_cfg.is_empty():
        print(f"[email-out] known Mail.app accounts: {_accounts_cfg.all_addresses()}", flush=True)
    else:
        print("[email-out] no Mail.app accounts enumerated; from: validation disabled", flush=True)

    async def _refresh_accounts() -> None:
        global _accounts_cfg
        while True:
            await asyncio.sleep(ACCOUNTS_REFRESH_INTERVAL)
            fresh = await A.refresh()
            if not fresh.is_empty() and fresh.accounts != _accounts_cfg.accounts:
                print(f"[email-out] account list changed: {fresh.all_addresses()}", flush=True)
                _accounts_cfg = fresh

    refresh_task = asyncio.create_task(_refresh_accounts())

    observer = Observer()
    observer.schedule(_Handler(loop, queue), str(DRAFTS_DIR), recursive=False)
    observer.start()
    print(f"[email-out] watching {DRAFTS_DIR}", flush=True)

    # Boot scan: any drafts already sitting around get re-evaluated.
    # Idempotent — terminal-state drafts get skipped on the marker check.
    for f in _scan_existing():
        await _process(f)

    try:
        while True:
            path = await queue.get()
            # Coalesce bursts of write events for the same file.
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
        refresh_task.cancel()
        observer.stop()
        observer.join(timeout=2)
        _loop = None
        _queue = None
        print("[email-out] stopped", flush=True)
