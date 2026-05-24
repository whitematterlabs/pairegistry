"""AX inbound driver — supervises the Swift sidecar `axd`.

The sidecar owns all session state, the RPC server (Unix socket), and
target_pid stamping. This Python supervisor only:

  - resolves the axd binary,
  - spawns and restarts it (with rc=78 = Accessibility-grant-missing
    handled as a long-sleep retry, not a crash loop),
  - parses NDJSON events off its stdout,
  - forwards public async/failure events via P.emit_event(payload, target_pid=...).

The sidecar has flipped from ambient sensor to piloting surface — PAIs
talk to it via the `ax` bin tool (Unix socket JSON-RPC); only PAIs that
attached a session receive events for that session. No firehose, no
client-side filtering, no event log.

Requires the Accessibility TCC grant for `axd` (System Settings → Privacy
& Security → Accessibility).
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from boot import processes as P

PAI_ROOT = Path(os.environ.get("PAI_ROOT", str(Path.home() / ".pai")))

# Two candidate binary locations: the post-install staged copy (preferred)
# and the bundle-local dev build.
AXD_LIBEXEC = PAI_ROOT / "usr" / "libexec" / "ax" / "axd"
AXD_BUNDLE = PAI_ROOT / "usr" / "lib" / "drivers" / "ax" / "sidecar" / ".build" / "release" / "axd"

STATE_DIR = PAI_ROOT / "sys" / "drivers" / "ax"
SIDECAR_LOG = STATE_DIR / "sidecar.stderr.log"

# Socket lives under /var/run/ax/. The sidecar creates it on start; the
# `ax` bin tool reads $PAI_ROOT/var/run/ax/axd.sock to connect.
RUN_DIR = PAI_ROOT / "var" / "run" / "ax"

SLUG = "ax-in"


def _public_event_from_sidecar(data: dict) -> tuple[dict, int] | None:
    """Return the kernel-bus event for a sidecar event, if it should wake PAI.

    The `ax` CLI already returns synchronous attach/act/detach confirmations
    inline to the caller. Keep those private to the driver so completed
    one-shot tasks do not get replayed as follow-up nudges. Asynchronous state
    changes and failures still cross the public event bus.
    """
    kind = data.get("kind", "")
    if not isinstance(kind, str) or not kind.startswith("ax:"):
        return None

    target_pid = data.get("target_pid")
    if not isinstance(target_pid, int):
        return None

    bare_kind = kind[len("ax:"):]
    if bare_kind == "scope_attached":
        return None
    if bare_kind == "action_result" and data.get("ok") is True:
        return None
    if bare_kind == "scope_lost" and data.get("reason") == "detached":
        return None

    payload: dict = {"source": "ax", "kind": bare_kind}
    for k, v in data.items():
        if k in ("kind", "target_pid"):
            continue
        if v is None:
            continue
        payload[k] = v

    return payload, target_pid


def _resolve_axd() -> Optional[Path]:
    for cand in (AXD_LIBEXEC, AXD_BUNDLE):
        if cand.exists() and os.access(cand, os.X_OK):
            return cand
    return None


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
    """Parse one NDJSON event per line; forward public events to the bus.

    Each line is a full event payload from the sidecar. The sidecar stamps
    `kind: "ax:foo"`, `target_pid`, and any payload fields. Public events are
    re-packed into the kernel's event shape (source/kind separated) and emitted
    with the explicit target_pid so the router delivers point-to-point instead
    of fanning out by wake_on."""
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            return

        try:
            data = json.loads(line.decode("utf-8", errors="replace").strip())
        except json.JSONDecodeError as e:
            print(f"[ax-in] bad ndjson line: {e}", flush=True)
            continue

        public = _public_event_from_sidecar(data)
        if public is None:
            continue

        payload, target_pid = public
        P.emit_event(payload, target_pid=target_pid)


async def _supervise(axd: Path) -> None:
    backoff = 1
    max_backoff = 60
    while True:
        print(f"[ax-in] starting sidecar {axd} (backoff={backoff}s)", flush=True)
        P.append_log(SLUG, f"sidecar starting from {axd}")
        try:
            proc = await asyncio.create_subprocess_exec(
                str(axd),
                stdin=asyncio.subprocess.DEVNULL,
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
    RUN_DIR.mkdir(parents=True, exist_ok=True)

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
