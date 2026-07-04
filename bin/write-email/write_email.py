#!/usr/bin/env python
"""write-email - draft or send a Mail.app message through the email driver spool.

Writes one YAML file under var/spool/communication/email/drafts/; the
macmail-out driver notices it and acts on the explicit mode you chose:

  --draft   save it into Mail.app's Drafts folder for the owner to review.
  --send    set `action: send` so the driver delivers it — subject to the
            owner's `capabilities.email_send` grant. Under `ask` the send is
            staged into the owner's approvals queue (draft_state:
            pending_approval) and surfaced in the web console; under `no` it
            falls back to a saved draft with `send_blocked` set.

Exactly one of --send / --draft is required. There is no default: "draft an
email" and "send an email" are different owner intents, so the caller must
say which. This prevents silently drafting when the owner asked to send.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from boot import paths


_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Terminal draft_state values macmail-out writes. `pending_approval` is
# terminal from this command's view: under `ask` mode a send hands off to the
# approvals queue and never flips back here (an approved item returns as a
# fresh draft written by the approvals driver).
_TERMINAL_STATES = {"drafted", "sent", "failed", "pending_approval"}


def _split_values(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in values or []:
        for part in raw.split(","):
            item = part.strip()
            if item:
                out.append(item)
    return out


def _slug(value: str) -> str:
    slug = _NON_ALNUM.sub("-", value.lower()).strip("-")
    return slug[:80] or "draft"


def _draft_name(args: argparse.Namespace, to_addrs: list[str]) -> str:
    if args.name:
        raw = args.name.strip()
        if not raw:
            raise SystemExit("write-email: --name cannot be empty")
        if raw != Path(raw).name:
            raise SystemExit("write-email: --name must be a filename, not a path")
        return raw if raw.endswith(".yaml") else f"{raw}.yaml"

    subject_part = _slug(args.subject or "reply")
    if to_addrs:
        recipient_part = _slug(to_addrs[0].split("@", 1)[0])
        return f"{subject_part}-{recipient_part}.yaml"
    return f"{subject_part}.yaml"


def _unique_path(directory: Path, name: str) -> Path:
    base = name[:-5] if name.endswith(".yaml") else name
    path = directory / f"{base}.yaml"
    if not path.exists():
        return path
    for i in range(2, 1000):
        candidate = directory / f"{base}-{i}.yaml"
        if not candidate.exists():
            return candidate
    raise SystemExit(f"email: too many existing drafts named like {name!r}")


def _read_body(args: argparse.Namespace) -> str:
    if args.body is not None and args.body_file is not None:
        raise SystemExit("write-email: use only one of --body or --body-file")
    if args.body is not None:
        body = args.body
    elif args.body_file is not None:
        if args.body_file == "-":
            body = sys.stdin.read()
        else:
            try:
                body = Path(args.body_file).read_text()
            except OSError as e:
                raise SystemExit(f"email: cannot read body file: {e}") from e
    elif not sys.stdin.isatty():
        body = sys.stdin.read()
    else:
        raise SystemExit(
            "email: provide body text with --body, --body-file, or stdin"
        )

    body = body.rstrip()
    if not body:
        raise SystemExit("write-email: body is empty")
    return body


def _atomic_dump(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)


def _load_state(path: Path) -> dict[str, Any]:
    try:
        with path.open() as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _wait_for_terminal(path: Path, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = _load_state(path)
    while time.monotonic() < deadline:
        data = _load_state(path)
        if data:
            last = data
        if last.get("draft_state") in _TERMINAL_STATES:
            return last
        time.sleep(0.2)
    return last


def _rel_to_root(path: Path) -> str:
    try:
        return str(path.relative_to(paths.PAI_ROOT))
    except ValueError:
        return str(path)


def _print_result(path: Path, state: dict[str, Any]) -> None:
    draft_state = state.get("draft_state") or "pending"
    result: dict[str, Any] = {
        "path": f"drafts/{path.name}",
        "spool_path": _rel_to_root(path),
        "draft_state": draft_state,
    }
    if state.get("draft_error"):
        result["draft_error"] = state["draft_error"]
    if state.get("send_blocked"):
        result["send_blocked"] = state["send_blocked"]
    print(yaml.safe_dump(result, sort_keys=False), end="")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="write-email",
        description=(
            "Draft or send a Mail.app message. Exactly one of --send / --draft "
            "is required. --send only delivers if the owner granted "
            "capabilities.email_send; under `ask` it is queued for owner "
            "approval, under `no` it falls back to a saved draft."
        ),
    )
    parser.add_argument(
        "--from",
        dest="from_addr",
        required=True,
        help="Mail.app account/address to draft from",
    )
    parser.add_argument(
        "--to", action="append", help="recipient email; repeatable or comma-separated"
    )
    parser.add_argument(
        "--cc", action="append", help="CC recipient; repeatable or comma-separated"
    )
    parser.add_argument(
        "--bcc", action="append", help="BCC recipient; repeatable or comma-separated"
    )
    parser.add_argument("--subject", help="subject; required for new outbound drafts")
    parser.add_argument("--body", "--content", dest="body", help="plain-text draft body")
    parser.add_argument(
        "--body-file",
        "--content-file",
        dest="body_file",
        help="read plain-text draft body from a file, or '-' for stdin",
    )
    parser.add_argument(
        "--in-reply-to", help="parent Message-ID; switches the driver to reply mode"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--send",
        dest="send",
        action="store_true",
        help="set action: send so the driver delivers the message (subject to "
             "the owner's email_send capability; queued for approval under `ask`)",
    )
    mode.add_argument(
        "--draft",
        dest="send",
        action="store_false",
        help="save the message to Mail.app Drafts without sending",
    )
    parser.add_argument(
        "--reference",
        "--references",
        dest="references",
        action="append",
        help="Reference Message-ID; repeatable or comma-separated",
    )
    parser.add_argument(
        "--name", help="draft filename under drafts/; .yaml is added if omitted"
    )
    parser.add_argument(
        "--wait",
        nargs="?",
        const=15.0,
        default=0.0,
        type=float,
        metavar="SECONDS",
        help="wait for macmail-out to reach a terminal state (default with flag: 15)",
    )
    args = parser.parse_args(argv)

    args.from_addr = args.from_addr.strip()
    if not args.from_addr:
        parser.error("--from cannot be empty")
    if not args.subject and not args.in_reply_to:
        parser.error("--subject is required for new outbound drafts")
    if args.wait < 0:
        parser.error("--wait must be >= 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    to_addrs = _split_values(args.to)
    cc_addrs = _split_values(args.cc)
    bcc_addrs = _split_values(args.bcc)
    references = _split_values(args.references)

    if not to_addrs and not args.in_reply_to:
        raise SystemExit(
            "email: provide --to for new mail or --in-reply-to for replies"
        )

    draft: dict[str, Any] = {
        "from": args.from_addr,
        "to": to_addrs,
        "cc": cc_addrs,
        "bcc": bcc_addrs,
        "content": _read_body(args),
        "created_by": os.environ.get("PAI_SLUG") or os.environ.get("PAI_NAME") or "unknown",
    }
    if args.subject:
        draft["subject"] = args.subject
    if args.in_reply_to:
        draft["in_reply_to"] = args.in_reply_to.strip()
    if references:
        draft["references"] = references
    if args.send:
        draft["action"] = "send"

    drafts_dir = paths.var_spool_email_drafts()
    path = _unique_path(drafts_dir, _draft_name(args, to_addrs))
    _atomic_dump(path, draft)

    state = _wait_for_terminal(path, args.wait) if args.wait else draft
    _print_result(path, state)
    return 2 if state.get("draft_state") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
