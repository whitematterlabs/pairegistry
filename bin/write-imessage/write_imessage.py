#!/usr/bin/env python
"""write-imessage - send an iMessage through the imessage driver.

Wraps the day-file protocol so callers never hand-write it: builds the
thread dir + meta.yaml if needed, flattens a multi-line body into the
single ` ↵ `-marked day-file line (one invocation = exactly one
message), appends it as a bare send-request line, and waits for the
driver's verdict.

Delivery is subject to the owner's `capabilities.imessage_send` grant:

  yes  — the driver sends via Messages.app and writes the canonical
         `[HH:MM] me: ...` record (reported here as `sent`).
  ask  — the driver queues the message in the owner's approval tray
         (reported as `pending_approval` — tell the owner you sent it
         for approval, not that it was delivered).
  no   — the driver consumes the line without sending (`send_blocked`).

--to accepts an existing thread slug, a memory/people slug (handles
come from about.yaml), a raw phone number, or an email address. New
group threads can't be created here (they need a chat_guid from an
inbound message); existing group thread slugs work like any other.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

from boot import paths


NEWLINE_MARK = "↵"

# Driver-side outcome notes (drivers/imessage/outbound.py). The driver
# appends exactly one of these — or the canonical `me:` record — after
# consuming a bare line.
_CANONICAL = re.compile(r"^\[\d\d:\d\d\] me: (?P<text>.*)$")
_KERNEL_NOTE = re.compile(r"^\[\d\d:\d\d\] kernel: (?P<note>.*)$")

_PHONE_CHARS = re.compile(r"[\s\-().]")
_PHONE_SLUG = re.compile(r"^\d{7,}$")


def _messages_root() -> Path:
    return paths.var_spool_messages()


def _people_root() -> Path:
    return paths.var_lib_memory() / "people"


def _resolve_slug(to: str) -> str:
    """Normalize --to into a thread slug: email → lowercased address,
    phone-like → bare digits, anything else → lowercased people/thread slug."""
    raw = to.strip()
    if not raw:
        raise SystemExit("write-imessage: --to cannot be empty")
    if "@" in raw:
        return raw.lower()
    digits = _PHONE_CHARS.sub("", raw).lstrip("+")
    if _PHONE_SLUG.match(digits):
        return digits
    return raw.lower().replace(" ", "-")


def _load_meta(thread_dir: Path) -> Optional[dict]:
    meta_path = thread_dir / "meta.yaml"
    if not meta_path.exists():
        return None
    try:
        with meta_path.open() as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else {}


def _ensure_thread(slug: str) -> Path:
    """Return the thread dir for `slug`, creating dir + meta.yaml if needed.

    Meta materialization mirrors drivers/imessage/outbound._materialize_meta
    (the driver's version covers `mkdir && echo` done by hand; writing meta
    here before the day-file makes the append owned immediately, no
    dir-event race). Sources: memory/people/<slug>/about.yaml handles,
    else the slug itself when it is phone digits or an email."""
    thread_dir = _messages_root() / slug
    meta = _load_meta(thread_dir)
    if meta is not None:
        channel = meta.get("channel")
        if channel != "imessage":
            raise SystemExit(
                f"write-imessage: thread {slug!r} is channel {channel!r}, "
                "not imessage — use that channel's send path"
            )
        return thread_dir

    handles: list[str] = []
    display_name: Optional[str] = None
    person_about = _people_root() / slug / "about.yaml"
    if person_about.exists():
        try:
            with person_about.open() as f:
                data = yaml.safe_load(f) or {}
            handles = [str(h) for h in (data.get("handles") or []) if h]
            display_name = data.get("name") or None
        except yaml.YAMLError:
            pass
    if not handles:
        if _PHONE_SLUG.match(slug):
            handles = [f"+{slug}"]
        elif "@" in slug:
            handles = [slug]
    if not handles:
        raise SystemExit(
            f"write-imessage: no thread {slug!r} and no way to reach them — "
            f"need handles in memory/people/{slug}/about.yaml (use `addcontact`), "
            "or pass a raw phone number / email as --to"
        )

    thread_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "description": "",
        "created": datetime.now().date().isoformat(),
        "group": False,
        "handles": handles,
        "channel": "imessage",
    }
    if display_name:
        meta["display_name"] = display_name
    with (thread_dir / "meta.yaml").open("w") as f:
        yaml.safe_dump(meta, f, sort_keys=False)

    # Link the person into the thread, matching messages._create_person.
    # Relative target resolves through the /home/<pai>/ view.
    person_dir = _people_root() / slug
    link = thread_dir / slug
    if person_dir.is_dir() and not link.exists():
        link.symlink_to(Path("..") / ".." / ".." / "memory" / "people" / slug)
    return thread_dir


def _read_body(args: argparse.Namespace) -> str:
    if args.body is not None and args.body_file is not None:
        raise SystemExit("write-imessage: use only one of --body or --body-file")
    if args.body is not None:
        body = args.body
    elif args.body_file is not None:
        if args.body_file == "-":
            body = sys.stdin.read()
        else:
            try:
                body = Path(args.body_file).read_text()
            except OSError as e:
                raise SystemExit(f"write-imessage: cannot read body file: {e}") from e
    elif not sys.stdin.isatty():
        body = sys.stdin.read()
    else:
        raise SystemExit(
            "write-imessage: provide message text with --body, --body-file, or stdin"
        )
    body = body.replace("\r\n", "\n").strip()
    if not body:
        raise SystemExit("write-imessage: message is empty")
    return body


def _day_line(body: str) -> str:
    """Flatten to the single-line day-file form: real newlines become the
    same ` ↵ ` marker inbound uses, so one line stays one message and the
    driver expands it back at send time."""
    line = body.replace("\n", f" {NEWLINE_MARK} ")
    if line.startswith("["):
        # `[` opens the log-entry prefix; the driver would skip this line
        # as a log record and the message would silently never send.
        raise SystemExit(
            "write-imessage: message cannot start with '[' — the driver "
            "would read it as a day-file log entry; rephrase the opening"
        )
    return line


def _classify(new_text: str, day_line: str) -> Optional[dict[str, Any]]:
    """Scan driver-appended lines for this send's verdict."""
    for line in new_text.splitlines():
        m = _CANONICAL.match(line)
        if m and m.group("text") == day_line:
            return {"state": "sent"}
        m = _KERNEL_NOTE.match(line)
        if not m:
            continue  # inbound traffic landing concurrently
        note = m.group("note")
        if note.startswith("queued for owner approval"):
            return {"state": "pending_approval"}
        if note.startswith("send frozen"):
            return {"state": "send_blocked", "detail": note}
        if note.startswith("send failed"):
            return {"state": "failed", "detail": note}
    return None


def _wait_for_verdict(day_file: Path, offset: int, day_line: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with day_file.open("r", encoding="utf-8") as f:
                f.seek(offset)
                verdict = _classify(f.read(), day_line)
        except OSError:
            verdict = None
        if verdict:
            return verdict
        time.sleep(0.2)
    return {
        "state": "pending",
        "detail": f"no driver verdict within {timeout:g}s — check the day-file",
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="write-imessage",
        description=(
            "Send one iMessage. Multi-line bodies stay one message. Delivery "
            "follows the owner's imessage_send capability: sent outright under "
            "`yes`, queued for owner approval under `ask`, blocked under `no` "
            "— the printed `state` says which happened."
        ),
    )
    parser.add_argument(
        "--to",
        required=True,
        help="thread slug, memory/people slug, phone number, or email",
    )
    parser.add_argument("--body", "--content", dest="body", help="message text")
    parser.add_argument(
        "--body-file",
        "--content-file",
        dest="body_file",
        help="read message text from a file, or '-' for stdin",
    )
    parser.add_argument(
        "--wait",
        default=15.0,
        type=float,
        metavar="SECONDS",
        help="wait for the driver's verdict (default 15; 0 = fire and forget)",
    )
    args = parser.parse_args(argv)
    if args.wait < 0:
        parser.error("--wait must be >= 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    slug = _resolve_slug(args.to)
    day_line = _day_line(_read_body(args))

    thread_dir = _ensure_thread(slug)
    day_file = thread_dir / f"{datetime.now().date().isoformat()}.md"
    with day_file.open("a", encoding="utf-8") as f:
        f.write(day_line + "\n")
        f.flush()
        offset = f.tell()

    result: dict[str, Any] = {
        "thread": slug,
        "day_file": f"communication/messages/{slug}/{day_file.name}",
    }
    if args.wait:
        result.update(_wait_for_verdict(day_file, offset, day_line, args.wait))
    else:
        result["state"] = "pending"
    print(yaml.safe_dump(result, sort_keys=False, allow_unicode=True), end="")
    return 2 if result["state"] in ("failed", "send_blocked") else 0


if __name__ == "__main__":
    raise SystemExit(main())
