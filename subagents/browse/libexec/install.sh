#!/usr/bin/env bash
# Provisions the browse subagent: isolated venv, vendored browser-use,
# Playwright Chromium, and the cookie store. Idempotent.
set -euo pipefail

PAI_ROOT="${PAI_ROOT:-$HOME/.pai}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIBEXEC="$PAI_ROOT/usr/libexec/subagents/browse"
VENV="$LIBEXEC/venv"
VENDOR="$LIBEXEC/vendor"

# Pinned versions — bump deliberately.
BROWSER_USE_VERSION="0.1.40"

echo "[browse/install] PAI_ROOT=$PAI_ROOT"
echo "[browse/install] libexec=$LIBEXEC"

# Package owns its own state slot under /var/lib/<name>/.
mkdir -p "$PAI_ROOT/var/lib/browse/cookies"

# entry.py + chrome_cookies_import.py live in the source tree; expose them
# at the top of the libexec slot for the prompt's documented invocation.
ln -sfn "$HERE/../entry.py" "$LIBEXEC/entry.py"
ln -sfn "$HERE/chrome_cookies_import.py" "$LIBEXEC/chrome_cookies_import.py"

# Isolated venv (separate from kernel venv to keep deps pinned & private).
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
  pycryptodome \
  pyyaml \
  langchain-anthropic \
  langchain-openai

# Install Chromium browser into the libexec tree so it travels with the
# package and does not pollute the user's home cache.
export PLAYWRIGHT_BROWSERS_PATH="$LIBEXEC/playwright-browsers"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"
echo "[browse/install] installing Playwright Chromium into $PLAYWRIGHT_BROWSERS_PATH"
PYTHONPATH="$VENDOR" "$VENV/bin/python" -m playwright install chromium

# Persist the browsers path so entry.py (and its child processes) see it.
cat > "$LIBEXEC/env" <<EOF
export PLAYWRIGHT_BROWSERS_PATH="$PLAYWRIGHT_BROWSERS_PATH"
export PYTHONPATH="$VENDOR\${PYTHONPATH:+:\$PYTHONPATH}"
EOF

echo "[browse/install] done."
