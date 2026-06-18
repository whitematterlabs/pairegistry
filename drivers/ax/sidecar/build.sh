#!/usr/bin/env bash
# Stage the AX sidecar binary into $PAI_ROOT/usr/libexec/ax/axd.
#
# End-user installs DO NOT compile: a prebuilt, ad-hoc-signed `prebuilt/axd`
# (arm64, AXSwift statically linked) ships inside the driver bundle, so no Swift
# toolchain or network is required. `swift build` runs ONLY on a dev machine
# that has the toolchain AND whose Sources are newer than the committed binary;
# in that case the freshly built binary also refreshes prebuilt/axd so the
# committed artifact stays in sync.
#
# Safe to run as a paiman install hook (cwd = $PAI_ROOT) or directly during
# development from anywhere.

set -euo pipefail

# Locate this script's directory regardless of cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PAI_ROOT="${PAI_ROOT:-$HOME/.pai}"
DEST_DIR="$PAI_ROOT/usr/libexec/ax"
DEST="$DEST_DIR/axd"
PREBUILT="$SCRIPT_DIR/prebuilt/axd"
BUILT="$SCRIPT_DIR/.build/release/axd"

have_swift() { command -v swift >/dev/null 2>&1; }

# Sources newer than the committed prebuilt? (only meaningful on a dev box)
sources_changed() {
    [[ ! -x "$PREBUILT" ]] && return 0
    local src
    while IFS= read -r src; do
        [[ "$src" -nt "$PREBUILT" ]] && return 0
    done < <(find Sources -type f \( -name "*.swift" -o -name "*.h" -o -name "*.m" \))
    [[ "Package.swift" -nt "$PREBUILT" ]]
}

if have_swift && sources_changed; then
    # Dev path: rebuild and refresh the committed prebuilt binary.
    echo "ax/build.sh: swift build -c release"
    swift build -c release
    if [[ ! -x "$BUILT" ]]; then
        echo "ax/build.sh: build produced no binary at $BUILT" >&2
        exit 1
    fi
    mkdir -p "$SCRIPT_DIR/prebuilt"
    cp "$BUILT" "$PREBUILT"
    chmod +x "$PREBUILT"
    echo "ax/build.sh: refreshed $PREBUILT"
elif [[ -x "$PREBUILT" ]]; then
    # End-user / no-change path: ship the committed binary as-is. No toolchain.
    echo "ax/build.sh: using prebuilt $PREBUILT (no compile needed)"
else
    echo "ax/build.sh: no prebuilt axd and no Swift toolchain on PATH." >&2
    echo "ax/build.sh: install Xcode command line tools to build from source," >&2
    echo "ax/build.sh: or ship drivers/ax/sidecar/prebuilt/axd in the bundle." >&2
    exit 1
fi

SOURCE="$PREBUILT"

# Ad-hoc codesign so macOS can attach a stable TCC identity to the binary.
# Without this, every rebuild requires re-granting Accessibility. `codesign`
# ships with stock macOS, so this needs no developer tools.
if command -v codesign >/dev/null 2>&1; then
    codesign --force --sign - "$SOURCE" >/dev/null 2>&1 || \
        echo "ax/build.sh: codesign --sign - failed (continuing)" >&2
fi

mkdir -p "$DEST_DIR"
cp "$SOURCE" "$DEST"
chmod +x "$DEST"
echo "ax/build.sh: staged → $DEST"
