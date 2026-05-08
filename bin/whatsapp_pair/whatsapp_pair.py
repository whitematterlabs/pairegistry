#!/usr/bin/env python
"""whatsapp-pair — pair the WhatsApp Baileys bridge once.

Spawns bridge.js, renders the QR it emits into your terminal, blocks
until the bridge reports `status: open`. Writes credentials to
/Users/arda/.pai/sys/drivers/whatsapp/auth/. Run once per machine.

Refuses if whatsapp-in is running (auth dir is single-writer).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from boot import paths
from boot import processes as P

import qrcode

DRIVER_SLUG = "whatsapp-in"
PAI_ROOT = paths.PAI_ROOT
BRIDGE_JS = PAI_ROOT / "usr" / "libexec" / "whatsapp" / "bridge.js"
AUTH_DIR = PAI_ROOT / "sys" / "drivers" / "whatsapp" / "auth"
QR_TIMEOUT = 90


def _render_qr(data: str) -> None:
    qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(data)
    qr.make(fit=True)
    qr.print_ascii(out=sys.stdout, invert=True)
    sys.stdout.flush()


def _refuse_if_driver_running() -> None:
    try:
        if P.read_status(DRIVER_SLUG) == "running":
            print(
                f"refusing: {DRIVER_SLUG} is running. Stop it first:\n"
                f"    paictl stop {DRIVER_SLUG}",
                file=sys.stderr,
            )
            sys.exit(2)
    except Exception:
        pass


def _check_already_paired() -> bool:
    if not AUTH_DIR.exists():
        return False
    creds = AUTH_DIR / "creds.json"
    return creds.exists() and creds.stat().st_size > 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="whatsapp-pair", description=__doc__)
    ap.add_argument(
        "--force",
        action="store_true",
        help="re-pair even if creds.json already exists (wipes existing auth)",
    )
    args = ap.parse_args()

    _refuse_if_driver_running()

    if _check_already_paired() and not args.force:
        print(
            f"already paired (creds in {AUTH_DIR}). Use --force to re-pair.",
            file=sys.stderr,
        )
        return 0

    if args.force and AUTH_DIR.exists():
        for f in AUTH_DIR.iterdir():
            f.unlink()

    if not BRIDGE_JS.exists():
        print(f"bridge.js missing at {BRIDGE_JS}", file=sys.stderr)
        return 1
    node = shutil.which("node")
    if not node:
        print("node not found on PATH", file=sys.stderr)
        return 1

    AUTH_DIR.mkdir(parents=True, exist_ok=True)

    print("starting WhatsApp bridge — scan the QR with your phone:")
    print("  WhatsApp → Settings → Linked Devices → Link a Device\n")

    proc = subprocess.Popen(
        [node, str(BRIDGE_JS)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PAI_ROOT": str(PAI_ROOT)},
        text=True,
    )

    deadline = time.monotonic() + QR_TIMEOUT
    rc = 1
    try:
        while True:
            if time.monotonic() > deadline:
                print("\ntimed out waiting for QR scan.", file=sys.stderr)
                break
            line = proc.stdout.readline()
            if not line:
                err = proc.stderr.read()
                print(f"\nbridge exited unexpectedly: {err.strip()}", file=sys.stderr)
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = msg.get("type")
            if t == "qr":
                print("\n--- scan this QR with your phone ---\n")
                _render_qr(msg["qr"])
                print(f"\n(refresh in 20s, total timeout {QR_TIMEOUT}s)")
                deadline = time.monotonic() + QR_TIMEOUT
            elif t == "status":
                state = msg.get("state")
                if state == "open":
                    print("\n✓ paired. Credentials saved.")
                    rc = 0
                    break
                elif state == "close":
                    print("\nbridge closed before pairing completed.", file=sys.stderr)
                    break
            elif t == "error":
                print(f"\nbridge error: {msg.get('error')}", file=sys.stderr)
                break
    finally:
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            pass

    return rc


if __name__ == "__main__":
    sys.exit(main())
