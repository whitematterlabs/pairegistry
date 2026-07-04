#!/usr/bin/env bash
# Stage the browse Playwright sidecar into $PAI_ROOT/usr/libexec/browse/ and
# install its Node deps there with `npm ci --omit=dev`.
#
# Runtime artifacts (node_modules) live in the FHS libexec slot, NOT inside the
# bundle source — server.mjs reads playwright-core from exactly here, and
# keeping node_modules out of the bundle means paiman doesn't re-copy it on
# every install. Mirrors drivers/voice/libexec/install.sh and ax/sidecar/build.sh.
#
# Runs as a paiman install hook (cwd = $PAI_ROOT). Locates its own source
# regardless of cwd, so it also works run directly during development.
#
# playwright-core (NOT playwright) pulls no browser download and has no
# postinstall, so `npm ci` finishes well under paiman's 120s hook timeout.
set -euo pipefail

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
  echo "[browse/install] ERROR: could not resolve OS account home." >&2
  exit 1
fi
PAI_ROOT="${PAI_ROOT:-$REAL_HOME/.pai}"
DEST="$PAI_ROOT/usr/libexec/browse"

# Locate node/npm. paiman's install hook may run with a sanitized PATH that
# misses an nvm-managed node, so fall back to common locations the same way
# browse.py's _find_node does. Graceful, like `pai start --web` skipping on a
# missing toolchain — a clear message, not a stack trace.
find_tool() {
  local tool="$1"
  if command -v "$tool" >/dev/null 2>&1; then command -v "$tool"; return 0; fi
  local c
  for c in "/opt/homebrew/bin/$tool" "/usr/local/bin/$tool"; do
    [[ -x "$c" ]] && { echo "$c"; return 0; }
  done
  # latest nvm-managed node/npm
  if [[ -d "$REAL_HOME/.nvm/versions/node" ]]; then
    c="$(ls -d "$REAL_HOME"/.nvm/versions/node/*/bin/"$tool" 2>/dev/null | sort -V | tail -1)"
    [[ -n "$c" && -x "$c" ]] && { echo "$c"; return 0; }
  fi
  return 1
}

NPM="$(find_tool npm || true)"
NODE="$(find_tool node || true)"
if [[ -z "$NPM" || -z "$NODE" ]]; then
  echo "[browse/install] ERROR: node/npm not found on PATH." >&2
  echo "[browse/install] browse needs system Node to run its Playwright sidecar." >&2
  echo "[browse/install] Install Node (e.g. 'brew install node') and re-run:" >&2
  echo "[browse/install]   paiman install bin/browse" >&2
  exit 1
fi

# Put the located node's dir first on PATH so npm's own node lookup resolves.
export PATH="$(dirname "$NODE"):$PATH"

mkdir -p "$DEST"
cp "$SCRIPT_DIR/server.mjs"        "$DEST/server.mjs"
cp "$SCRIPT_DIR/package.json"      "$DEST/package.json"
cp "$SCRIPT_DIR/package-lock.json" "$DEST/package-lock.json"

echo "[browse/install] npm ci --omit=dev in $DEST"
( cd "$DEST" && "$NPM" ci --omit=dev )

echo "[browse/install] done. sidecar=$DEST (playwright-core installed)"
