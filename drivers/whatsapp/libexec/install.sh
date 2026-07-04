#!/usr/bin/env bash
# Stage the WhatsApp Baileys bridge into $PAI_ROOT/usr/libexec/whatsapp/ and
# install the `qrcode` Python lib the pairing tool renders the QR with.
#
# paiman runs this as the driver's `hooks.install` step (cwd = $PAI_ROOT). It is
# also safe to run directly during development from anywhere.
#
# What it does:
#   1. Copies the bridge sources (bridge.js + package manifests) from the
#      installed driver bundle into usr/libexec/whatsapp/ — the location
#      whatsapp_pair.py and the inbound/outbound drivers expect bridge.js at.
#   2. Runs `pnpm install --prod` there to populate node_modules (Baileys).
#   3. Installs the `qrcode` Python package into the kernel venv via uv, since
#      whatsapp_pair imports it to render the pairing QR in the terminal.
#
# Pairing is NOT run here — it is interactive (renders a QR, blocks on a phone
# scan) and install may run non-interactively. It is the driver's `hooks.setup`
# step, which paiman offers (default-yes, skippable) right after this hook when
# installing in a terminal. Run `whatsapp_pair` manually to (re)link later.

set -euo pipefail

# Locate this script's dir (the bridge source lives alongside it) regardless of cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
    echo "whatsapp/install.sh: could not resolve OS account home." >&2
    exit 1
fi
PAI_ROOT="${PAI_ROOT:-$REAL_HOME/.pai}"
DEST_DIR="$PAI_ROOT/usr/libexec/whatsapp"
VENV_PY="$PAI_ROOT/usr/lib/venv/bin/python"

# --- 1 + 2: stage the Node bridge and install its deps ---------------------
if ! command -v node >/dev/null 2>&1; then
    echo "whatsapp/install.sh: node not found on PATH — install Node.js first." >&2
    exit 1
fi
if ! command -v pnpm >/dev/null 2>&1; then
    echo "whatsapp/install.sh: pnpm not found on PATH — install pnpm first." >&2
    exit 1
fi

mkdir -p "$DEST_DIR"
for f in bridge.js package.json pnpm-lock.yaml pnpm-workspace.yaml; do
    if [[ -f "$SCRIPT_DIR/$f" ]]; then
        cp "$SCRIPT_DIR/$f" "$DEST_DIR/$f"
    fi
done

echo "whatsapp/install.sh: pnpm install --prod in $DEST_DIR"
( cd "$DEST_DIR" && pnpm install --prod )

# --- 3: install the qrcode Python lib into the kernel venv -----------------
# The venv is built by `uv venv`, which omits pip, so `python -m pip` is not
# available — install via uv against the venv interpreter.
if ! command -v uv >/dev/null 2>&1; then
    echo "whatsapp/install.sh: uv not found on PATH — cannot install the qrcode" >&2
    echo "whatsapp/install.sh: Python lib into the venv. Install uv, then re-run" >&2
    echo "whatsapp/install.sh: paiman install whatsapp." >&2
    exit 1
fi
if [[ ! -x "$VENV_PY" ]]; then
    echo "whatsapp/install.sh: kernel venv python missing at $VENV_PY" >&2
    echo "whatsapp/install.sh: run paifs-init to provision the venv first." >&2
    exit 1
fi
echo "whatsapp/install.sh: uv pip install qrcode -> $VENV_PY"
uv pip install --python "$VENV_PY" qrcode

echo "whatsapp/install.sh: staged → $DEST_DIR"
echo "whatsapp/install.sh: WhatsApp bridge ready — paiman will offer to link your phone (QR) next, or run 'whatsapp_pair' anytime."
