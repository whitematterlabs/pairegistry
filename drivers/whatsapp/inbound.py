"""WhatsApp inbound driver.

Supervises a Node.js Baileys bridge as a subprocess. The bridge speaks
JSON-per-line over stdout/stdin. Auth state persists under
/Users/arda/.pai/sys/drivers/whatsapp/auth/ — first run emits a QR code for pairing,
subsequent runs reconnect with saved creds.

Also watches the outbox directory for outbound send requests and
forwards them to the bridge's stdin.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from boot import processes as P
from boot import paths

# ── paths ──────────────────────────────────────────────────────────
PAI_ROOT = Path(os.environ.get("PAI_ROOT", str(Path.home() / ".pai")))
BRIDGE_JS = PAI_ROOT / "usr" / "libexec" / "whatsapp" / "bridge.js"
MESSAGES_ROOT = paths.var_spool_communication() / "whatsapp"
PEOPLE_ROOT = paths.var_lib_memory() / "people"
OUTBOX_DIR = PAI_ROOT / "sys" / "drivers" / "whatsapp" / "outbox"

# ── bridge ref (set by _run_bridge, read by outbox handler) ────────
_bridge_stdin: Optional[asyncio.StreamWriter] = None


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
            if isinstance(h, str) and h == phone:
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


def _write_message(thread_dir: Path, sender: str, text: str) -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    day_file = thread_dir / f"{today}.md"
    hm = datetime.now().strftime("%H:%M")
    line = f"[{hm}] {sender}: {text}\n"
    with day_file.open("a") as f:
        f.write(line)
    return day_file


# ── outbox watcher ─────────────────────────────────────────────────
class _OutboxHandler(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        self.loop = loop
        self.queue = queue

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".json"):
            self.loop.call_soon_threadsafe(self.queue.put_nowait, Path(event.src_path))


async def _drain_outbox(queue: asyncio.Queue) -> None:
    """Read outbox JSON files and forward them to the bridge's stdin."""
    while True:
        path = await queue.get()
        try:
            if not path.exists():
                continue
            payload = path.read_text()
            data = json.loads(payload)
            if _bridge_stdin is not None:
                _bridge_stdin.write((json.dumps(data) + "\n").encode())
                await _bridge_stdin.drain()
                print(f"[whatsapp-in] forwarded outbox → bridge: {data.get('to')}", flush=True)
            else:
                print("[whatsapp-in] outbox entry but no bridge stdin yet", flush=True)
        except Exception as e:
            print(f"[whatsapp-in] outbox drain error: {e!r}", flush=True)
        finally:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


# ── bridge supervision ─────────────────────────────────────────────
async def _run_bridge() -> None:
    global _bridge_stdin

    backoff = 1
    max_backoff = 60

    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)

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
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PAI_ROOT": str(PAI_ROOT)},
        )
        _bridge_stdin = proc.stdin

        # Drain any outbox files that accumulated while bridge was down
        await _drain_pending_outbox()

        try:
            await _read_bridge_stdout(proc)
        except asyncio.CancelledError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
            _bridge_stdin = None
            raise
        except Exception as e:
            print(f"[whatsapp-in] bridge read error: {e!r}", flush=True)

        _bridge_stdin = None
        print(f"[whatsapp-in] bridge exited (rc={proc.returncode}), restarting in {backoff}s", flush=True)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)


async def _drain_pending_outbox() -> None:
    """Forward any outbox files that were written while the bridge was down."""
    if not OUTBOX_DIR.exists():
        return
    for f in sorted(OUTBOX_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            if _bridge_stdin is not None:
                _bridge_stdin.write((json.dumps(data) + "\n").encode())
                await _bridge_stdin.drain()
            f.unlink()
        except Exception as e:
            print(f"[whatsapp-in] pending outbox error: {e!r}", flush=True)


async def _read_bridge_stdout(proc: asyncio.subprocess.Process) -> None:
    assert proc.stdout is not None

    backlog_messages: list[dict] = []
    backlog_deadline = time.time() + 5
    backlog_emitted = False

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

            slug, display_name = _lookup_contact(phone)
            sender = display_name or phone
            thread_dir = _ensure_thread_dir(slug)
            day_file = _write_message(thread_dir, sender, body)

            if not backlog_emitted and time.time() < backlog_deadline:
                backlog_messages.append({
                    "phone": phone,
                    "slug": slug,
                    "sender": sender,
                    "text": body,
                })
                continue

            P.emit_event({
                "source": "whatsapp",
                "kind": "new_message",
                "thread": slug,
                "sender": sender,
                "text": body,
                "day_file": str(day_file.relative_to(PAI_ROOT)),
            })
            print(f"[whatsapp-in] emitted message from {sender} → {slug}", flush=True)

        elif msg_type == "qr":
            qr_str = data.get("qr", "")
            if qr_str:
                P.emit_event({
                    "source": "whatsapp",
                    "kind": "qr_ready",
                    "qr": qr_str,
                })
                print("[whatsapp-in] emitted qr_ready event", flush=True)

        elif msg_type == "status":
            state = data.get("state", "unknown")
            print(f"[whatsapp-in] bridge status: {state}", flush=True)

        elif msg_type == "error":
            err = data.get("error", "unknown")
            print(f"[whatsapp-in] bridge error: {err}", flush=True)
            # If the error is a send failure, emit send_failed
            cmd = data.get("command")
            if cmd and cmd.get("direction") == "out":
                P.emit_event({
                    "source": "whatsapp",
                    "kind": "send_failed",
                    "thread": cmd.get("thread", "unknown"),
                    "text": cmd.get("body", ""),
                    "reason": err,
                })

    # Emit backlog if any accumulated
    if backlog_messages and not backlog_emitted:
        _emit_backlog(backlog_messages)
        backlog_emitted = True


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
        "kind": "messages_backlog",
        "since": datetime.now(timezone.utc).isoformat(),
        "threads": list(threads_map.values()),
        "total": len(messages),
    })
    print(f"[whatsapp-in] emitted backlog ({len(messages)} messages across {len(threads_map)} threads)", flush=True)


async def run() -> None:
    print("[whatsapp-in] starting", flush=True)

    # Start outbox watcher
    loop = asyncio.get_running_loop()
    outbox_queue: asyncio.Queue = asyncio.Queue()
    observer = Observer()
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    observer.schedule(_OutboxHandler(loop, outbox_queue), str(OUTBOX_DIR), recursive=False)
    observer.start()

    outbox_task = asyncio.create_task(_drain_outbox(outbox_queue))

    try:
        await _run_bridge()
    except asyncio.CancelledError:
        raise
    finally:
        outbox_task.cancel()
        try:
            await outbox_task
        except asyncio.CancelledError:
            pass
        observer.stop()
        observer.join(timeout=2)
        print("[whatsapp-in] stopped", flush=True)
