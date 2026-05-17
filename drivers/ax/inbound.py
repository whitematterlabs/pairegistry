"""AX inbound driver (Phase 1, sensor only).

Supervises the Swift sidecar `axd` as a subprocess. The sidecar fans in
AXObserver notifications across every running macOS app and emits one
NDJSON event per line on stdout. We re-emit them onto the kernel bus via
P.emit_event and also append the raw NDJSON to /var/log/ax/events.ndjson
for cheap `tail -f` debugging.

No actuation, no RPC, no tree compression in Phase 1.

Requires the Accessibility TCC grant for `axd` (System Settings → Privacy
& Security → Accessibility). On first launch the sidecar will trigger the
prompt; if denied it emits ax:permission_lost and exits cleanly so the
kernel marks ax-in failed instead of crash-looping.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from boot import processes as P

PAI_ROOT = Path(os.environ.get("PAI_ROOT", str(Path.home() / ".pai")))

# Two candidate binary locations: the post-install staged copy (preferred)
# and the bundle-local dev build. build.sh stages to the libexec path on
# install; running build.sh manually during development also stages it.
AXD_LIBEXEC = PAI_ROOT / "usr" / "libexec" / "ax" / "axd"
AXD_BUNDLE = PAI_ROOT / "usr" / "lib" / "drivers" / "ax" / "sidecar" / ".build" / "release" / "axd"

STATE_DIR = PAI_ROOT / "sys" / "drivers" / "ax"
SIDECAR_LOG = STATE_DIR / "sidecar.stderr.log"

EVENT_LOG_DIR = PAI_ROOT / "var" / "log" / "ax"
EVENT_LOG = EVENT_LOG_DIR / "events.ndjson"
# Rotate when the live log exceeds this size; keep one .1 generation.
EVENT_LOG_MAX_BYTES = 32 * 1024 * 1024

SLUG = "ax-in"

# Phase 1: only operationally critical events go on the kernel bus.
# Everything else lives in the NDJSON event log. See the firehose comment
# in _read_stdout.
_BUS_EMIT_KINDS = frozenset({
    "permission_lost",
    "secure_input_active",
    "secure_input_cleared",
    "event_rate_capped",
})


def _resolve_axd() -> Optional[Path]:
    for cand in (AXD_LIBEXEC, AXD_BUNDLE):
        if cand.exists() and os.access(cand, os.X_OK):
            return cand
    return None


def _maybe_rotate_log() -> None:
    try:
        if EVENT_LOG.exists() and EVENT_LOG.stat().st_size > EVENT_LOG_MAX_BYTES:
            rotated = EVENT_LOG.with_suffix(".ndjson.1")
            if rotated.exists():
                rotated.unlink()
            EVENT_LOG.rename(rotated)
    except OSError:
        pass


def _append_event_log(line: bytes) -> None:
    try:
        with EVENT_LOG.open("ab") as f:
            f.write(line)
            if not line.endswith(b"\n"):
                f.write(b"\n")
    except OSError as e:
        print(f"[ax-in] event log write failed: {e!r}", flush=True)


async def _drain_stderr(proc: asyncio.subprocess.Process) -> None:
    assert proc.stderr is not None
    try:
        with SIDECAR_LOG.open("ab") as f:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                f.write(line)
                f.flush()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[ax-in] stderr drain error: {e!r}", flush=True)


async def _read_stdout(proc: asyncio.subprocess.Process) -> None:
    """Parse one NDJSON event per line; forward to kernel bus + event log.

    Sidecar is the single source of truth for coalescing and rate caps;
    Python does not filter, drop, or batch."""
    assert proc.stdout is not None
    rotate_check_counter = 0
    while True:
        line = await proc.stdout.readline()
        if not line:
            return

        # Append to event log unconditionally (cheap tail target).
        _append_event_log(line)
        rotate_check_counter += 1
        if rotate_check_counter >= 1000:
            rotate_check_counter = 0
            _maybe_rotate_log()

        try:
            data = json.loads(line.decode("utf-8", errors="replace").strip())
        except json.JSONDecodeError as e:
            print(f"[ax-in] bad ndjson line: {e}", flush=True)
            continue

        kind = data.get("kind", "")
        if not isinstance(kind, str) or not kind.startswith("ax:"):
            print(f"[ax-in] dropping event with bad kind: {kind!r}", flush=True)
            continue

        # Wire format from the sidecar matches the events.yaml public name
        # ("ax:foo"), but the kernel re-prefixes with source — so we'd end
        # up with `ax:ax:foo` nudges. Strip the prefix before emit.
        bare_kind = kind[len("ax:"):]

        # Phase 1 firehose suppression. AX is an ambient sensor feed (every
        # keystroke fires ax:value_changed, every focus change fires
        # ax:focus_changed). With no ax-pilot persub yet to consume with
        # wake_on filters, every event falls back to the catch-all PAI and
        # wakes the LLM per keystroke. Per AX_PLAN Phase 1 exit criterion,
        # validation is via ~/.pai/var/log/ax/events.ndjson (`tail -f`), not
        # the kernel bus. We still emit operationally critical events
        # (permission_lost, secure_input_*, event_rate_capped) so the
        # kernel/owner notices grant revocation or runaway rates. Re-enable
        # the rest when Phase 3 lands a persub subscriber.
        if bare_kind not in _BUS_EMIT_KINDS:
            continue

        payload = {
            "source": "ax",
            "kind": bare_kind,
            "ts": data.get("ts"),
            "pid": data.get("pid"),
            "bundle_id": data.get("bundle_id"),
            **(data.get("payload") or {}),
        }
        # Drop None-valued top-level keys to keep events.yaml-style tidy.
        payload = {k: v for k, v in payload.items() if v is not None}
        P.emit_event(payload)


async def _supervise(axd: Path) -> None:
    backoff = 1
    max_backoff = 60
    while True:
        print(f"[ax-in] starting sidecar {axd} (backoff={backoff}s)", flush=True)
        P.append_log(SLUG, f"sidecar starting from {axd}")
        try:
            proc = await asyncio.create_subprocess_exec(
                str(axd),
                stdin=asyncio.subprocess.PIPE,  # Phase 2: RPC writes here
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PAI_ROOT": str(PAI_ROOT)},
            )
        except OSError as e:
            print(f"[ax-in] failed to spawn sidecar: {e!r}", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
            continue

        stderr_task = asyncio.create_task(_drain_stderr(proc))
        try:
            await _read_stdout(proc)
        except asyncio.CancelledError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass
            raise
        finally:
            stderr_task.cancel()

        rc = await proc.wait()
        # rc == 78 → sidecar voluntarily exited because the Accessibility
        # grant is missing. Don't crash-loop: surface, sleep long, retry.
        if rc == 78:
            print("[ax-in] sidecar exited: Accessibility grant missing. "
                  "Grant axd in System Settings → Privacy → Accessibility. "
                  "Retrying in 60s.", flush=True)
            P.append_log(SLUG, "Accessibility grant missing — retry in 60s")
            await asyncio.sleep(60)
            backoff = 1
            continue

        print(f"[ax-in] sidecar exited rc={rc}; restarting in {backoff}s", flush=True)
        P.append_log(SLUG, f"sidecar exited rc={rc}")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)


async def run() -> None:
    print(f"[ax-in] starting at {datetime.now().isoformat(timespec='seconds')}", flush=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    EVENT_LOG_DIR.mkdir(parents=True, exist_ok=True)

    axd = _resolve_axd()
    if axd is None:
        msg = (f"axd binary not found at {AXD_LIBEXEC} or {AXD_BUNDLE}. "
               f"Run: bash {PAI_ROOT}/usr/lib/drivers/ax/sidecar/build.sh")
        print(f"[ax-in] {msg}", flush=True)
        P.append_log(SLUG, msg)
        return

    P.append_log(SLUG, f"sidecar resolved at {axd}")
    try:
        await _supervise(axd)
    except asyncio.CancelledError:
        raise
    finally:
        print("[ax-in] stopped", flush=True)
