#!/usr/bin/env bash
# Installs the notetaker driver's Python deps into the FHS venv. Idempotent.
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
PAI_ROOT="${PAI_ROOT:-$REAL_HOME/.pai}"
VENV_PY="$PAI_ROOT/usr/lib/venv/bin/python"

# CoreAudio → process tap + aggregate device creation (the IO leg is ctypes,
# no extra dep). sounddevice/numpy usually arrive with voice, but the dep is
# ours too, so declare it.
PY_DEPS=(pyobjc-framework-CoreAudio sounddevice numpy)
if [[ -x "$VENV_PY" ]]; then
  echo "[notetaker/install] installing Python deps into FHS venv: ${PY_DEPS[*]}"
  uv pip install --python "$VENV_PY" "${PY_DEPS[@]}"
else
  echo "[notetaker/install] WARNING: FHS venv not found at $VENV_PY — run paifs-init first" >&2
fi
