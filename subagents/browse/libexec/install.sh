#!/usr/bin/env bash
# Provisions the browse subagent: isolated venv + vendored browser-use.
# No bundled Chromium — browse only attaches to the owner's real Chrome
# over CDP, so Playwright is just the CDP client transport.
set -euo pipefail

PAI_ROOT="${PAI_ROOT:-$HOME/.pai}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIBEXEC="$PAI_ROOT/usr/libexec/subagents/browse"
VENV="$LIBEXEC/venv"
VENDOR="$LIBEXEC/vendor"

BROWSER_USE_VERSION="0.1.40"

echo "[browse/install] PAI_ROOT=$PAI_ROOT"
echo "[browse/install] libexec=$LIBEXEC"

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "[browse/install] creating venv at $VENV"
  python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --upgrade --quiet pip

mkdir -p "$VENDOR"
echo "[browse/install] installing browser-use==$BROWSER_USE_VERSION + deps into $VENDOR"
"$VENV/bin/python" -m pip install --quiet \
  --target "$VENDOR" \
  --upgrade \
  "browser-use==$BROWSER_USE_VERSION" \
  playwright \
  pyyaml \
  langchain-anthropic \
  langchain-openai

cat > "$LIBEXEC/env" <<EOF
export PYTHONPATH="$VENDOR\${PYTHONPATH:+:\$PYTHONPATH}"
EOF

ENTRY="$PAI_ROOT/usr/lib/subagents/browse/entry.py"
SHIM="$PAI_ROOT/usr/bin/browse"
mkdir -p "$PAI_ROOT/usr/bin"
cat > "$SHIM" <<EOF
#!/usr/bin/env bash
exec env PYTHONPATH="$VENDOR\${PYTHONPATH:+:\$PYTHONPATH}" \\
  "$VENV/bin/python" "$ENTRY" "\$@"
EOF
chmod +x "$SHIM"
echo "[browse/install] shim → $SHIM"

echo "[browse/install] done."
