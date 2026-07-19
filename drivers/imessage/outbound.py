"""iMessage outbound driver.

Tails threads with `channel: imessage` in their meta.yaml. PAI signals a
send by appending a *bare* line (no `[HH:MM] sender:` prefix) to a
day-file: we send it via Messages.app (osascript), then append the
canonical `[HH:MM] me: <text>` record to the same file. That canonical
line is one-shot suppressed on the tailer so we don't re-read it.

Bracketed lines (`[HH:MM] sender: ...`) are *log entries only* — never
sends. This includes `me:` lines the kernel writes when chat.db echoes
back a message Arda sent from his phone: those lines are the record of
the send, not a request to re-send.

New thread dirs — when `/home/pai/messages/{slug}/` appears with no
meta.yaml, we materialize one from `memory/people/{slug}/about.yaml` (or,
for a raw phone/email slug, from the slug itself). PAI's workflow
collapses to `mkdir /home/pai/messages/{slug} && echo "text" >> /home/pai/messages/{slug}/$(date +%F).md`.

Tries iMessage first, falls back to SMS if iMessage errors (covers
Android contacts when "Send as SMS" isn't doing the fallback itself).
SMS fallback requires Text Message Forwarding from your iPhone.

Permanent failures (both services error) are surfaced to PAI via a
`kernel: send failed` note in the thread day-file and a `send_failed`
event. The tailer cursor advances on failure or freeze so we don't retry forever.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from boot import config
from boot import outbound_echo
from boot import processes as P

from boot import paths

from drivers.approvals import queue as approvals_queue

from tailer import Tailer

# Watch the canonical spool directly. v3: messages live at
# /var/spool/communication/ and are shared across the fleet (each PAI's
# /home/<pai>/communication/ is just a symlink view); the outbound
# driver is system-shared, not per-PAI.
MESSAGES_ROOT = paths.var_spool_messages()
PEOPLE_ROOT = paths.var_lib_memory() / "people"
STATE_DIR = paths.PAI_ROOT / "sys" / "drivers" / "imessage"
FREEZE_PATH = STATE_DIR / "outbound.freeze"

# Bracketed prefix — log entries (inbound, canonical me:, kernel notes).
# Never treated as send requests; only bare lines are.
BRACKET_LINE = re.compile(r"^\[")
# Intra-message line break marker. Inbound flattens multi-line texts to a
# single day-file line with ` ↵ ` (messages._append_day_file); outbound
# expands the same marker back to real newlines at send time, so one bare
# line is always exactly one message and the log round-trips symmetrically.
NEWLINE_MARK = "↵"
# Phone slug = all digits (after earlier `h`-prefix removal); email slug
# contains `@` (unusual but handled).
_PHONE_SLUG = re.compile(r"^\d{7,}$")


def _sends_frozen() -> bool:
    """Frozen unless the owner granted `imessage_send: yes` AND no freeze file
    is present. Capability mode is the live source of truth (matching email-out),
    so a `yes`→`ask`/`no` downgrade in config.yaml takes effect on the very next
    send — even before the kernel re-projects the freeze file. This closes the
    window where a just-removed freeze file (from the prior `yes`) would let a
    now-downgraded send slip out. The freeze file is kept as a fail-closed
    backstop: a stray freeze on disk still stops sends even if the config read
    momentarily disagrees."""
    if config.capability_modes().get("imessage_send", "no") != "yes":
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
        source = "capabilities.imessage_send"
        detail = f"mode={config.capability_modes().get('imessage_send', 'no')}"
    if detail:
        return f"iMessage sends frozen by {source}: {detail}"
    return f"iMessage sends frozen by {source}"


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
    return meta.get("channel") == "imessage"


def _expand_newlines(text: str) -> str:
    """Expand ` ↵ ` markers (and bare `↵`) into real newlines for delivery."""
    return text.replace(f" {NEWLINE_MARK} ", "\n").replace(NEWLINE_MARK, "\n")


def _escape_applescript(text: str) -> str:
    # Interpolated into an AppleScript double-quoted string. Newlines can't
    # sit raw inside the literal — splice them as `" & linefeed & "`.
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", '" & linefeed & "')
    )


def _applescript_for_1to1(handle: str, text: str, service_type: str) -> str:
    h = handle.replace("\\", "\\\\").replace('"', '\\"')
    t = _escape_applescript(text)
    return (
        'tell application "Messages"\n'
        f'  set targetService to 1st service whose service type = {service_type}\n'
        f'  set targetBuddy to buddy "{h}" of targetService\n'
        f'  send "{t}" to targetBuddy\n'
        'end tell'
    )


def _applescript_for_group(chat_guid: str, text: str) -> str:
    g = chat_guid.replace("\\", "\\\\").replace('"', '\\"')
    t = _escape_applescript(text)
    return (
        'tell application "Messages"\n'
        f'  set targetChat to chat id "{g}"\n'
        f'  send "{t}" to targetChat\n'
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


def _append_kernel_note(day_file: Path, note: str) -> None:
    hm = datetime.now().strftime("%H:%M")
    with day_file.open("a", encoding="utf-8") as f:
        f.write(f"[{hm}] kernel: {note}\n")


async def _send(meta: dict, text: str) -> str:
    """Send one line; return the service used. Raises on permanent failure.

    `text` is the day-file form (single line, ` ↵ ` markers); the delivered
    message carries real newlines. Covers both the tailer path and the
    approvals driver, which calls `_send` directly on approve."""
    text = _expand_newlines(text)
    if meta.get("group"):
        chat_guid = meta.get("chat_guid")
        if not chat_guid:
            raise RuntimeError("group thread missing chat_guid")
        script = _applescript_for_group(chat_guid, text)
        code, err = await _run_osascript(script)
        if code != 0:
            raise RuntimeError(f"group send failed: exit {code} — {err}")
        return "iMessage"

    handles = meta.get("handles") or []
    if not handles:
        raise RuntimeError("1:1 thread missing handles")

    errors: list[str] = []
    for service in ("iMessage", "SMS"):
        script = _applescript_for_1to1(handles[0], text, service)
        code, err = await _run_osascript(script)
        if code == 0:
            return service
        errors.append(f"{service}: exit {code} — {err}")
    raise RuntimeError(" | ".join(errors))


def _emit_send_failed(thread: str, text: str, reason: str) -> None:
    P.emit_event({
        "source": "imessage-out",
        "kind": "send_failed",
        "thread": thread,
        "text": text,
        "reason": reason,
    })


def _append_canonical(day_file: Path, text: str) -> str:
    """Append `[HH:MM] me: <text>` to the day-file and return the exact
    line string (for suppression registration)."""
    hm = datetime.now().strftime("%H:%M")
    line = f"[{hm}] me: {text}"
    with day_file.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return line


def _emit_sent(thread: str, text: str, service: str) -> None:
    P.emit_event({
        "source": "imessage-out",
        "kind": "sent",
        "thread": thread,
        "text": text,
        "service": service,
    })


async def _process_send(path: Path, text: str) -> bool:
    """Send `text` out through the meta for `path`'s thread. Returns True
    on success, False on permanent failure or a queued approval (note + event
    already emitted; a queued approval is not a failure, but there is nothing
    left for the tailer to do with this line either way)."""
    meta = _load_meta(path)
    if not meta or meta.get("channel") != "imessage":
        return False
    thread = path.parent.name
    if _sends_frozen():
        mode = config.capability_modes().get("imessage_send", "no")
        if mode == "ask":
            approvals_queue.stage_pending("imessage", {"thread": thread, "text": text})
            print(f"[imessage-out] queued for owner approval: {thread}: {text[:80]}", flush=True)
            try:
                _append_kernel_note(path, "queued for owner approval")
            except Exception as note_err:
                print(f"[imessage-out] could not append kernel note: {note_err}", flush=True)
            return False
        reason = _freeze_reason()
        print(f"[imessage-out] send frozen for {thread}: {text[:80]}", flush=True)
        try:
            _append_kernel_note(path, f"send frozen — not sent — {reason}")
        except Exception as note_err:
            print(f"[imessage-out] could not append kernel note: {note_err}", flush=True)
        _emit_send_failed(thread, text, reason)
        return False
    try:
        service = await _send(meta, text)
    except Exception as e:
        reason = str(e)
        print(f"[imessage-out] send failed to {thread}: {reason}", flush=True)
        try:
            _append_kernel_note(path, f"send failed — {reason}")
        except Exception as note_err:
            print(f"[imessage-out] could not append kernel note: {note_err}", flush=True)
        _emit_send_failed(thread, text, reason)
        return False
    print(f"[imessage-out] sent to {thread} via {service}: {text[:80]}", flush=True)
    _emit_sent(thread, text, service)
    return True


def _materialize_meta(thread_dir: Path) -> bool:
    """Create meta.yaml for a new thread dir from memory/people or the slug
    itself. Returns True if meta.yaml now exists (either created or already
    present), False if we have no way to populate it."""
    meta_path = thread_dir / "meta.yaml"
    if meta_path.exists():
        return True
    slug = thread_dir.name

    # Source 1: memory/people/{slug}/about.yaml — the address-book path.
    person_about = PEOPLE_ROOT / slug / "about.yaml"
    handles: list[str] = []
    display_name: Optional[str] = None
    if person_about.exists():
        try:
            with person_about.open() as f:
                data = yaml.safe_load(f) or {}
            handles = [str(h) for h in (data.get("handles") or []) if h]
            display_name = data.get("name") or None
        except yaml.YAMLError:
            pass

    # Source 2: slug IS the handle (raw phone digits or an email).
    if not handles:
        if _PHONE_SLUG.match(slug):
            handles = [f"+{slug}"]
        elif "@" in slug:
            handles = [slug.lower()]

    if not handles:
        print(
            f"[imessage-out] new thread {slug}/ has no matching people entry "
            f"and no handle-like slug; leaving meta.yaml empty",
            flush=True,
        )
        return False

    meta: dict = {
        "description": "",
        "created": datetime.now().date().isoformat(),
        "group": False,
        "handles": handles,
        "channel": "imessage",
    }
    if display_name:
        meta["display_name"] = display_name
    with meta_path.open("w") as f:
        yaml.safe_dump(meta, f, sort_keys=False)

    # Link person into thread if present, matching messages._create_person.
    person_dir = PEOPLE_ROOT / slug
    link = thread_dir / slug
    if person_dir.is_dir() and not link.exists():
        link.symlink_to(Path("..") / ".." / ".." / "memory" / "people" / slug)

    print(f"[imessage-out] materialized meta.yaml for {slug}/ (handles={handles})", flush=True)
    return True


def build() -> Tailer:
    tailer: Tailer

    async def _on_dir_created(path: Path) -> None:
        # Only top-level thread dirs: messages/{slug}/. Ignore deeper paths.
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
        # Drain any .md files already sitting in the new dir — PAI may have
        # done `mkdir && echo` back-to-back, and the file-event could have
        # raced the dir-event to a not-yet-owned skip.
        for child in path.iterdir():
            if child.is_file() and child.suffix == ".md":
                await tailer._drain_file(child)  # noqa: SLF001

    async def on_line(path: Path, line: str) -> None:
        if BRACKET_LINE.match(line):
            return  # log entry — inbound, canonical me:, kernel note, etc.
        text = line.rstrip()
        if not text:
            return
        # Bare line = outbound draft. Arm the echo registration *before*
        # the send: kqueue on chat.db-wal fires faster than osascript
        # returns to Python, so imessage-in can emit the from_me=True
        # row before we'd otherwise have registered. Disarm on failure
        # so a real owner-from-phone send isn't swallowed later.
        slug = path.parent.name
        outbound_echo.register(slug, text)
        ok = await _process_send(path, text)
        if not ok:
            outbound_echo.consume(slug, text)
            return
        canonical = _append_canonical(path, text)
        tailer.suppress_next(path, canonical)

    tailer = Tailer(
        name="imessage-out",
        roots=[MESSAGES_ROOT],
        owned=_owned,
        on_line=on_line,
        on_dir_created=_on_dir_created,
    )
    return tailer


async def run() -> None:
    await build().run()
