"""macOS Contacts lookup — handle → display name.

Uses CNContactStore (PyObjC) to pull the address book once on first access
and cache it for the process lifetime. Restart the kernel to pick up new
Contacts entries.

Requires Contacts access: System Settings → Privacy & Security → Contacts
→ add the process running the kernel. First fetch prompts the OS dialog.

If pyobjc-framework-Contacts isn't installed or the fetch fails, falls
back to an empty cache — callers get None and keep going.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

_lock = threading.Lock()
_cache: Optional[dict[str, str]] = None


def _normalize_phone(raw: str) -> str:
    """Strip to digits, keep last 10 (drops country code)."""
    digits = re.sub(r"\D", "", raw)
    return digits[-10:] if len(digits) >= 10 else digits


def _load() -> dict[str, str]:
    try:
        import Contacts  # type: ignore[import-not-found]
    except ImportError:
        print("[contacts] pyobjc-framework-Contacts not installed; skipping", flush=True)
        return {}

    store = Contacts.CNContactStore.alloc().init()
    keys = [
        Contacts.CNContactGivenNameKey,
        Contacts.CNContactFamilyNameKey,
        Contacts.CNContactPhoneNumbersKey,
        Contacts.CNContactEmailAddressesKey,
    ]
    request = Contacts.CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)
    lookup: dict[str, str] = {}
    results: list = []

    def _handler(contact, stop):
        results.append(contact)

    success, error = store.enumerateContactsWithFetchRequest_error_usingBlock_(
        request, None, _handler
    )
    if not success:
        print(f"[contacts] CNContactStore fetch failed: {error}", flush=True)
        return {}

    for c in results:
        given = c.givenName() or ""
        family = c.familyName() or ""
        full = f"{given} {family}".strip()
        if not full:
            continue
        for phone in c.phoneNumbers():
            norm = _normalize_phone(phone.value().stringValue())
            if norm:
                lookup[norm] = full
        for email in c.emailAddresses():
            addr = email.value()
            if addr:
                lookup[str(addr).lower()] = full

    print(f"[contacts] loaded {len(lookup)} entries from macOS Contacts", flush=True)
    return lookup


def _ensure_loaded() -> dict[str, str]:
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load()
        return _cache


def resolve(handle: str) -> Optional[str]:
    """Return 'First Last' for a handle, or None if unknown."""
    if not handle:
        return None
    lookup = _ensure_loaded()
    if not lookup:
        return None
    h = handle.strip()
    if "@" in h:
        return lookup.get(h.lower())
    norm = _normalize_phone(h)
    if not norm:
        return None
    return lookup.get(norm)


def refresh() -> int:
    """Force-reload the cache. Returns entry count."""
    global _cache
    with _lock:
        _cache = _load()
        return len(_cache)


def _normalize_phone_handle(raw: str) -> str:
    """Match the handle format chat.db uses (digits + leading +)."""
    return re.sub(r"[\s\-\(\)]+", "", raw.strip())


def _has_phone(handles: list[str]) -> bool:
    return any("@" not in h for h in handles)


def sync(people_dir: Path, messages_dir: Path) -> tuple[int, int, int]:
    """For each macOS contact, create people/{slug}/about.yaml and (if the
    contact has a phone) messages/{slug}/meta.yaml as an iMessage thread.

    First-write-wins (`mkdir` semantics): existing entries are left
    untouched. Person stub is created for any contact with at least one
    handle. Thread is only created for contacts with at least one phone
    number — iMessage routing needs one to deliver outbound messages.

    Returns (people_created, threads_created, skipped).
    """
    try:
        import Contacts  # type: ignore[import-not-found]
    except ImportError:
        return 0, 0, 0

    # Lazy import to avoid a circular dep with kernel.messages.
    from drivers.messages import slugify

    store = Contacts.CNContactStore.alloc().init()
    keys = [
        Contacts.CNContactGivenNameKey,
        Contacts.CNContactFamilyNameKey,
        Contacts.CNContactPhoneNumbersKey,
        Contacts.CNContactEmailAddressesKey,
    ]
    request = Contacts.CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)
    results: list = []

    def _handler(contact, stop):
        results.append(contact)

    success, error = store.enumerateContactsWithFetchRequest_error_usingBlock_(
        request, None, _handler
    )
    if not success:
        print(f"[contacts] sync fetch failed: {error}", flush=True)
        return 0, 0, 0

    people_dir.mkdir(parents=True, exist_ok=True)
    messages_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().date().isoformat()
    people_created = 0
    threads_created = 0
    skipped = 0
    for c in results:
        given = c.givenName() or ""
        family = c.familyName() or ""
        full = f"{given} {family}".strip()
        if not full:
            continue
        handles: list[str] = []
        for phone in c.phoneNumbers():
            norm = _normalize_phone_handle(phone.value().stringValue())
            if norm and norm not in handles:
                handles.append(norm)
        for email in c.emailAddresses():
            addr = email.value()
            if addr:
                low = str(addr).lower()
                if low not in handles:
                    handles.append(low)
        if not handles:
            continue

        slug = slugify(full)
        person_dir = people_dir / slug
        about = person_dir / "about.yaml"
        if about.exists():
            skipped += 1
        else:
            person_dir.mkdir(parents=True, exist_ok=True)
            with about.open("w") as f:
                yaml.safe_dump(
                    {"name": full, "handles": handles, "relationship": "", "entry": ""},
                    f,
                    sort_keys=False,
                )
            people_created += 1

        if not _has_phone(handles):
            continue
        thread_dir = messages_dir / slug
        meta = thread_dir / "meta.yaml"
        if meta.exists():
            continue
        thread_dir.mkdir(parents=True, exist_ok=True)
        with meta.open("w") as f:
            yaml.safe_dump(
                {
                    "description": "",
                    "created": today,
                    "group": False,
                    "handles": handles,
                    "display_name": full,
                    "channel": "imessage",
                },
                f,
                sort_keys=False,
            )
        # Symlink person into thread dir. Path is relative to the link's
        # parent (var/spool/communication/messages/<slug>/), four levels up
        # to var/, then into lib/memory/people/<slug>.
        link = thread_dir / slug
        if not link.exists():
            link.symlink_to(Path("..") / ".." / ".." / ".." / "lib" / "memory" / "people" / slug)
        threads_created += 1

    print(
        f"[contacts] sync → {people_created} people, {threads_created} threads created; "
        f"{skipped} existing people left alone",
        flush=True,
    )
    return people_created, threads_created, skipped
