#!/usr/bin/env bash
# Provisions the cloud voice provider's deps into the FHS venv. No native build,
# no model download — OpenAI STT and ElevenLabs TTS are plain HTTPS calls, so all
# this needs is `requests` (HTTP) and `python-dotenv` (re-read keys mid-session).
# Idempotent: uv pip no-ops when the deps are already satisfied.
set -euo pipefail

PAI_ROOT="${PAI_ROOT:-$HOME/.pai}"
VENV_PY="$PAI_ROOT/usr/lib/venv/bin/python"

PY_DEPS=(requests python-dotenv)
if [[ -x "$VENV_PY" ]]; then
  echo "[voice_cloud/install] installing Python deps into FHS venv: ${PY_DEPS[*]}"
  uv pip install --python "$VENV_PY" "${PY_DEPS[@]}"
else
  echo "[voice_cloud/install] WARNING: FHS venv not found at $VENV_PY — run paifs-init first" >&2
fi

echo "[voice_cloud/install] done."
