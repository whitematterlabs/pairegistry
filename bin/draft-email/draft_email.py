#!/usr/bin/env python
"""draft-email - create a Mail.app draft through the email driver spool.

This command never sends mail. It writes one YAML file under
var/spool/communication/email/drafts/; macmail-out notices the file and
saves it into Mail.app Drafts for the owner to review and send.
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
            raise SystemExit("draft-email: --name cannot be empty")
        if raw != Path(raw).name:
            raise SystemExit("draft-email: --name must be a filename, not a path")
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
    raise SystemExit(f"draft-email: too many existing drafts named like {name!r}")


def _read_body(args: argparse.Namespace) -> str:
    if args.body is not None and args.body_file is not None:
        raise SystemExit("draft-email: use only one of --body or --body-file")
    if args.body is not None:
        body = args.body
    elif args.body_file is not None:
        if args.body_file == "-":
            body = sys.stdin.read()
        else:
            try:
                body = Path(args.body_file).read_text()
            except OSError as e:
                raise SystemExit(f"draft-email: cannot read body file: {e}") from e
    elif not sys.stdin.isatty():
        body = sys.stdin.read()
    else:
        raise SystemExit(
            "draft-email: provide body text with --body, --body-file, or stdin"
        )

    body = body.rstrip()
    if not body:
        raise SystemExit("draft-email: body is empty")
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
        if last.get("draft_state") in {"drafted", "failed"}:
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
    print(yaml.safe_dump(result, sort_keys=False), end="")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="draft-email",
        description=(
            "Create a Mail.app draft. This writes a draft YAML only; it never sends."
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
        help="wait for macmail-out to mark the draft drafted/failed (default with flag: 15)",
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
            "draft-email: provide --to for new mail or --in-reply-to for replies"
        )

    draft: dict[str, Any] = {
        "from": args.from_addr,
        "to": to_addrs,
        "cc": cc_addrs,
        "bcc": bcc_addrs,
        "content": _read_body(args),
    }
    if args.subject:
        draft["subject"] = args.subject
    if args.in_reply_to:
        draft["in_reply_to"] = args.in_reply_to.strip()
    if references:
        draft["references"] = references

    drafts_dir = paths.var_spool_email_drafts()
    path = _unique_path(drafts_dir, _draft_name(args, to_addrs))
    _atomic_dump(path, draft)

    state = _wait_for_terminal(path, args.wait) if args.wait else draft
    _print_result(path, state)
    return 2 if state.get("draft_state") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
