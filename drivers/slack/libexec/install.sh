#!/usr/bin/env bash
# Install the Slack driver's one dependency: the `slack_sdk` Python package,
# into the kernel venv. Socket Mode holds an outbound WebSocket, so there is no
# Node bridge, no tunnel, and no public URL to provision — unlike whatsapp.
#
# paiman runs this as the driver's `hooks.install` step (cwd = $PAI_ROOT). It is
# also safe to run directly during development from anywhere.
#
# Token pasting is NOT done here — it is interactive (blocks on a terminal
# prompt) and install may run non-interactively. It is the driver's
# `hooks.setup` step (usr/bin/slack_setup), which paiman offers (default-yes,
# skippable) right after this hook when installing in a terminal.

set -euo pipefail

real_home() {
  if command -v python3 >/dev/null 2>&1; then
    python3 - <<'PY'
import os
import pwd
print(pwd.getpwuid(os.getuid()).pw_dir)
PY
    return
  fi
  if command -v dscl >/dev/null 2>&1; then
    dscl . -read "/Users/$(id -un)" NFSHomeDirectory | awk '{print $2}'
    return
  fi
  echo "real_home: could not resolve OS account home" >&2
  return 1
}
REAL_HOME="$(real_home)" || exit 1
if [[ -z "$REAL_HOME" ]]; then
    echo "slack/install.sh: could not resolve OS account home." >&2
    exit 1
fi
PAI_ROOT="${PAI_ROOT:-$REAL_HOME/.pai}"
VENV_PY="$PAI_ROOT/usr/lib/venv/bin/python"

# The venv is built by `uv venv`, which omits pip, so `python -m pip` is not
# available — install via uv against the venv interpreter.
if ! command -v uv >/dev/null 2>&1; then
    echo "slack/install.sh: uv not found on PATH — cannot install slack_sdk into" >&2
    echo "slack/install.sh: the venv. Install uv, then re-run paiman install slack." >&2
    exit 1
fi
if [[ ! -x "$VENV_PY" ]]; then
    echo "slack/install.sh: kernel venv python missing at $VENV_PY" >&2
    echo "slack/install.sh: run paifs-init to provision the venv first." >&2
    exit 1
fi

echo "slack/install.sh: uv pip install slack_sdk -> $VENV_PY"
uv pip install --python "$VENV_PY" slack_sdk

echo "slack/install.sh: slack_sdk installed — paiman will offer to paste your Slack tokens next, or run 'slack_setup' anytime."
