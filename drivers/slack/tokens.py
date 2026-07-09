"""Slack token store — leaf module, no driver-side imports.

Both `slack-in` and `slack-out` read the app-level (`xapp-`) and bot
(`xoxb-`) tokens from `sys/drivers/slack/tokens.yaml`. That file is written
by the `slack_setup` TTY tool (chmod 600) and is never in any PAI's prompt
or home view. Kept as a leaf so inbound/outbound can share it without an
import cycle.
"""

from __future__ import annotations

import yaml

from boot import paths

TOKENS_PATH = paths.PAI_ROOT / "sys" / "drivers" / "slack" / "tokens.yaml"


def load_tokens() -> dict:
    if not TOKENS_PATH.exists():
        return {}
    try:
        with TOKENS_PATH.open() as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def bot_token() -> str:
    """The bot user OAuth token (`xoxb-…`) used for Web API calls."""
    return str(load_tokens().get("bot_token") or "").strip()


def app_token() -> str:
    """The app-level token (`xapp-…`) used to open the Socket Mode socket."""
    return str(load_tokens().get("app_token") or "").strip()
