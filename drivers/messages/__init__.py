"""Incoming message routing — handle → thread slug, append, auto-create.

The kernel calls `ingest()` when a `new_message` event arrives. This module
owns the filesystem shape of `communication/messages/` and `memory/people/`
for that ingest path. It never touches PAI — the caller does the nudge.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from boot import paths
from drivers import contacts

# v3: messages and people live in canonical shared state, not per-PAI
# home views. The home stitching surfaces them via symlinks.
MESSAGES_DIR = paths.var_spool_messages()
PEOPLE_DIR = paths.var_lib_memory() / "people"

FILLER_WORDS = {
    "a", "an", "the", "and", "or", "but", "for", "from", "about",
    "with", "into", "over", "after", "before", "during", "between",
    "of", "on", "in", "to", "at", "by", "as", "is", "are", "was",
    "their", "his", "her", "its",
}


@dataclass
class IngestResult:
    slug: str
    created_thread: bool
    created_person: bool
    day_file: Path
    sender: str


def slugify(name: str, *, max_words: int = 0) -> str:
    s = name.strip().lower()
    s = re.sub(r"[’']s\b", "s", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if max_words > 0:
        tokens = [t for t in s.split("-") if t not in FILLER_WORDS]
        s = "-".join(tokens[:max_words])
    return s or "unknown"


def _normalize_handle(handle: str) -> str:
    """Normalize phone/email handles so lookups are stable.

    Emails lowercased. Phone numbers stripped of spaces, dashes, parens.
    """
    h = handle.strip()
    if "@" in h:
        return h.lower()
    return re.sub(r"[\s\-\(\)]+", "", h)


def _slug_from_handle(handle: str) -> str:
    """Fallback slug when no display name is available."""
    h = _normalize_handle(handle)
    if "@" in h:
        return slugify(h.split("@", 1)[0]) or "unknown"
    digits = re.sub(r"\D", "", h)
    return digits or "unknown"


def resolve_slug(
    handle: str,
    chat_guid: Optional[str] = None,
) -> Optional[str]:
    """Find an existing thread slug for a handle or chat_guid. None if new."""
    if not MESSAGES_DIR.exists():
        return None
    norm_handle = _normalize_handle(handle) if handle else None

    for thread_dir in MESSAGES_DIR.iterdir():
        if not thread_dir.is_dir() or thread_dir.name.startswith("."):
            continue
        meta_path = thread_dir / "meta.yaml"
        if not meta_path.exists():
            continue
        try:
            with meta_path.open() as f:
                meta = yaml.safe_load(f) or {}
        except yaml.YAMLError:
            continue

        if chat_guid and meta.get("chat_guid") == chat_guid:
            return thread_dir.name

        handles = meta.get("handles") or []
        if norm_handle and norm_handle in (_normalize_handle(h) for h in handles):
            return thread_dir.name

    return None


def _person_has_handle(slug: str, handle: str) -> bool:
    """True if people/{slug}/about.yaml already lists this handle."""
    about = PEOPLE_DIR / slug / "about.yaml"
    if not about.exists():
        return False
    try:
        with about.open() as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError:
        return False
    norm = _normalize_handle(handle)
    return any(_normalize_handle(h) == norm for h in (data.get("handles") or []))


def _unique_slug(base: str, handle: str = "") -> str:
    """Suffix -2, -3, ... if base collides with an unrelated thread/person.

    A person dir whose about.yaml already lists `handle` is the same human,
    not a collision — reuse the slug so threads and people stay aligned.
    """
    candidate = base
    n = 2
    while True:
        msg_taken = (MESSAGES_DIR / candidate).exists()
        person_taken = (PEOPLE_DIR / candidate).exists()
        if person_taken and handle and _person_has_handle(candidate, handle):
            person_taken = False
        if not msg_taken and not person_taken:
            return candidate
        candidate = f"{base}-{n}"
        n += 1


def _group_slug(chat_guid: str) -> str:
    return "group-" + hashlib.sha1(chat_guid.encode("utf-8")).hexdigest()[:8]


def _person_slug_for_handle(handle: str) -> Optional[str]:
    """Find an existing person slug whose about.yaml lists this handle."""
    if not PEOPLE_DIR.exists() or not handle:
        return None
    norm = _normalize_handle(handle)
    for person_dir in PEOPLE_DIR.iterdir():
        if not person_dir.is_dir() or person_dir.name.startswith("."):
            continue
        about = person_dir / "about.yaml"
        if not about.exists():
            continue
        try:
            with about.open() as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError:
            continue
        if any(_normalize_handle(h) == norm for h in (data.get("handles") or [])):
            return person_dir.name
    return None


def _resolve_sender(handle: str) -> str:
    """Pick a per-line sender label for a group message.

    Person-slug match → Contacts first name → last-4-digits like `…4567`
    (or local-part for emails) → raw normalized handle.
    """
    if not handle:
        return "unknown"
    person = _person_slug_for_handle(handle)
    if person:
        return _sender_name(person, None)
    name = contacts.resolve(handle)
    if name:
        return name.split()[0].lower()
    norm = _normalize_handle(handle)
    if "@" in norm:
        return norm.split("@", 1)[0]
    digits = re.sub(r"\D", "", norm)
    if len(digits) >= 4:
        return f"…{digits[-4:]}"
    return norm or "unknown"


def _create_thread(
    slug: str,
    handle: str,
    display_name: Optional[str],
    chat_guid: Optional[str],
    source: Optional[str] = None,
    chat_handles: Optional[list[str]] = None,
) -> None:
    thread_dir = MESSAGES_DIR / slug
    thread_dir.mkdir(parents=True, exist_ok=True)

    if chat_guid:
        # Group thread: meta.handles is the participant roster, not a single
        # peer. Skip display_name (we use a structural slug for groups).
        roster = [_normalize_handle(h) for h in (chat_handles or []) if h]
        # Make sure the first sender is in the roster even if chat_handle_join
        # lagged the message write.
        if handle and _normalize_handle(handle) not in roster:
            roster.append(_normalize_handle(handle))
        meta: dict = {
            "description": "",
            "created": datetime.now().date().isoformat(),
            "group": True,
            "handles": roster,
            "chat_guid": chat_guid,
        }
    else:
        meta = {
            "description": "",
            "created": datetime.now().date().isoformat(),
            "group": False,
            "handles": [_normalize_handle(handle)] if handle else [],
        }
        if display_name:
            meta["display_name"] = display_name
    # `channel` tells outbound drivers which transport to use. Today only
    # imessage — set it when the source event came from imessage so auto-
    # created threads can reply out without manual meta.yaml edits.
    if source == "imessage":
        meta["channel"] = "imessage"
    with (thread_dir / "meta.yaml").open("w") as f:
        yaml.safe_dump(meta, f, sort_keys=False)


def _create_person(slug: str, handle: str, display_name: Optional[str]) -> None:
    person_dir = PEOPLE_DIR / slug
    person_dir.mkdir(parents=True, exist_ok=True)
    about_path = person_dir / "about.yaml"
    if about_path.exists():
        return
    about = {
        "name": display_name or slug,
        "handles": [_normalize_handle(handle)] if handle else [],
        "relationship": "",
        "entry": "",
    }
    with about_path.open("w") as f:
        yaml.safe_dump(about, f, sort_keys=False)

    # Symlink the person into the thread folder (scaffolding convention).
    thread_dir = MESSAGES_DIR / slug
    link = thread_dir / slug
    if not link.exists() and thread_dir.exists():
        target = Path("..") / ".." / ".." / ".." / "lib" / "memory" / "people" / slug
        link.symlink_to(target)


def _sender_name(slug: str, display_name: Optional[str]) -> str:
    """First name lowercased, for the `[HH:MM] sender:` prefix."""
    about = PEOPLE_DIR / slug / "about.yaml"
    if about.exists():
        try:
            with about.open() as f:
                data = yaml.safe_load(f) or {}
            name = data.get("name")
            if name:
                return name.split()[0].lower()
        except yaml.YAMLError:
            pass
    if display_name:
        return display_name.split()[0].lower()
    return slug.split("-")[0]


def _append_day_file(slug: str, sender: str, text: str, at: datetime) -> Path:
    thread_dir = MESSAGES_DIR / slug
    day_file = thread_dir / f"{at.date().isoformat()}.md"
    # Collapse embedded newlines to a single visible marker. The imessage
    # outbound tailer treats any bare (non-bracket-prefixed) line as a send
    # draft, so a multi-line inbound body would otherwise get parroted back
    # to the sender line-by-line.
    flat = text.replace("\r\n", "\n").replace("\n", " ↵ ").rstrip()
    line = f"[{at.strftime('%H:%M')}] {sender}: {flat}\n"
    with day_file.open("a") as f:
        f.write(line)
    return day_file


def ingest(
    handle: str,
    text: str,
    chat_guid: Optional[str] = None,
    display_name: Optional[str] = None,
    received_at: Optional[datetime] = None,
    source: Optional[str] = None,
    sender_override: Optional[str] = None,
    chat_handles: Optional[list[str]] = None,
) -> IngestResult:
    """Place an incoming message in the right thread; create thread + person if new."""
    MESSAGES_DIR.mkdir(parents=True, exist_ok=True)
    PEOPLE_DIR.mkdir(parents=True, exist_ok=True)

    at = received_at or datetime.now()

    slug = resolve_slug(handle, chat_guid)
    created_thread = False
    created_person = False

    if slug is None:
        if chat_guid:
            # Group thread: structural slug from chat_guid, never a person stub.
            slug = _group_slug(chat_guid)
            if (MESSAGES_DIR / slug).exists():
                # Same chat_guid was missed by resolve_slug (meta.yaml unreadable
                # or missing the field). Disambiguate rather than collide.
                slug = _unique_slug(slug, handle=handle)
        else:
            # For 1:1 threads, try macOS Contacts to fill display_name when the
            # event didn't carry one.
            if not display_name and handle:
                display_name = contacts.resolve(handle)
            base = slugify(display_name, max_words=2) if display_name else _slug_from_handle(handle)
            slug = _unique_slug(base or "unknown", handle=handle)
        _create_thread(
            slug, handle, display_name, chat_guid,
            source=source, chat_handles=chat_handles,
        )
        created_thread = True
        if not chat_guid:  # only create person stub for 1:1 threads
            _create_person(slug, handle, display_name)
            created_person = True

    if sender_override:
        sender = sender_override
    elif chat_guid:
        sender = _resolve_sender(handle)
    else:
        sender = _sender_name(slug, display_name)
    day_file = _append_day_file(slug, sender, text, at)

    return IngestResult(
        slug=slug,
        created_thread=created_thread,
        created_person=created_person,
        day_file=day_file,
        sender=sender,
    )
