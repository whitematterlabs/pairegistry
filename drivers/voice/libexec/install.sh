#!/usr/bin/env bash
# Provisions /usr/libexec/voice/ with whisper.cpp binary + ggml-base.en model,
# and installs the driver's Python deps into the FHS venv.
# Idempotent: skips clone+build if binary already present (use --force to redo).
set -euo pipefail

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HERE/whisper-cli"
MODEL="$HERE/ggml-base.en.bin"
SRC="$HERE/whisper.cpp"

PAI_ROOT="${PAI_ROOT:-$HOME/.pai}"
VENV_PY="$PAI_ROOT/usr/lib/venv/bin/python"

# ── Python deps into FHS venv ────────────────────────────────────────
PY_DEPS=(openwakeword onnxruntime sounddevice webrtcvad numpy soundfile)
if [[ -x "$VENV_PY" ]]; then
  echo "[voice/install] installing Python deps into FHS venv: ${PY_DEPS[*]}"
  uv pip install --python "$VENV_PY" "${PY_DEPS[@]}"
  echo "[voice/install] downloading openwakeword ONNX models (one-time)"
  "$VENV_PY" -c "from openwakeword.utils import download_models; download_models()"
else
  echo "[voice/install] WARNING: FHS venv not found at $VENV_PY — run paifs-init first" >&2
fi

# ── portaudio system dep (sounddevice needs it) ──────────────────────
if ! pkg-config --exists portaudio-2.0 2>/dev/null && ! brew list portaudio &>/dev/null; then
  if command -v brew >/dev/null; then
    echo "[voice/install] installing portaudio via brew (required by sounddevice)"
    brew install portaudio
  else
    echo "[voice/install] WARNING: portaudio not found and brew unavailable; sounddevice will fail at runtime" >&2
  fi
fi

if [[ -x "$BIN" && -f "$MODEL" && $FORCE -eq 0 ]]; then
  echo "[voice/install] whisper-cli + model already present; skipping (use --force to redo)"
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
