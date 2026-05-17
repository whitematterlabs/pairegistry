#!/usr/bin/env bash
# Idempotent build + stage for the AX sidecar.
#
# - If sidecar/.build/release/axd exists and is newer than every file under
#   Sources/, skip swift build.
# - Always ad-hoc codesign and copy to $PAI_ROOT/usr/libexec/ax/axd.
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
BUILT="$SCRIPT_DIR/.build/release/axd"

need_build=0
if [[ ! -x "$BUILT" ]]; then
    need_build=1
else
    # Rebuild if any source is newer than the binary.
    while IFS= read -r src; do
        if [[ "$src" -nt "$BUILT" ]]; then
            need_build=1
            break
        fi
    done < <(find Sources -type f \( -name "*.swift" -o -name "*.h" -o -name "*.m" \))
    if [[ "Package.swift" -nt "$BUILT" ]]; then
        need_build=1
    fi
fi

if [[ "$need_build" -eq 1 ]]; then
    if ! command -v swift >/dev/null 2>&1; then
        echo "ax/build.sh: swift not on PATH; install Xcode command line tools" >&2
        exit 1
    fi
    echo "ax/build.sh: swift build -c release"
    swift build -c release
else
    echo "ax/build.sh: $BUILT up to date — skipping swift build"
fi

if [[ ! -x "$BUILT" ]]; then
    echo "ax/build.sh: build produced no binary at $BUILT" >&2
    exit 1
fi

# Ad-hoc codesign so macOS can attach a stable TCC identity to the binary.
# Without this, every rebuild requires re-granting Accessibility.
if command -v codesign >/dev/null 2>&1; then
    codesign --force --sign - "$BUILT" >/dev/null 2>&1 || \
        echo "ax/build.sh: codesign --sign - failed (continuing)" >&2
fi

mkdir -p "$DEST_DIR"
cp "$BUILT" "$DEST"
chmod +x "$DEST"
echo "ax/build.sh: staged → $DEST"
