#!/usr/bin/env bash
# Installs the cowork driver's Python deps into the FHS venv. Idempotent.
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

# ApplicationServices → AX window titles/documents; FSEvents → file activity.
# AppKit/Quartz (activation notifications, pasteboard, idle) ship with the
# kernel's own pyobjc deps already.
PY_DEPS=(pyobjc-framework-ApplicationServices pyobjc-framework-FSEvents)
if [[ -x "$VENV_PY" ]]; then
  echo "[cowork/install] installing Python deps into FHS venv: ${PY_DEPS[*]}"
  uv pip install --python "$VENV_PY" "${PY_DEPS[@]}"
else
  echo "[cowork/install] WARNING: FHS venv not found at $VENV_PY — run paifs-init first" >&2
fi
