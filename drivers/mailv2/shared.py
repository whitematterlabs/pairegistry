"""Shared on-disk helpers for the mailv2 email driver.

mailv2 partitions messages into a nested `YYYY/MM/DD` tree so date-scoped
`rg`/glob queries are trivial (`communication/email/*/2026/06/25/`). This
is the deliberate divergence from the flat `email/shared.py` (which keeps
`{YYYY-MM-DD}/` single-level dirs); the two trees are NOT interchangeable.

Shape:
    home/communication/email/{account}/
        {YYYY}/{MM}/{DD}/{subject-slug}.yaml         # canonical
        {YYYY}/{MM}/{DD}/{subject-slug}.prev -> ...  # one-hop walkback
        threads/{thread-slug}/...yaml -> ...         # chronological index

Pure functions, provider-agnostic.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional

import yaml

# Coerce any str subclass (e.g. email.headerregistry header objects returned
# by stdlib's modern email policy) to plain str so SafeDumper can represent
# it. Without this, headers like Subject crash represent_undefined.
def _str_subclass_repr(dumper, data):
    return dumper.represent_str(str(data))
yaml.SafeDumper.add_multi_representer(str, _str_subclass_repr)

_RE_PREFIX = re.compile(r"^\s*(re|fw|fwd|aw)\s*[:\-]\s*", re.IGNORECASE)
_RE_NONALNUM = re.compile(r"[^a-z0-9]+")
_EMAIL_SPOOL_PREFIX = "var/spool/communication/email"


def home_view_path(rel_path: str) -> str:
    """Return the PAI-home spelling for an email spool path.

    Drivers write canonical files under `var/spool/communication/email`, but
    event consumers run with CWD at their stitched home. A relative
    `var/spool/...` path is therefore misleading; `communication/email/...`
    is the readable home view documented in the email skill.
    """
    rel = rel_path.strip("/")
    if rel == _EMAIL_SPOOL_PREFIX:
        return "communication/email"
    prefix = _EMAIL_SPOOL_PREFIX + "/"
    if rel.startswith(prefix):
        return "communication/email/" + rel[len(prefix):]
    return rel_path


def _strip_subject_prefixes(subject: str) -> str:
    s = subject or ""
    while True:
        new = _RE_PREFIX.sub("", s, count=1)
        if new == s:
            return s
        s = new


def normalize_subject(subject: str) -> str:
    s = _strip_subject_prefixes(subject).lower()
    s = _RE_NONALNUM.sub("-", s).strip("-")
    return s or "no-subject"


def subject_slug(subject: str) -> str:
    """Filesystem-safe slug derived from subject. Cap at 80 chars."""
    s = normalize_subject(subject)
    return s[:80]


def thread_slug(subject: str, references: Optional[Iterable[str]], message_id: str) -> str:
    """Stable per-thread slug. Hash of the root Message-ID gives uniqueness
    across threads that share a normalized subject."""
    refs = list(references or [])
    root = refs[0] if refs else message_id
    h = hashlib.sha1(root.encode("utf-8")).hexdigest()[:8]
    return f"{normalize_subject(subject)}-{h}"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        f.write(text)
    os.replace(tmp, path)


def write_message_yaml(account_dir: Path, msg: dict, dedup: bool = True) -> tuple[Path, bool]:
    """Write per-message yaml under {account_dir}/{YYYY}/{MM}/{DD}/{subject-slug}.yaml.

    Returns (path, created). `created=False` when an existing yaml with the
    same Message-ID was found — caller can use that to skip duplicate event
    emission when the cursor is parked behind a `.partial.emlx` row and a
    later, already-ingested row is being re-scanned.

    `dedup=False` skips the by-Message-ID `find_message_by_id` scan. The
    backfill passes this after a caller-side in-memory dedup so the bulk
    rebuild stays O(n) instead of O(n²) (one full-tree scan per insert).

    Uses `received_at` for inbound, falls back to `sent_at` for outbound.
    Appends `-{HH-MM}` on same-day slug collision.
    """
    ts = msg.get("received_at") or msg.get("sent_at")
    if not ts:
        raise ValueError("message must have received_at or sent_at")
    dt = ts if isinstance(ts, datetime) else datetime.fromisoformat(ts)

    message_id = (msg.get("message_id") or "").strip()
    if dedup and message_id:
        existing = find_message_by_id(account_dir, message_id)
        if existing is not None:
            return existing, False

    date_dir = account_dir / f"{dt:%Y}" / f"{dt:%m}" / f"{dt:%d}"
    slug = subject_slug(msg.get("subject", ""))

    path = date_dir / f"{slug}.yaml"
    if path.exists():
        path = date_dir / f"{slug}-{dt.strftime('%H-%M')}.yaml"
        if path.exists():
            path = date_dir / f"{slug}-{dt.strftime('%H-%M-%S')}.yaml"

    body = yaml.safe_dump(msg, sort_keys=False, allow_unicode=True)
    _atomic_write(path, body)
    return path, True


def link_thread(account_dir: Path, msg_path: Path, t_slug: str, received_at: datetime) -> Path:
    """Create threads/{t_slug}/{YYYY-MM-DD}T{HH-MM}-{subject-slug}.yaml -> msg_path."""
    threads_dir = account_dir / "threads" / t_slug
    threads_dir.mkdir(parents=True, exist_ok=True)
    stem = msg_path.stem
    name = f"{received_at.strftime('%Y-%m-%dT%H-%M')}-{stem}.yaml"
    link = threads_dir / name
    if link.is_symlink() or link.exists():
        return link
    target = os.path.relpath(msg_path, start=threads_dir)
    os.symlink(target, link)
    return link


def link_prev(msg_path: Path, parent_msg_path: Optional[Path]) -> Optional[Path]:
    """Best-effort `.prev` symlink next to msg_path. No-op when parent unknown."""
    if parent_msg_path is None:
        return None
    link = msg_path.with_suffix(".prev")
    if link.is_symlink() or link.exists():
        return link
    target = os.path.relpath(parent_msg_path, start=msg_path.parent)
    os.symlink(target, link)
    return link


def find_message_by_id(account_dir: Path, message_id: str) -> Optional[Path]:
    """Linear scan of the nested {YYYY}/{MM}/{DD} tree for a yaml containing
    this Message-ID. Walks newest-first (replies usually point to recent
    parents)."""
    if not message_id or not account_dir.exists():
        return None
    needle = f"message_id: {message_id}"
    needle_quoted = f"message_id: '{message_id}'"
    needle_dquoted = f'message_id: "{message_id}"'
    # The glob path encodes YYYY/MM/DD, so reverse-sorting the matched paths
    # orders them newest date first. The digit character classes exclude the
    # `threads/` index and `meta.yaml` from the walk.
    yamls = sorted(
        account_dir.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/*.yaml"),
        reverse=True,
    )
    for yml in yamls:
        try:
            head = yml.read_text(errors="replace")
        except OSError:
            continue
        if needle in head or needle_quoted in head or needle_dquoted in head:
            return yml
    return None


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "head"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        # Collapse runs of blank lines and trim trailing whitespace per line.
        lines = [ln.rstrip() for ln in raw.splitlines()]
        out: list[str] = []
        blank = 0
        for ln in lines:
            if ln.strip():
                out.append(ln)
                blank = 0
            else:
                blank += 1
                if blank <= 1:
                    out.append("")
        return "\n".join(out).strip() + "\n"


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html or "")
    p.close()
    return p.text()
