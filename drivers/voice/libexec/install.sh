#!/usr/bin/env bash
# Provisions /usr/libexec/voice/ with whisper.cpp binary + ggml-base.en model,
# installs the driver's Python deps into the FHS venv, downloads openWakeWord
# ONNX models, and ensures system deps (portaudio, tmux) are present.
#
# Idempotent. The whisper.cpp clone+build is the only step gated by --force;
# everything else is safe to re-run (uv pip / brew / download_models all no-op
# when satisfied).
set -euo pipefail

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HERE/whisper-cli"
MODEL="$HERE/ggml-base.en.bin"
SRC="$HERE/whisper.cpp"

PAI_ROOT="${PAI_ROOT:-$HOME/.pai}"
VENV_PY="$PAI_ROOT/usr/lib/venv/bin/python"

# ── system deps via brew ──────────────────────────────────────────────
brew_install() {
  local pkg="$1"
  if brew list "$pkg" &>/dev/null; then
    return 0
  fi
  if ! command -v brew >/dev/null; then
    echo "[voice/install] WARNING: brew not found; cannot install $pkg" >&2
    return 1
  fi
  echo "[voice/install] brew install $pkg"
  brew install "$pkg"
}

brew_install portaudio   # sounddevice runtime dep

# ── Python deps into FHS venv ────────────────────────────────────────
PY_DEPS=(openwakeword onnxruntime sounddevice webrtcvad "setuptools<81" numpy soundfile)
if [[ -x "$VENV_PY" ]]; then
  echo "[voice/install] installing Python deps into FHS venv: ${PY_DEPS[*]}"
  uv pip install --python "$VENV_PY" "${PY_DEPS[@]}"
  echo "[voice/install] downloading openwakeword ONNX models (one-time)"
  "$VENV_PY" -c "from openwakeword.utils import download_models; download_models()"
else
  echo "[voice/install] WARNING: FHS venv not found at $VENV_PY — run paifs-init first" >&2
fi

# ── whisper.cpp build (gated) ────────────────────────────────────────
if [[ -x "$BIN" && -f "$MODEL" && $FORCE -eq 0 ]]; then
  echo "[voice/install] whisper-cli + model already present; skipping build (use --force to rebuild)"
  exit 0
fi

if [[ ! -d "$SRC" ]]; then
  echo "[voice/install] cloning whisper.cpp..."
  git clone --depth 1 https://github.com/ggerganov/whisper.cpp "$SRC"
else
  echo "[voice/install] whisper.cpp already cloned; pulling latest"
  (cd "$SRC" && git pull --ff-only || true)
fi

echo "[voice/install] building whisper.cpp (Metal-accelerated on macOS)..."
(cd "$SRC" && make -j)

# whisper.cpp recently renamed `main` -> `whisper-cli`. Try both.
BUILT=""
for candidate in "$SRC/build/bin/whisper-cli" "$SRC/whisper-cli" "$SRC/main" "$SRC/build/bin/main"; do
  if [[ -x "$candidate" ]]; then BUILT="$candidate"; break; fi
done
if [[ -z "$BUILT" ]]; then
  echo "[voice/install] ERROR: whisper.cpp build produced no whisper-cli/main binary" >&2
  exit 1
fi
cp "$BUILT" "$BIN"
chmod +x "$BIN"

if [[ ! -f "$MODEL" ]]; then
  echo "[voice/install] downloading ggml-base.en model..."
  (cd "$SRC" && bash ./models/download-ggml-model.sh base.en)
  cp "$SRC/models/ggml-base.en.bin" "$MODEL"
fi

echo "[voice/install] done. binary=$BIN model=$MODEL"
