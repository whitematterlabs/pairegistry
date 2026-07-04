#!/usr/bin/env bash
# Provisions the cloud voice provider's deps into the FHS venv. No native build,
# no model download — OpenAI STT and ElevenLabs TTS are plain HTTPS calls, so all
# this needs is `requests` (HTTP) and `python-dotenv` (re-read keys mid-session).
# Idempotent: uv pip no-ops when the deps are already satisfied.
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
  echo "[voice_cloud/install] ERROR: could not resolve OS account home." >&2
  exit 1
fi
PAI_ROOT="${PAI_ROOT:-$REAL_HOME/.pai}"
VENV_PY="$PAI_ROOT/usr/lib/venv/bin/python"

PY_DEPS=(requests python-dotenv)
if [[ -x "$VENV_PY" ]]; then
  echo "[voice_cloud/install] installing Python deps into FHS venv: ${PY_DEPS[*]}"
  uv pip install --python "$VENV_PY" "${PY_DEPS[@]}"
else
  echo "[voice_cloud/install] WARNING: FHS venv not found at $VENV_PY — run paifs-init first" >&2
fi

echo "[voice_cloud/install] done."
