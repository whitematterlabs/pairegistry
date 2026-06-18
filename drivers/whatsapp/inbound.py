"""WhatsApp inbound driver.

Supervises a Node.js Baileys bridge as a subprocess. The bridge speaks
JSON-per-line over stdout. Auth state persists under
/Users/arda/.pai/sys/drivers/whatsapp/auth/ — first run emits a QR code for pairing,
subsequent runs reconnect with saved creds.

This driver is receive-only: it records inbound messages and emits
events. It has no send path.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from boot import processes as P
from boot import paths

# ── paths ──────────────────────────────────────────────────────────
PAI_ROOT = Path(os.environ.get("PAI_ROOT", str(Path.home() / ".pai")))
BRIDGE_JS = PAI_ROOT / "usr" / "libexec" / "whatsapp" / "bridge.js"
MESSAGES_ROOT = paths.var_spool_communication() / "whatsapp"
PEOPLE_ROOT = paths.var_lib_memory() / "people"
AUTH_DIR = PAI_ROOT / "sys" / "drivers" / "whatsapp" / "auth"


# ── LID → phone resolution ─────────────────────────────────────────
# Baileys persists every LID↔phone mapping it sees into the auth dir:
#   lid-mapping-<phone>.json          → "<lid>"
#   lid-mapping-<lid>_reverse.json    → "<phone>"
# WhatsApp now delivers some inbound JIDs with an LID-shaped local part
# even under @s.whatsapp.net, so we can't rely on the bridge's @lid-suffix
# check alone. A real E.164 phone is ≤15 digits; LIDs are ~14-17 digits but
# don't correspond to any real country code. Heuristic: if the local part
# is all digits and we have a reverse-mapping file for it, treat it as a
# LID and substitute the phone.
def _resolve_lid_to_phone(local: str) -> str:
    if not local.isdigit():
        return local
    reverse = AUTH_DIR / f"lid-mapping-{local}_reverse.json"
    if not reverse.exists():
        return local
    try:
        phone = json.loads(reverse.read_text()).strip()
    except (OSError, json.JSONDecodeError, AttributeError):
        return local
    return phone if phone.isdigit() else local


# ── phone number slug detection ────────────────────────────────────
def _is_phone_slug(slug: str) -> bool:
    cleaned = slug.lstrip("+")
    return cleaned.isdigit() and len(cleaned) >= 7


# ── contact resolution ─────────────────────────────────────────────
def _lookup_contact(phone: str) -> tuple[str, str | None]:
    if not PEOPLE_ROOT.exists():
        return (phone, None)

    for entry in PEOPLE_ROOT.iterdir():
        if not entry.is_dir():
            continue
        about = entry / "about.yaml"
        if not about.exists():
            continue
        try:
            import yaml
            data = yaml.safe_load(about.read_text()) or {}
        except Exception:
            continue
        handles = data.get("handles", [])
        for h in handles:
            if isinstance(h, str) and h.lstrip("+") == phone.lstrip("+"):
                return (entry.name, data.get("name") or entry.name)
    return (phone, None)


def _ensure_thread_dir(thread_slug: str) -> Path:
    thread_dir = MESSAGES_ROOT / thread_slug
    thread_dir.mkdir(parents=True, exist_ok=True)
    meta = thread_dir / "meta.yaml"
    if not meta.exists():
        import yaml
        meta.write_text(yaml.dump({"channel": "whatsapp"}))
    return thread_dir


def _write_message(thread_dir: Path, sender: str, text: str, ts_iso: str | None = None) -> Path:
    when = datetime.now()
    if ts_iso:
        try:
            parsed = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
            when = parsed.astimezone().replace(tzinfo=None)
        except ValueError:
            pass
    day_file = thread_dir / f"{when.strftime('%Y-%m-%d')}.md"
    hm = when.strftime("%H:%M")
    # Prefix every line: day-files invariant is "every line starts with
    # [HH:MM] sender:", so multi-line messages stay parseable as a log.
    prefix = f"[{hm}] {sender}: "
    body = "".join(prefix + ln + "\n" for ln in text.splitlines() or [""])
    with day_file.open("a") as f:
        f.write(body)
    return day_file


# ── bridge supervision ─────────────────────────────────────────────
def _reap_stale_bridges() -> None:
    # The wwebjs-session user-data-dir is an exclusive lock. If a prior
    # driver lifetime leaked its bridge or Chromium (kernel crash, orphan
    # after reload, stranded process from a previous boot), the new bridge
    # can't acquire the lock and stalls silently with no log output.
    session_dir = PAI_ROOT / "sys" / "drivers" / "whatsapp" / "wwebjs-session"
    patterns = (
        r"node.*whatsapp.*bridge\.js",
        f"--user-data-dir={session_dir}",
    )
    me = os.getpid()
    pids: set[int] = set()
    for pat in patterns:
        try:
            out = subprocess.run(
                ["pgrep", "-f", pat], capture_output=True, text=True, timeout=2
            )
        except Exception:
            continue
        for line in out.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                pid = int(line)
                if pid != me:
                    pids.add(pid)
    if not pids:
        return
    print(
        f"[whatsapp-in] reaping {len(pids)} stale bridge/chromium pids: {sorted(pids)}",
        flush=True,
    )
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as e:
            print(f"[whatsapp-in] reap SIGTERM {pid}: {e!r}", flush=True)
    time.sleep(1)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as e:
            print(f"[whatsapp-in] reap SIGKILL {pid}: {e!r}", flush=True)


async def _run_bridge() -> None:
    backoff = 1
    max_backoff = 60

    _reap_stale_bridges()

    while True:
        if not BRIDGE_JS.exists():
            print(f"[whatsapp-in] bridge.js not found at {BRIDGE_JS}; driver idle", flush=True)
            return
        node_bin = shutil.which("node")
        if not node_bin:
            print("[whatsapp-in] node binary not found on PATH; driver idle", flush=True)
            return

        print(f"[whatsapp-in] starting bridge (backoff={backoff}s)", flush=True)
        proc = await asyncio.create_subprocess_exec(
            node_bin, str(BRIDGE_JS),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PAI_ROOT": str(PAI_ROOT)},
        )

        try:
            await _read_bridge_stdout(proc)
        except asyncio.CancelledError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
            raise
        except Exception as e:
            print(f"[whatsapp-in] bridge read error: {e!r}", flush=True)

        print(f"[whatsapp-in] bridge exited (rc={proc.returncode}), restarting in {backoff}s", flush=True)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)


HISTORY_QUIESCE_SECONDS = 3.0


async def _read_bridge_stdout(proc: asyncio.subprocess.Process) -> None:
    assert proc.stdout is not None

    backlog_messages: list[dict] = []
    backlog_flush_task: asyncio.Task | None = None

    async def _flush_after_quiesce():
        try:
            await asyncio.sleep(HISTORY_QUIESCE_SECONDS)
        except asyncio.CancelledError:
            return
        if backlog_messages:
            _emit_backlog(list(backlog_messages))
            backlog_messages.clear()

    while True:
        line = await proc.stdout.readline()
        if not line:
            break

        try:
            data = json.loads(line.decode("utf-8").strip())
        except json.JSONDecodeError:
            continue

        msg_type = data.get("type", "")

        if msg_type == "message" and data.get("direction") == "in":
            phone = data.get("from", "")
            body = data.get("body", "")
            if not phone or not body:
                continue
            # Strip Baileys device suffix ("<phone>:<device>") — handles in
            # people/<slug>/about.yaml store the bare phone number.
            phone = phone.split(":", 1)[0]
            phone = _resolve_lid_to_phone(phone)

            slug, display_name = _lookup_contact(phone)
            sender = display_name or phone
            thread_dir = _ensure_thread_dir(slug)
            day_file = _write_message(thread_dir, sender, body, data.get("timestamp"))

            if data.get("history"):
                backlog_messages.append({
                    "phone": phone,
                    "slug": slug,
                    "sender": sender,
                    "text": body,
                })
                if backlog_flush_task and not backlog_flush_task.done():
                    backlog_flush_task.cancel()
                backlog_flush_task = asyncio.create_task(_flush_after_quiesce())
                continue

            P.emit_event({
                "source": "whatsapp",
                "kind": "new",
                "thread": slug,
                "sender": sender,
                "text": body,
                "day_file": str(day_file.relative_to(PAI_ROOT)),
            })
            print(f"[whatsapp-in] emitted message from {sender} → {slug}", flush=True)

        elif msg_type == "qr":
            # Pairing is a one-time interactive step. Runtime driver should
            # never see this — if it does, the bridge has lost its session.
            print(
                "[whatsapp-in] bridge requested QR pairing — session is missing "
                "or expired. Stop this driver and run `whatsapp-pair`.",
                flush=True,
            )

        elif msg_type == "status":
            state = data.get("state", "unknown")
            reason = data.get("reason")
            should_reconnect = data.get("shouldReconnect")
            extra = ""
            if reason is not None or should_reconnect is not None:
                extra = f" (reason={reason}, shouldReconnect={should_reconnect})"
            print(f"[whatsapp-in] bridge status: {state}{extra}", flush=True)

        elif msg_type == "error":
            err = data.get("error", "unknown")
            print(f"[whatsapp-in] bridge error: {err}", flush=True)

    # EOF safety net: flush any pending backlog before returning.
    if backlog_flush_task and not backlog_flush_task.done():
        backlog_flush_task.cancel()
    if backlog_messages:
        _emit_backlog(list(backlog_messages))
        backlog_messages.clear()


def _emit_backlog(messages: list[dict]) -> None:
    threads_map: dict[str, dict] = {}
    for m in messages:
        t = threads_map.setdefault(m["slug"], {
            "thread": m["slug"],
            "inbound": 0,
            "outbound": 0,
        })
        t["inbound"] += 1
        t["last_text"] = m["text"]

    P.emit_event({
        "source": "whatsapp",
        "kind": "backlog",
        "since": datetime.now(timezone.utc).isoformat(),
        "threads": list(threads_map.values()),
        "total": len(messages),
    })
    print(f"[whatsapp-in] emitted backlog ({len(messages)} messages across {len(threads_map)} threads)", flush=True)


async def run() -> None:
    print("[whatsapp-in] starting", flush=True)

    try:
        await _run_bridge()
    except asyncio.CancelledError:
        raise
    finally:
        print("[whatsapp-in] stopped", flush=True)
