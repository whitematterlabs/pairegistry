"""WhatsApp outbound driver — the owner-gated send path.

Tails threads with `channel: whatsapp` in their meta.yaml. PAI signals a
send by appending a *bare* line (no `[HH:MM] sender:` prefix) to a
day-file: we check the owner's `whatsapp_send` capability, send via the
live Baileys socket if permitted, then append the canonical
`[HH:MM] me: <text>` record. That canonical line is one-shot suppressed on
the tailer so we don't re-read it.

Bracketed lines (`[HH:MM] sender: ...`) are *log entries only* — never
sends. The spool defaults to DENY: with no `whatsapp_send` grant the freeze
file is present and a bare line is consumed with a `kernel: send frozen`
note, never delivered.

Structural divergence from imessage: a WhatsApp send is socket-bound — it
must go through `currentSock.sendMessage()`, which only the bridge process
owns. So this tailer does NOT run as a separate `whatsapp-out` process; it
runs in-process with inbound and sends through a shared `BridgeClient`
(passed into `run`). And an `ask`-mode approval can't be delivered inline by
the approvals driver (it can't reach the socket) — instead the approvals
driver drops a trusted, token'd handoff file under the driver-internal
`sys/drivers/whatsapp/outbox/*.yaml` that this tailer ingests and delivers,
bypassing the freeze. That drop dir is deliberately *outside* the message
spool (and thus out of any PAI's home view) and off the tailer's watched
tree — a second recursive watch on the same path as the tailer collides at
the macOS FSEvents layer ("already scheduled"), silently killing the watcher.

Recipient resolution: whatsapp meta.yaml is minimal (`{channel: whatsapp}`).
The JID is resolved at send time — a bare-digit slug maps to
`<digits>@s.whatsapp.net`; otherwise the phone handle from
`people/<slug>/about.yaml` is used. Resolved handles are written back into
meta.yaml on first send. Known v1 gap: contacts addressable only via `@lid`
aren't resolved here — we try `@s.whatsapp.net` first; a future pass can
reverse-resolve phone→lid from the auth-dir `lid-mapping-*.json` files.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import yaml

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from boot import config
from boot import paths
from boot import processes as P

try:
    from boot import recipient_allowlist
except ImportError:  # kernel predates send_allowlist — fail closed, always ask
    recipient_allowlist = None

from drivers.approvals import queue as approvals_queue

from tailer import Tailer

if TYPE_CHECKING:  # avoid an import cycle — inbound imports this module
    from drivers.whatsapp.inbound import BridgeClient


# Watch the canonical whatsapp spool directly (shared across the fleet; each
# PAI's home view is a symlink into it).
MESSAGES_ROOT = paths.var_spool_communication() / "whatsapp"
PEOPLE_ROOT = paths.var_lib_memory() / "people"
STATE_DIR = paths.PAI_ROOT / "sys" / "drivers" / "whatsapp"
FREEZE_PATH = STATE_DIR / "outbound.freeze"

# The approvals driver owns this token; a handoff file carrying it is trusted
# to bypass the freeze. A PAI can't read it (never in a prompt or home view),
# so it can't forge an approved send.
GRANT_TOKEN_PATH = paths.PAI_ROOT / "sys" / "drivers" / "approvals" / "grant.token"

# Trusted handoff files the approvals driver drops for approved sends. A flat,
# driver-internal dir under sys/ — off the tailer's watched message tree (so the
# outbox watcher's FSEvents watch can't collide with the tailer's) and out of
# every PAI's home view (trust still rests on the grant token, not the path).
OUTBOX_ROOT = STATE_DIR / "outbox"

# Bracketed prefix — log entries (inbound, canonical me:, kernel notes).
# Never treated as send requests; only bare lines are.
BRACKET_LINE = re.compile(r"^\[")
# Intra-message line break marker on outbound bare lines, matching the
# imessage driver: one bare line is always exactly one message; ` ↵ `
# expands to a real newline at send time. (Inbound multi-line texts use
# repeated-prefix lines instead — see inbound._write_message.)
NEWLINE_MARK = "↵"
# Phone slug = all digits (WhatsApp threads for unknown contacts are named
# by the raw phone number).
_PHONE_SLUG = re.compile(r"^\d{7,}$")
_NON_DIGIT = re.compile(r"\D")


def _sends_frozen() -> bool:
    """Frozen unless the owner granted `whatsapp_send: yes` AND no freeze file
    is present. Capability mode is the live source of truth (matching email-out),
    so a `yes`→`ask`/`no` downgrade in config.yaml takes effect on the very next
    send — even before the kernel re-projects the freeze file. This closes the
    window where a just-removed freeze file (from the prior `yes`) would let a
    now-downgraded send slip out. The freeze file is kept as a fail-closed
    backstop: a stray freeze on disk still stops sends even if the config read
    momentarily disagrees."""
    if config.capability_modes().get("whatsapp_send", "no") != "yes":
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
        source = "capabilities.whatsapp_send"
        detail = f"mode={config.capability_modes().get('whatsapp_send', 'no')}"
    if detail:
        return f"WhatsApp sends frozen by {source}: {detail}"
    return f"WhatsApp sends frozen by {source}"


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
    return meta.get("channel") == "whatsapp"


def _digits(value: str) -> str:
    return _NON_DIGIT.sub("", value or "")


def _expand_newlines(text: str) -> str:
    """Expand ` ↵ ` markers (and bare `↵`) into real newlines for delivery."""
    return text.replace(f" {NEWLINE_MARK} ", "\n").replace(NEWLINE_MARK, "\n")


def _recipient_allowlisted(meta: dict, slug: str) -> bool:
    """True iff this thread's resolved JID matches the owner's
    `send_allowlist.whatsapp` rules (phone rules match `<digits>@s.whatsapp.net`;
    group JIDs match exact-string only). Consulted only in `ask` mode."""
    if recipient_allowlist is None or not hasattr(config, "send_allowlist"):
        return False
    jid = _resolve_jid(meta, slug)
    if not jid:
        return False
    return recipient_allowlist.handle_allowed(jid, config.send_allowlist("whatsapp"))


def _resolve_jid(meta: dict, slug: str) -> Optional[str]:
    """Resolve a WhatsApp JID for a thread. An explicit `meta.jid` wins;
    otherwise the first phone-like handle, else a bare-digit slug. Returns
    None when no recipient can be determined."""
    jid = meta.get("jid")
    if isinstance(jid, str) and jid.strip():
        return jid.strip()
    for h in meta.get("handles") or []:
        d = _digits(str(h))
        if d:
            return f"{d}@s.whatsapp.net"
    if _PHONE_SLUG.match(slug):
        return f"{slug}@s.whatsapp.net"
    return None


def _append_kernel_note(day_file: Path, note: str) -> None:
    hm = datetime.now().strftime("%H:%M")
    day_file.parent.mkdir(parents=True, exist_ok=True)
    with day_file.open("a", encoding="utf-8") as f:
        f.write(f"[{hm}] kernel: {note}\n")


def _append_canonical(day_file: Path, text: str) -> str:
    """Append `[HH:MM] me: <text>` to the day-file and return the exact line
    string (for suppression registration)."""
    hm = datetime.now().strftime("%H:%M")
    line = f"[{hm}] me: {text}"
    day_file.parent.mkdir(parents=True, exist_ok=True)
    with day_file.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return line


def _emit_send_failed(thread: str, text: str, reason: str) -> None:
    P.emit_event({
        "source": "whatsapp-out",
        "kind": "send_failed",
        "thread": thread,
        "text": text,
        "reason": reason,
    })


def _emit_sent(thread: str, text: str) -> None:
    P.emit_event({
        "source": "whatsapp-out",
        "kind": "sent",
        "thread": thread,
        "text": text,
    })


def _materialize_meta(thread_dir: Path) -> bool:
    """Ensure thread meta.yaml exists and carries resolved `handles`.

    Inbound already writes a minimal `{channel: whatsapp}` meta; a PAI that
    `mkdir`s a new thread to reach a contact may not. Either way, resolve the
    recipient's handle (from memory/people or a bare-digit slug) and write it
    back so the JID resolves on this and every later send. Returns True if the
    thread is now addressable, False if we have no way to resolve it."""
    meta_path = thread_dir / "meta.yaml"
    slug = thread_dir.name
    meta: dict = {}
    if meta_path.exists():
        try:
            with meta_path.open() as f:
                meta = yaml.safe_load(f) or {}
        except yaml.YAMLError:
            meta = {}
    if not isinstance(meta, dict):
        meta = {}

    handles = [str(h) for h in (meta.get("handles") or []) if h]

    # Source 1: memory/people/{slug}/about.yaml — the address-book path.
    display_name: Optional[str] = meta.get("display_name")
    if not handles:
        person_about = PEOPLE_ROOT / slug / "about.yaml"
        if person_about.exists():
            try:
                with person_about.open() as f:
                    data = yaml.safe_load(f) or {}
                handles = [str(h) for h in (data.get("handles") or []) if h]
                display_name = display_name or data.get("name") or None
            except yaml.YAMLError:
                pass

    # Source 2: slug IS the phone number.
    if not handles and _PHONE_SLUG.match(slug):
        handles = [slug]

    changed = False
    if meta.get("channel") != "whatsapp":
        meta["channel"] = "whatsapp"
        changed = True
    if handles and meta.get("handles") != handles:
        meta["handles"] = handles
        changed = True
    if display_name and meta.get("display_name") != display_name:
        meta["display_name"] = display_name
        changed = True

    if changed or not meta_path.exists():
        with meta_path.open("w") as f:
            yaml.safe_dump(meta, f, sort_keys=False, allow_unicode=True)

    if not handles and not (isinstance(meta.get("jid"), str) and meta["jid"].strip()):
        print(
            f"[whatsapp-out] thread {slug}/ has no phone handle and no jid; "
            f"cannot resolve a recipient",
            flush=True,
        )
        return False
    return True


async def _process_send(path: Path, text: str, client: "BridgeClient") -> bool:
    """Send `text` out through the meta for `path`'s thread. Returns True on
    success, False on freeze/failure/queued-approval (note + event already
    handled)."""
    thread = path.parent.name
    _materialize_meta(path.parent)
    meta = _load_meta(path)
    if not meta or meta.get("channel") != "whatsapp":
        return False

    allowlisted = False
    if _sends_frozen():
        mode = config.capability_modes().get("whatsapp_send", "no")
        if mode == "ask" and _recipient_allowlisted(meta, thread):
            allowlisted = True
        elif mode == "ask":
            approvals_queue.stage_pending("whatsapp", {"thread": thread, "text": text})
            print(f"[whatsapp-out] queued for owner approval: {thread}: {text[:80]}", flush=True)
            try:
                _append_kernel_note(path, "queued for owner approval")
            except Exception as note_err:
                print(f"[whatsapp-out] could not append kernel note: {note_err}", flush=True)
            return False
        else:
            reason = _freeze_reason()
            print(f"[whatsapp-out] send frozen for {thread}: {text[:80]}", flush=True)
            try:
                _append_kernel_note(path, f"send frozen — not sent — {reason}")
            except Exception as note_err:
                print(f"[whatsapp-out] could not append kernel note: {note_err}", flush=True)
            _emit_send_failed(thread, text, reason)
            return False

    jid = _resolve_jid(meta, thread)
    if not jid:
        reason = "could not resolve a WhatsApp recipient for this thread"
        print(f"[whatsapp-out] {reason}: {thread}", flush=True)
        try:
            _append_kernel_note(path, f"send failed — {reason}")
        except Exception as note_err:
            print(f"[whatsapp-out] could not append kernel note: {note_err}", flush=True)
        _emit_send_failed(thread, text, reason)
        return False

    try:
        await client.send(jid, _expand_newlines(text))
    except Exception as e:
        reason = str(e)
        print(f"[whatsapp-out] send failed to {thread}: {reason}", flush=True)
        try:
            _append_kernel_note(path, f"send failed — {reason}")
        except Exception as note_err:
            print(f"[whatsapp-out] could not append kernel note: {note_err}", flush=True)
        _emit_send_failed(thread, text, reason)
        return False

    print(f"[whatsapp-out] sent to {thread} ({jid}): {text[:80]}", flush=True)
    if allowlisted:
        try:
            _append_kernel_note(path, "sent (allowlisted recipient)")
        except Exception as note_err:
            print(f"[whatsapp-out] could not append kernel note: {note_err}", flush=True)
    _emit_sent(thread, text)
    return True


# ── approved-handoff ingestion (email-style trust boundary) ─────────
# The approvals driver can't reach the socket, so an owner-approved send is
# delivered by dropping a token'd file under sys/drivers/whatsapp/outbox/. This
# watcher verifies the token against the approvals grant, then delivers bypassing
# the freeze — the mode gate has already been satisfied by the owner's approval.

def _grant_token() -> str:
    try:
        return GRANT_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _valid_token(token) -> bool:
    tok = str(token or "").strip()
    if not tok:
        return False
    return tok == _grant_token()


def stage_approved_handoff(slug: str, text: str) -> Path:
    """Drop a trusted, token'd handoff file the whatsapp process delivers.

    Imported and called by the approvals driver (a different process) once the
    owner approves a queued whatsapp send. It carries the approvals grant token
    so the whatsapp tailer trusts it through the freeze, plus the thread `slug`
    (the flat drop dir no longer encodes it in the path). Pure Python, no socket
    — safe to call cross-process."""
    OUTBOX_ROOT.mkdir(parents=True, exist_ok=True)
    ident = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = OUTBOX_ROOT / f"{ident}.yaml"
    n = 1
    while path.exists():
        path = OUTBOX_ROOT / f"{ident}-{n}.yaml"
        n += 1
    approvals_queue.atomic_dump(path, {"slug": slug, "text": text, "token": _grant_token()})
    return path


def _is_outbox_file(path: Path) -> bool:
    return path.suffix == ".yaml" and path.parent == OUTBOX_ROOT


async def _process_outbox(path: Path, client: "BridgeClient") -> None:
    """Deliver an owner-approved handoff, bypassing the freeze. Verifies the
    grant token, sends, appends the canonical line, then deletes the file."""
    if not path.exists():
        return
    try:
        with path.open() as f:
            rec = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return
    if not isinstance(rec, dict):
        return
    if not _valid_token(rec.get("token")):
        print(f"[whatsapp-out] rejecting untrusted handoff {path.name} (bad token)", flush=True)
        try:
            path.unlink()
        except OSError:
            pass
        return

    slug = str(rec.get("slug") or "").strip()
    text = str(rec.get("text") or "")
    if not slug or not text.strip():
        try:
            path.unlink()
        except OSError:
            pass
        return

    thread_dir = MESSAGES_ROOT / slug
    _materialize_meta(thread_dir)
    day_file = thread_dir / f"{datetime.now().date().isoformat()}.md"
    meta = _load_meta(day_file) or {}
    jid = _resolve_jid(meta, slug)
    if not jid:
        reason = "could not resolve a WhatsApp recipient for approved send"
        print(f"[whatsapp-out] {reason}: {slug}", flush=True)
        _append_kernel_note(day_file, f"approved send failed — {reason}")
        _emit_send_failed(slug, text, reason)
        try:
            path.unlink()
        except OSError:
            pass
        return

    try:
        await client.send(jid, _expand_newlines(text))
    except Exception as e:
        # Leave the handoff file so a later driver boot retries; the owner
        # already approved it and we don't want to silently drop it.
        reason = str(e)
        print(f"[whatsapp-out] approved send failed to {slug}: {reason}", flush=True)
        _append_kernel_note(day_file, f"approved send failed — {reason}")
        _emit_send_failed(slug, text, reason)
        return

    _tailer_suppress = _append_canonical(day_file, text)
    if _CURRENT_TAILER is not None:
        _CURRENT_TAILER.suppress_next(day_file, _tailer_suppress)
    print(f"[whatsapp-out] delivered approved send to {slug} ({jid}): {text[:80]}", flush=True)
    _emit_sent(slug, text)
    try:
        path.unlink()
    except OSError:
        pass


# Set in build() so the outbox path can suppress the canonical line it appends
# (the day-file tailer would otherwise re-read it as a fresh send request).
_CURRENT_TAILER: Optional[Tailer] = None


class _OutboxHandler(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: "asyncio.Queue[Path]"):
        self.loop = loop
        self.queue = queue

    def _enqueue(self, raw: str) -> None:
        p = Path(raw)
        if _is_outbox_file(p):
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


def _scan_outbox() -> list[Path]:
    if not OUTBOX_ROOT.exists():
        return []
    return sorted(OUTBOX_ROOT.glob("*.yaml"))


async def _run_outbox_watcher(client: "BridgeClient") -> None:
    """Watch sys/drivers/whatsapp/outbox/ for approved handoff files.

    A flat, non-recursive watch on a dir off the tailer's message tree — a
    second recursive watch on the tailer's own root collides at the macOS
    FSEvents layer and kills this watcher's emitter thread."""
    OUTBOX_ROOT.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    queue: "asyncio.Queue[Path]" = asyncio.Queue()

    observer = Observer()
    observer.schedule(_OutboxHandler(loop, queue), str(OUTBOX_ROOT), recursive=False)
    observer.start()
    print(f"[whatsapp-out] watching {OUTBOX_ROOT} for approved sends", flush=True)

    # Boot scan: deliver anything already dropped while we were down.
    for f in _scan_outbox():
        await _process_outbox(f, client)

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
                await _process_outbox(f, client)
    except asyncio.CancelledError:
        raise
    finally:
        observer.stop()
        observer.join(timeout=2)


def build(client: "BridgeClient") -> Tailer:
    global _CURRENT_TAILER
    tailer: Tailer

    async def _on_dir_created(path: Path) -> None:
        try:
            path = path.resolve()
        except OSError:
            return
        if path.parent != MESSAGES_ROOT.resolve():
            return
        if not path.is_dir():
            return
        if not _materialize_meta(path):
            return
        for child in path.iterdir():
            if child.is_file() and child.suffix == ".md":
                await tailer._drain_file(child)  # noqa: SLF001

    async def on_line(path: Path, line: str) -> None:
        if BRACKET_LINE.match(line):
            return  # log entry — inbound, canonical me:, kernel note, etc.
        text = line.rstrip()
        if not text:
            return
        ok = await _process_send(path, text, client)
        if not ok:
            return
        canonical = _append_canonical(path, text)
        tailer.suppress_next(path, canonical)

    tailer = Tailer(
        name="whatsapp-out",
        roots=[MESSAGES_ROOT],
        owned=_owned,
        on_line=on_line,
        on_dir_created=_on_dir_created,
    )
    _CURRENT_TAILER = tailer
    return tailer


async def run(client: "BridgeClient") -> None:
    """Run the day-file tailer and the approved-handoff watcher together.

    Called in-process by the inbound supervisor (not a separate process),
    sharing the live bridge socket via `client`."""
    tailer = build(client)
    await asyncio.gather(
        tailer.run(),
        _run_outbox_watcher(client),
    )
