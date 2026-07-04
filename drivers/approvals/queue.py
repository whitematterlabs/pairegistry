"""Shared approval-queue I/O — leaf module, no driver-side imports.

Record creation for an outbound driver (email/imessage) that just detected a
send is blocked under a capability in `ask` mode, plus the atomic yaml
helpers `drivers/approvals/driver.py` reuses for its own read/write. Import
direction is one-way: outbound drivers and the approvals driver both import
this module; it never imports either of them, so there's no cycle even
though the approvals driver separately imports drivers.imessage.outbound for
delivery helpers.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import yaml

from boot import paths


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _slug(value: str) -> str:
    s = _NON_ALNUM.sub("-", (value or "").lower()).strip("-")
    return s[:60] or "send"


def atomic_dump(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)


def load(path: Path) -> Optional[dict]:
    try:
        with path.open() as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _unique_path(d: Path, ident: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{ident}.yaml"
    n = 1
    while path.exists():
        path = d / f"{ident}-{n}.yaml"
        n += 1
    return path


def stage_pending(
    channel: str,
    action: dict[str, Any],
    created_by: Optional[str] = None,
    source_ref: Optional[str] = None,
) -> Path:
    """Write a `pending` approval record for a send an outbound driver just
    blocked because the capability is in `ask` mode. Never called by a PAI
    directly — only by `email`/`imessage`/`whatsapp` outbound drivers, on the
    PAI's behalf, when it attempted a normal send. The owner approves/rejects
    it in the web console; the approvals driver then delivers or reports back.

    `source_ref` (email only) is the PAI_ROOT-relative path of the original
    draft yaml, so a rejection can be written back onto it (`draft_state:
    failed`) and surfaced via the normal `email-out:draft_failed` event —
    iMessage has no equivalent originating artifact to update."""
    if channel == "email":
        to = action.get("to") or []
        label = action.get("subject") or (to[0] if to else "") or "email"
    elif channel == "imessage":
        label = action.get("thread") or "imessage"
    elif channel == "whatsapp":
        label = action.get("thread") or "whatsapp"
    else:
        label = channel
    ident = time.strftime("%Y%m%d-%H%M%S") + "-" + _slug(str(label))
    path = _unique_path(paths.var_spool_approvals(), ident)
    rec: dict[str, Any] = {
        "id": path.stem,
        "channel": channel,
        "status": "pending",
        "created_by": created_by or "unknown",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "action": action,
        "decided_at": None,
        "decided_by": None,
        "error": None,
    }
    if source_ref:
        rec["source_ref"] = source_ref
    atomic_dump(path, rec)
    return path
