#!/usr/bin/env python
"""propose-send — queue an outbound action for the owner to approve.

A PAI holding a send capability in `approve` mode cannot send directly; it
*proposes*. This writes one YAML record into var/spool/approvals/. The owner
approves it in the web console; the approvals driver then delivers it through
the channel's native send path. This command NEVER sends anything itself.

Carries `email` and `imessage`.

  propose-send --channel email --from me@x.com --to bob@acme.com \
    --subject "Re: Q3 budget" --body - --summary "reply to bob re Q3 budget" \
    --source-event email:new --source-ref communication/email/.../msg.yaml

  propose-send --channel imessage --thread bob --body - \
    --summary "reply to bob" --source-event imessage:new
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
CHANNELS = ("email", "imessage")


def _slug(value: str) -> str:
    s = _NON_ALNUM.sub("-", (value or "").lower()).strip("-")
    return s[:60] or "send"


def _split(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in values or []:
        for part in str(raw).split(","):
            p = part.strip()
            if p:
                out.append(p)
    return out


def _read_body(value: str | None) -> str:
    if value is None or value == "-":
        return sys.stdin.read()
    return value


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="propose-send",
        description="Queue an outbound action for owner approval. Never sends.",
    )
    p.add_argument("--channel", required=True, choices=CHANNELS)
    p.add_argument(
        "--summary", required=True,
        help="one-line description shown in the owner's approval queue",
    )
    p.add_argument("--body", default="-", help="message body; '-' (default) reads stdin")
    # email
    p.add_argument("--from", dest="from_addr", default="", help="sending Mail.app account")
    p.add_argument("--to", action="append", help="recipient(s); repeatable or comma-separated")
    p.add_argument("--cc", action="append", help="cc(s); repeatable or comma-separated")
    p.add_argument("--subject", default="")
    p.add_argument("--in-reply-to", dest="in_reply_to", default="", help="Message-ID for a reply")
    # imessage
    p.add_argument("--thread", default="", help="iMessage thread slug (home/messages/<slug>/)")
    # provenance (optional)
    p.add_argument("--source-event", dest="source_event", default="")
    p.add_argument("--source-ref", dest="source_ref", default="")
    args = p.parse_args(argv)

    if args.channel == "email":
        if not args.from_addr.strip():
            p.error("--from is required for --channel email")
        if not _split(args.to) and not args.in_reply_to.strip():
            p.error("email needs --to (new message) or --in-reply-to (reply)")
    elif args.channel == "imessage":
        if not args.thread.strip():
            p.error("--thread is required for --channel imessage")
    return args


def _build_record(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    body = _read_body(args.body)
    if args.channel == "email":
        action: dict[str, Any] = {
            "from": args.from_addr.strip(),
            "to": _split(args.to),
            "cc": _split(args.cc),
            "content": body,
        }
        if args.subject:
            action["subject"] = args.subject
        if args.in_reply_to.strip():
            action["in_reply_to"] = args.in_reply_to.strip()
        to = _split(args.to)
        label = args.subject or (to[0] if to else "email")
    elif args.channel == "imessage":
        action = {
            "thread": args.thread.strip(),
            "text": body,
        }
        label = args.thread.strip()
    else:  # pragma: no cover - guarded by choices
        raise SystemExit(f"propose-send: unsupported channel {args.channel!r}")

    preview = " ".join(body.split())[:200]
    rec: dict[str, Any] = {
        "channel": args.channel,
        "status": "pending",
        "created_by": os.environ.get("PAI_SLUG") or os.environ.get("PAI_NAME") or "unknown",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "summary": args.summary.strip(),
        "body_preview": preview,
        "action": action,
        "decided_at": None,
        "decided_by": None,
        "error": None,
    }
    if args.source_event:
        rec["source_event"] = args.source_event
    if args.source_ref:
        rec["source_ref"] = args.source_ref
    return rec, label


def _atomic_dump(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)


def _unique_path(d: Path, ident: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{ident}.yaml"
    n = 1
    while path.exists():
        path = d / f"{ident}-{n}.yaml"
        n += 1
    return path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    rec, label = _build_record(args)
    ident = time.strftime("%Y%m%d-%H%M%S") + "-" + _slug(label)
    path = _unique_path(paths.var_spool_approvals(), ident)
    rec["id"] = path.stem
    _atomic_dump(path, rec)
    print(f"queued for approval: {path.stem} ({args.channel}) — {rec['summary']}")
    print(f"  {path}")
    print("  (the owner approves it in the web console; nothing is sent until then)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
