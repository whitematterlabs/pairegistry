#!/usr/bin/env python
"""slack_setup — paste the Slack app tokens once.

Prompts for the app-level token (`xapp-…`, opens the Socket Mode socket) and
the bot token (`xoxb-…`, used for Web API calls) and writes them to
sys/drivers/slack/tokens.yaml (chmod 600). Idempotent: if tokens already
exist it shows a masked preview and offers to re-enter, otherwise leaves them.

Get the tokens from your Slack app config (https://api.slack.com/apps):
  - Socket Mode → Generate an app-level token with `connections:write`  → xapp-
  - OAuth & Permissions → Bot User OAuth Token (after installing to the
    workspace)                                                          → xoxb-
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

from boot import paths
from boot import processes as P

DRIVER_SLUG = "slack-in"
TOKENS_PATH = paths.PAI_ROOT / "sys" / "drivers" / "slack" / "tokens.yaml"


def _mask(token: str) -> str:
    token = (token or "").strip()
    if len(token) <= 10:
        return "…" if token else "(empty)"
    return f"{token[:8]}…{token[-4:]}"


def _load_existing() -> dict:
    if not TOKENS_PATH.exists():
        return {}
    try:
        with TOKENS_PATH.open() as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _prompt(label: str, prefix: str, current: str) -> str:
    hint = f" [{_mask(current)}]" if current else ""
    while True:
        raw = input(f"{label} ({prefix}…){hint}: ").strip()
        if not raw and current:
            return current  # keep existing on empty
        if not raw:
            print(f"  a {prefix} token is required.", file=sys.stderr)
            continue
        if not raw.startswith(prefix):
            print(f"  that doesn't look like a {prefix} token — try again.", file=sys.stderr)
            continue
        return raw


def _write(app_token: str, bot_token: str) -> None:
    TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKENS_PATH.with_suffix(".yaml.tmp")
    with tmp.open("w") as f:
        yaml.safe_dump({"app_token": app_token, "bot_token": bot_token}, f, sort_keys=False)
    os.replace(tmp, TOKENS_PATH)
    try:
        TOKENS_PATH.chmod(0o600)
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(prog="slack_setup", description=__doc__)
    ap.add_argument(
        "--force",
        action="store_true",
        help="re-enter tokens without the already-configured prompt",
    )
    args = ap.parse_args()

    existing = _load_existing()
    have_both = bool(existing.get("app_token")) and bool(existing.get("bot_token"))

    if have_both and not args.force:
        print("Slack tokens already configured:")
        print(f"  app_token: {_mask(str(existing.get('app_token')))}")
        print(f"  bot_token: {_mask(str(existing.get('bot_token')))}")
        try:
            ans = input("Re-enter them? [y/N]: ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("Keeping existing tokens.")
            return 0

    if not sys.stdin.isatty():
        print(
            "slack_setup needs a terminal to paste tokens. Run it directly:\n"
            "    slack_setup",
            file=sys.stderr,
        )
        return 2

    print("Paste your Slack app tokens (leave blank to keep the existing value):\n")
    app_token = _prompt("App-level token", "xapp-", str(existing.get("app_token") or ""))
    bot_token = _prompt("Bot token", "xoxb-", str(existing.get("bot_token") or ""))

    _write(app_token, bot_token)
    print(f"\n✓ tokens written to {TOKENS_PATH} (chmod 600).")

    # A running slack-in loaded the old tokens at startup — it won't pick these
    # up until restarted.
    try:
        if P.read_status(DRIVER_SLUG) == "running":
            print(
                f"\nNote: {DRIVER_SLUG} is running with the previous tokens. Restart it:\n"
                f"    paictl stop {DRIVER_SLUG} && paictl start {DRIVER_SLUG}"
            )
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
