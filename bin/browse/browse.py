#!/usr/bin/env python
"""browse — thin CDP verbs against PAI's own persistent Chrome.

Chrome is treated as a daemon: kept alive on a PAI-dedicated debug port
(127.0.0.1:9333, deliberately not the conventional 9222) against PAI's own
profile under PAI_ROOT, seeded once from the owner's real profile. PAI never
attaches to the owner's everyday Chrome. Each verb opens a short-lived
WebSocket to the PAI's own tab, sends one or two CDP commands, prints a
compact result, and exits. No nested LLM loop, no Playwright, no browser_use.

Verbs:
  browse goto <url>
  browse text [--max-chars N]
  browse dom
  browse click <idx>
  browse type <idx> "<text>" [--submit]
  browse press <key>
  browse scroll [down|up|N]
  browse screenshot [path]
  browse url
  browse title
  browse wait <selector|text> [--timeout SECONDS]
  browse tabs
  browse claim <tab_id>
  browse close

Tab map at /sys/drivers/browse/tabs/<slug>.yaml. Snapshot at
/sys/drivers/browse/snapshots/<slug>.json (refreshed on goto/dom/click).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib import request as urlreq
from urllib.error import URLError
from urllib.parse import urlparse

import yaml

PAI_ROOT = Path(os.environ.get("PAI_ROOT", str(Path.home() / ".pai")))
# PAI drives its OWN Chrome on a dedicated debug port — deliberately NOT the
# conventional 9222. The owner's everyday Chrome (or an IDE, extension, or
# login item) is most likely to expose 9222; if PAI shared it, browse would
# attach to and drive the owner's real tabs instead of its isolated, seeded
# profile. A PAI-specific port lets the two coexist. _cdp_owner_is_pai() is a
# second guard for the rare case something squats this port too.
CDP_PORT = 9333
CDP_BASE = f"http://127.0.0.1:{CDP_PORT}"
TAB_DIR = PAI_ROOT / "sys" / "drivers" / "browse" / "tabs"
SNAP_DIR = PAI_ROOT / "sys" / "drivers" / "browse" / "snapshots"
# Chrome 136+ blocks --remote-debugging-port when --user-data-dir resolves
# to the user's default profile path. PAI keeps its own profile dir under
# PAI_ROOT; on first launch we seed it from the owner's real profile so
# logged-in sessions carry over. After that the two profiles drift —
# logins added in real Chrome later won't appear here automatically; re-seed
# by deleting CHROME_PROFILE.
CHROME_PROFILE = str(PAI_ROOT / "var" / "chrome" / "profile")
REAL_CHROME_PROFILE = str(Path.home() / "Library" / "Application Support" / "Google" / "Chrome")
CHROME_LAUNCH_TIMEOUT_S = 60
DEFAULT_NAV_TIMEOUT_S = 30


# ---------- HTTP helpers ----------

def _http_json(path: str, timeout: float = 2.5, method: str = "GET"):
    req = urlreq.Request(f"{CDP_BASE}{path}", method=method)
    with urlreq.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _cdp_alive() -> bool:
    try:
        _http_json("/json/version", timeout=1.5)
        return True
    except Exception:
        return False


CHROME_APP_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HOST_PROCESS_ENV = {
    **os.environ,
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
}


def _host_process_tool(*candidates: str, fallback: str) -> str:
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return fallback


LSOF_BIN = _host_process_tool("/usr/sbin/lsof", "/usr/bin/lsof", fallback="lsof")
PS_BIN = _host_process_tool("/bin/ps", "/usr/bin/ps", fallback="ps")


def _port_listener_cmdline(port: int) -> str | None:
    """Command line of the process listening on `port`, or None if unknown.

    Used to tell PAI's own Chrome apart from a foreign one squatting the debug
    port. Tries lsof for the listening PID first, then falls back to scanning
    ps for a Chrome started with the matching --remote-debugging-port flag
    (PAI's instance always carries it, so the fallback reliably finds us even
    when lsof is unavailable)."""
    pids: list[str] = []
    try:
        out = subprocess.run(
            [LSOF_BIN, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=5, env=HOST_PROCESS_ENV,
        ).stdout
        pids = [p for p in out.split() if p.isdigit()]
    except (OSError, subprocess.SubprocessError):
        pass
    for pid in pids:
        try:
            cmd = subprocess.run(
                [PS_BIN, "-p", pid, "-o", "command="],
                capture_output=True, text=True, timeout=5, env=HOST_PROCESS_ENV,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if cmd:
            return cmd
    # Fallback: lsof gave nothing usable — scan every process for the flag.
    try:
        out = subprocess.run(
            [PS_BIN, "-ax", "-o", "command="],
            capture_output=True, text=True, timeout=5, env=HOST_PROCESS_ENV,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if f"--remote-debugging-port={port}" in line:
            return line.strip()
    return None


def _cmdline_is_pai_chrome(cmdline: str) -> bool:
    """True if a Chrome command line is PAI's dedicated instance — i.e. it runs
    against CHROME_PROFILE. Pure predicate (unit-tested)."""
    return f"--user-data-dir={CHROME_PROFILE}" in cmdline


def _cdp_owner_is_pai() -> bool:
    """True iff the Chrome answering CDP on CDP_PORT is PAI's own instance.

    Guards against attaching to the owner's everyday Chrome that happens to
    expose the same debug port — which would make PAI drive the owner's real
    tabs instead of its isolated profile. If the listener can't be introspected
    we return False: refusing is safe; silently driving a foreign browser is
    the bug we're fixing."""
    cmdline = _port_listener_cmdline(CDP_PORT)
    if cmdline is None:
        return False
    return _cmdline_is_pai_chrome(cmdline)


def _seed_profile_if_needed() -> None:
    """One-time clone of the owner's real Chrome profile into CHROME_PROFILE.
    Runs only when CHROME_PROFILE doesn't exist yet. After the seed, the
    two profiles diverge — that's intentional, since PAI's Chrome runs
    independently from the owner's daily Chrome."""
    profile = Path(CHROME_PROFILE)
    if profile.is_dir():
        return
    src = Path(REAL_CHROME_PROFILE)
    if not src.is_dir():
        print(
            f"[browse] real Chrome profile not found at {src}; "
            f"starting with a fresh profile",
            file=sys.stderr,
        )
        profile.mkdir(parents=True, exist_ok=True)
        return
    print(
        f"[browse] first-run: cloning {src.name} → {profile}\n"
        f"[browse] (this preserves your logged-in sessions; can take a minute)",
        file=sys.stderr,
    )
    profile.mkdir(parents=True, exist_ok=True)
    # rsync the bits that matter: top-level Local State (cookie crypto key
    # + profile registry) and the Default subprofile (cookies, login data,
    # bookmarks, history). Skip the gigabyte-scale Cache/ subdirs.
    rsync_args = [
        "rsync", "-a",
        "--exclude=Default/Cache/",
        "--exclude=Default/Code Cache/",
        "--exclude=Default/GPUCache/",
        "--exclude=Default/Service Worker/",
        "--exclude=Default/File System/",
        "--exclude=Default/DawnGraphiteCache/",
        "--exclude=Default/DawnWebGPUCache/",
        "--exclude=Default/Application Cache/",
        "--include=Local State",
        "--include=Default/***",
        "--exclude=*",
        f"{src}/",
        f"{profile}/",
    ]
    try:
        subprocess.run(rsync_args, check=True, timeout=300)
    except subprocess.CalledProcessError as e:
        print(f"[browse] profile seed failed (rsync rc={e.returncode}); "
              f"continuing with whatever was copied", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("[browse] profile seed timed out after 5 minutes; "
              "continuing with partial copy", file=sys.stderr)


def _ensure_chrome() -> None:
    """Ensure PAI's own Chrome is up with CDP on CDP_PORT — and that whatever
    answers there is actually ours.

      - CDP alive and it's PAI's instance (CHROME_PROFILE) → reuse it.
      - CDP alive but it's a foreign Chrome (e.g. the owner's everyday browser
        squatting the port) → refuse; never drive the owner's real tabs.
      - CDP not alive → launch our dedicated-profile Chrome with CDP.

    Launch uses the Chrome binary directly (not `open -na`) because
    LaunchServices silently drops the --args when an instance is already
    registered, and `subprocess.Popen` with `start_new_session=True`
    gives us deterministic flag passing.
    """
    if _cdp_alive():
        if _cdp_owner_is_pai():
            return
        sys.exit(
            f"browse: CDP port {CDP_PORT} is held by a Chrome that is NOT "
            f"PAI's dedicated profile ({CHROME_PROFILE}) — most likely the "
            f"owner's everyday Chrome. Refusing to drive it. Quit that Chrome "
            f"(or drop its --remote-debugging-port={CDP_PORT}) and re-run."
        )
    if not Path(CHROME_APP_BIN).is_file():
        sys.exit(f"browse: Chrome not found at {CHROME_APP_BIN}")
    _seed_profile_if_needed()
    subprocess.Popen(
        [
            CHROME_APP_BIN,
            f"--remote-debugging-port={CDP_PORT}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-allow-origins=http://127.0.0.1:{CDP_PORT}",
            f"--user-data-dir={CHROME_PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + CHROME_LAUNCH_TIMEOUT_S
    while time.time() < deadline:
        if _cdp_alive():
            return
        time.sleep(0.4)
    sys.exit(
        f"browse: Chrome did not expose CDP on {CDP_BASE} within "
        f"{CHROME_LAUNCH_TIMEOUT_S}s. If Chrome is open, fully quit it "
        f"(⌘Q) and re-run the verb."
    )


# ---------- Minimal WebSocket client (stdlib only) ----------

class _WS:
    def __init__(self, url: str, timeout: float = 30.0):
        if not url.startswith("ws://"):
            raise ValueError(f"ws url must start with ws://: {url!r}")
        rest = url[5:]
        host_port, _, path = rest.partition("/")
        host, _, port_s = host_port.partition(":")
        port = int(port_s) if port_s else 80
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {host_port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise IOError("ws handshake: short read")
            buf += chunk
        head, _, leftover = buf.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            raise IOError(f"ws handshake failed: {status_line!r}")
        self._buf = leftover

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise IOError("ws: closed mid-frame")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send_text(self, data: str) -> None:
        payload = data.encode("utf-8")
        header = bytearray([0x81])  # FIN + text opcode
        ln = len(payload)
        if ln < 126:
            header.append(0x80 | ln)
        elif ln < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", ln)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", ln)
        mask = secrets.token_bytes(4)
        header += mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv_text(self) -> str:
        payload = bytearray()
        while True:
            head = self._recv_exact(2)
            b1, b2 = head[0], head[1]
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask_key = self._recv_exact(4) if masked else b""
            data = self._recv_exact(length) if length else b""
            if masked:
                data = bytes(b ^ mask_key[i & 3] for i, b in enumerate(data))
            if opcode == 0x9:  # ping
                # respond with pong (same payload)
                self._send_control(0xA, data)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                raise IOError("ws: closed by peer")
            payload += data
            if fin:
                break
        return bytes(payload).decode("utf-8", errors="replace")

    def _send_control(self, opcode: int, data: bytes) -> None:
        header = bytearray([0x80 | opcode, 0x80 | len(data)])
        mask = secrets.token_bytes(4)
        header += mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


class CDP:
    def __init__(self, ws_url: str):
        self.ws = _WS(ws_url, timeout=60.0)
        self._id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(0.5, deadline - time.time())
            self.ws.sock.settimeout(remaining)
            raw = self.ws.recv_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method}: {msg['error']}")
                return msg.get("result", {})
            # otherwise it's an event we don't care about; drop and keep waiting
        raise TimeoutError(f"CDP {method} timed out after {timeout}s")

    def close(self) -> None:
        self.ws.close()


# ---------- Tab map ----------

def _slug() -> str:
    s = os.environ.get("PAI_SLUG")
    if not s:
        sys.exit("browse: $PAI_SLUG not set")
    return s


def _tab_path(slug: str) -> Path:
    return TAB_DIR / f"{slug}.yaml"


def _snap_path(slug: str) -> Path:
    return SNAP_DIR / f"{slug}.json"


def _load_tab(slug: str) -> dict | None:
    p = _tab_path(slug)
    if not p.is_file():
        return None
    try:
        return yaml.safe_load(p.read_text()) or None
    except Exception:
        return None


def _save_tab(slug: str, data: dict) -> None:
    TAB_DIR.mkdir(parents=True, exist_ok=True)
    _tab_path(slug).write_text(yaml.safe_dump(data, sort_keys=False))


def _list_targets() -> list[dict]:
    return _http_json("/json/list")


def _find_target(tab_id: str) -> dict | None:
    for t in _list_targets():
        if t.get("id") == tab_id and t.get("type") == "page":
            return t
    return None


def _ws_for_tab(tab_id: str) -> str | None:
    t = _find_target(tab_id)
    if not t:
        return None
    return t.get("webSocketDebuggerUrl")


def _new_target() -> dict:
    # Chrome 119+ rejects GET on /json/new; PUT is required.
    return _http_json("/json/new?about:blank", method="PUT")


def _ensure_tab(slug: str) -> tuple[dict, str]:
    """Return (tab_record, ws_url). Lazily launches Chrome + creates a tab."""
    _ensure_chrome()
    tab = _load_tab(slug)
    if tab and tab.get("tab_id"):
        ws_url = _ws_for_tab(tab["tab_id"])
        if ws_url:
            return tab, ws_url
        # tab vanished — fall through to recreate
    t = _new_target()
    tab = {
        "tab_id": t["id"],
        "created": datetime.now().isoformat(timespec="seconds"),
        "owner_slug": slug,
        "owner_status": "running",
        "last_url": t.get("url", ""),
        "last_title": t.get("title", ""),
    }
    _save_tab(slug, tab)
    return tab, t["webSocketDebuggerUrl"]


def _update_tab_after_nav(tab: dict, cdp: CDP) -> None:
    """Refresh last_url/last_title from Target.getTargetInfo."""
    try:
        r = cdp.call("Target.getTargetInfo", {"targetId": tab["tab_id"]}, timeout=5.0)
        info = r.get("targetInfo", {})
        tab["last_url"] = info.get("url", tab.get("last_url", ""))
        tab["last_title"] = info.get("title", tab.get("last_title", ""))
        _save_tab(tab["owner_slug"], tab)
    except Exception:
        pass


# ---------- Snapshot helpers ----------

_DOM_TAGGER_JS = r"""
(() => {
  const sel = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="textbox"], [role="checkbox"], [role="radio"], [onclick], [contenteditable="true"]';
  const els = Array.from(document.querySelectorAll(sel));
  const out = [];
  let n = 0;
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    const idx = ++n;
    el.setAttribute('data-pai-idx', String(idx));
    let label = (el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
                 el.value || el.innerText || el.textContent || '').trim();
    label = label.replace(/\s+/g, ' ').slice(0, 140);
    out.push({
      idx,
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      role: el.getAttribute('role') || '',
      name: el.getAttribute('name') || '',
      href: el.getAttribute('href') || '',
      text: label
    });
  }
  return JSON.stringify(out);
})()
"""


def _eval_js(cdp: CDP, expr: str, await_promise: bool = False, timeout: float = 15.0) -> dict:
    return cdp.call(
        "Runtime.evaluate",
        {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": await_promise,
        },
        timeout=timeout,
    )


def _save_snapshot(slug: str, items: list[dict]) -> None:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    _snap_path(slug).write_text(json.dumps({"items": items, "saved": time.time()}))


def _drop_snapshot(slug: str) -> None:
    p = _snap_path(slug)
    if p.exists():
        p.unlink()


# ---------- Verbs ----------

def cmd_goto(args: argparse.Namespace) -> int:
    slug = _slug()
    tab, ws_url = _ensure_tab(slug)
    url = args.url
    if "://" not in url:
        url = "https://" + url
    cdp = CDP(ws_url)
    try:
        cdp.call("Page.enable", timeout=5.0)
        cdp.call("Page.navigate", {"url": url}, timeout=DEFAULT_NAV_TIMEOUT_S)
        # Wait for document.readyState === 'complete' (or interactive) up to
        # 15s. Some pages never fully load (long-polling) — interactive is
        # enough for almost all interactions.
        deadline = time.time() + 15
        while time.time() < deadline:
            r = _eval_js(cdp, "document.readyState", timeout=3.0)
            state = r.get("result", {}).get("value", "")
            if state in ("complete", "interactive"):
                break
            time.sleep(0.2)
        _drop_snapshot(slug)
        _update_tab_after_nav(tab, cdp)
        print(f"ok {tab['last_url']}")
        if tab.get("last_title"):
            print(f"   {tab['last_title']}")
    finally:
        cdp.close()
    return 0


def cmd_text(args: argparse.Namespace) -> int:
    slug = _slug()
    tab, ws_url = _ensure_tab(slug)
    cdp = CDP(ws_url)
    try:
        r = _eval_js(cdp, "document.body && document.body.innerText || ''", timeout=10.0)
        text = r.get("result", {}).get("value", "") or ""
        if args.max_chars and len(text) > args.max_chars:
            text = text[: args.max_chars] + f"\n…[truncated, {len(text) - args.max_chars} more chars]"
        print(text)
    finally:
        cdp.close()
    return 0


def cmd_dom(args: argparse.Namespace) -> int:
    slug = _slug()
    tab, ws_url = _ensure_tab(slug)
    cdp = CDP(ws_url)
    try:
        r = _eval_js(cdp, _DOM_TAGGER_JS, timeout=10.0)
        raw = r.get("result", {}).get("value", "[]") or "[]"
        items = json.loads(raw)
        _save_snapshot(slug, items)
        for it in items:
            tag = it.get("tag", "?")
            typ = it.get("type") or ""
            if typ:
                tag = f"{tag}[{typ}]"
            text = (it.get("text") or "").strip()
            if not text and it.get("href"):
                text = it["href"]
            print(f"  {it['idx']:>3}  {tag:<14} {text}")
        if not items:
            print("(no interactive elements found)")
    finally:
        cdp.close()
    return 0


def _click_expr(idx: int) -> str:
    return (
        f"(() => {{const el = document.querySelector('[data-pai-idx=\"{idx}\"]');"
        f"if (!el) return 'NOT_FOUND';"
        f"el.scrollIntoView({{block:'center'}});"
        f"el.click();"
        f"return 'OK:' + (el.tagName) + ':' + (el.getAttribute('href')||'');}})()"
    )


def cmd_click(args: argparse.Namespace) -> int:
    slug = _slug()
    tab, ws_url = _ensure_tab(slug)
    snap = _snap_path(slug)
    if not snap.exists():
        sys.exit("browse: no snapshot — run `browse dom` first")
    cdp = CDP(ws_url)
    try:
        cdp.call("Page.enable", timeout=5.0)
        r = _eval_js(cdp, _click_expr(args.idx), timeout=10.0)
        val = r.get("result", {}).get("value", "")
        if val == "NOT_FOUND":
            sys.exit(f"browse: idx {args.idx} not found in current DOM")
        # Click may have navigated; wait briefly for readyState
        time.sleep(0.3)
        for _ in range(40):
            rr = _eval_js(cdp, "document.readyState", timeout=2.0)
            if rr.get("result", {}).get("value") in ("complete", "interactive"):
                break
            time.sleep(0.1)
        _drop_snapshot(slug)
        _update_tab_after_nav(tab, cdp)
        print(f"clicked {args.idx} → {tab['last_url']}")
    finally:
        cdp.close()
    return 0


def _type_expr(idx: int, text: str) -> str:
    payload = json.dumps(text)
    return (
        f"(() => {{const el = document.querySelector('[data-pai-idx=\"{idx}\"]');"
        f"if (!el) return 'NOT_FOUND';"
        f"el.focus();"
        f"if ('value' in el) {{ el.value = {payload}; "
        f"  el.dispatchEvent(new Event('input', {{bubbles:true}})); "
        f"  el.dispatchEvent(new Event('change', {{bubbles:true}})); }}"
        f"else {{ el.textContent = {payload}; }}"
        f"return 'OK';}})()"
    )


def cmd_type(args: argparse.Namespace) -> int:
    slug = _slug()
    tab, ws_url = _ensure_tab(slug)
    cdp = CDP(ws_url)
    try:
        r = _eval_js(cdp, _type_expr(args.idx, args.text), timeout=10.0)
        val = r.get("result", {}).get("value", "")
        if val == "NOT_FOUND":
            sys.exit(f"browse: idx {args.idx} not found in current DOM")
        if args.submit:
            cdp.call("Input.dispatchKeyEvent", {
                "type": "keyDown", "key": "Enter", "code": "Enter",
                "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
            }, timeout=5.0)
            cdp.call("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": "Enter", "code": "Enter",
                "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
            }, timeout=5.0)
            time.sleep(0.4)
            _update_tab_after_nav(tab, cdp)
        print(f"typed into {args.idx}")
    finally:
        cdp.close()
    return 0


_KEYMAP = {
    "enter":     ("Enter",     13),
    "return":    ("Enter",     13),
    "tab":       ("Tab",        9),
    "escape":    ("Escape",    27),
    "esc":       ("Escape",    27),
    "backspace": ("Backspace",  8),
    "delete":    ("Delete",    46),
    "arrowdown": ("ArrowDown", 40),
    "arrowup":   ("ArrowUp",   38),
    "arrowleft": ("ArrowLeft", 37),
    "arrowright":("ArrowRight",39),
    "down":      ("ArrowDown", 40),
    "up":        ("ArrowUp",   38),
    "left":      ("ArrowLeft", 37),
    "right":     ("ArrowRight",39),
    "pagedown":  ("PageDown",  34),
    "pageup":    ("PageUp",    33),
    "home":      ("Home",      36),
    "end":       ("End",       35),
    "space":     (" ",         32),
}


def cmd_press(args: argparse.Namespace) -> int:
    slug = _slug()
    tab, ws_url = _ensure_tab(slug)
    key = args.key.lower()
    if key not in _KEYMAP:
        sys.exit(f"browse: unknown key {args.key!r} (known: {', '.join(sorted(_KEYMAP))})")
    code, vk = _KEYMAP[key]
    cdp = CDP(ws_url)
    try:
        params = {"key": code, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
        cdp.call("Input.dispatchKeyEvent", {"type": "keyDown", **params}, timeout=5.0)
        cdp.call("Input.dispatchKeyEvent", {"type": "keyUp", **params}, timeout=5.0)
        time.sleep(0.2)
        _update_tab_after_nav(tab, cdp)
        print(f"pressed {code}")
    finally:
        cdp.close()
    return 0


def cmd_scroll(args: argparse.Namespace) -> int:
    slug = _slug()
    tab, ws_url = _ensure_tab(slug)
    direction = args.direction or "down"
    try:
        amount = int(direction)
    except ValueError:
        amount = 800 if direction == "down" else -800 if direction == "up" else 800
    cdp = CDP(ws_url)
    try:
        _eval_js(cdp, f"window.scrollBy(0, {amount}); window.scrollY", timeout=5.0)
        print(f"scrolled {amount}")
    finally:
        cdp.close()
    return 0


def cmd_screenshot(args: argparse.Namespace) -> int:
    slug = _slug()
    tab, ws_url = _ensure_tab(slug)
    out = Path(args.path) if args.path else (
        PAI_ROOT / "proc" / slug / "screenshot.png"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    cdp = CDP(ws_url)
    try:
        cdp.call("Page.enable", timeout=5.0)
        r = cdp.call("Page.captureScreenshot", {"format": "png"}, timeout=20.0)
        data = base64.b64decode(r.get("data", ""))
        out.write_bytes(data)
        print(f"![viewport]({out}) (saved, {len(data)} bytes)")
    finally:
        cdp.close()
    return 0


def cmd_url(args: argparse.Namespace) -> int:
    slug = _slug()
    tab, ws_url = _ensure_tab(slug)
    cdp = CDP(ws_url)
    try:
        _update_tab_after_nav(tab, cdp)
        print(tab.get("last_url", ""))
    finally:
        cdp.close()
    return 0


def cmd_title(args: argparse.Namespace) -> int:
    slug = _slug()
    tab, ws_url = _ensure_tab(slug)
    cdp = CDP(ws_url)
    try:
        _update_tab_after_nav(tab, cdp)
        print(tab.get("last_title", ""))
    finally:
        cdp.close()
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    slug = _slug()
    tab, ws_url = _ensure_tab(slug)
    target = args.what
    timeout = args.timeout
    is_selector = target.startswith(("#", ".", "[")) or any(
        c in target for c in (">", " ", "(", ":")
    )
    cdp = CDP(ws_url)
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if is_selector:
                expr = f"document.querySelector({json.dumps(target)}) !== null"
            else:
                expr = (
                    f"(document.body && document.body.innerText || '')"
                    f".indexOf({json.dumps(target)}) >= 0"
                )
            r = _eval_js(cdp, expr, timeout=3.0)
            if r.get("result", {}).get("value") is True:
                print(f"found {target!r}")
                return 0
            time.sleep(0.3)
        sys.exit(f"browse: wait timed out after {timeout}s for {target!r}")
    finally:
        cdp.close()


def _human_age(iso: str) -> str:
    try:
        t = datetime.fromisoformat(iso)
    except Exception:
        return "?"
    delta = (datetime.now() - t).total_seconds()
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def cmd_tabs(args: argparse.Namespace) -> int:
    slug = _slug()
    _ensure_chrome()
    # Build map: tab_id -> live target
    live = {t["id"]: t for t in _list_targets() if t.get("type") == "page"}
    rows: list[tuple[str, str, str, str]] = []  # (status, tab_id, title, age)
    if not TAB_DIR.exists():
        TAB_DIR.mkdir(parents=True, exist_ok=True)
    for tf in sorted(TAB_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(tf.read_text()) or {}
        except Exception:
            continue
        tid = data.get("tab_id")
        if not tid or tid not in live:
            continue
        status = data.get("owner_status", "?")
        title = data.get("last_title") or live[tid].get("title", "") or "(untitled)"
        age = _human_age(data.get("created", ""))
        if data.get("owner_slug") == slug:
            tag = "mine"
        elif status == "orphan":
            tag = "orphan"
        else:
            tag = f"owned by {data.get('owner_slug', '?')}"
        rows.append((tag, str(tid), title, age))
    if not rows:
        print("(no tabs)")
        return 0
    for tag, tid, title, age in rows:
        print(f"  {tid}  [{tag}]  {title}  ({age})")
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    slug = _slug()
    _ensure_chrome()
    target_id = args.tab_id
    # Find which yaml owns this tab_id (if any)
    source_file: Path | None = None
    source_data: dict | None = None
    for tf in TAB_DIR.glob("*.yaml"):
        try:
            d = yaml.safe_load(tf.read_text()) or {}
        except Exception:
            continue
        if d.get("tab_id") == target_id:
            source_file = tf
            source_data = d
            break
    # If not in our map but live in Chrome, still allow claim
    if not _find_target(target_id):
        sys.exit(f"browse: tab {target_id!r} not found in Chrome")
    if source_data and source_data.get("owner_slug") != slug \
            and source_data.get("owner_status") not in (None, "orphan"):
        sys.exit(
            f"browse: tab {target_id!r} is owned by "
            f"{source_data.get('owner_slug')!r} (status={source_data.get('owner_status')!r}); "
            "refuse to steal a live tab"
        )
    live = _find_target(target_id) or {}
    new = {
        "tab_id": target_id,
        "created": (source_data or {}).get("created")
                   or datetime.now().isoformat(timespec="seconds"),
        "owner_slug": slug,
        "owner_status": "running",
        "last_url": live.get("url", (source_data or {}).get("last_url", "")),
        "last_title": live.get("title", (source_data or {}).get("last_title", "")),
    }
    # If we currently own a different tab, that becomes orphan-eligible
    # but per design we just overwrite our own tab file: the previous
    # tab keeps living in Chrome until the owner closes it manually.
    _save_tab(slug, new)
    if source_file and source_file != _tab_path(slug):
        try:
            source_file.unlink()
        except OSError:
            pass
    print(f"claimed {target_id} → {new['last_url']}")
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    slug = _slug()
    tab = _load_tab(slug)
    if not tab:
        print("(no tab to close)")
        return 0
    target_id = tab.get("tab_id")
    if target_id and _find_target(target_id):
        try:
            _http_json(f"/json/close/{target_id}")
        except (URLError, json.JSONDecodeError):
            pass
    try:
        _tab_path(slug).unlink()
    except OSError:
        pass
    _drop_snapshot(slug)
    print(f"closed {target_id or '(unknown)'}")
    return 0


# ---------- argparse ----------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="browse",
        description="Thin CDP verbs against the owner's persistent Chrome.",
    )
    sub = p.add_subparsers(dest="verb", required=True)

    sg = sub.add_parser("goto", help="navigate the tab to URL")
    sg.add_argument("url")
    sg.set_defaults(func=cmd_goto)

    st = sub.add_parser("text", help="print current page innerText")
    st.add_argument("--max-chars", type=int, default=8000)
    st.set_defaults(func=cmd_text)

    sd = sub.add_parser("dom", help="snapshot + print numbered interactive elements")
    sd.set_defaults(func=cmd_dom)

    sc = sub.add_parser("click", help="click element by snapshot index")
    sc.add_argument("idx", type=int)
    sc.set_defaults(func=cmd_click)

    sy = sub.add_parser("type", help="type text into element by snapshot index")
    sy.add_argument("idx", type=int)
    sy.add_argument("text")
    sy.add_argument("--submit", action="store_true", help="press Enter after typing")
    sy.set_defaults(func=cmd_type)

    sp = sub.add_parser("press", help="dispatch a key (enter/tab/escape/...)")
    sp.add_argument("key")
    sp.set_defaults(func=cmd_press)

    ss = sub.add_parser("scroll", help="scroll the page")
    ss.add_argument("direction", nargs="?", default="down",
                    help="'down' | 'up' | integer pixels")
    ss.set_defaults(func=cmd_scroll)

    sh = sub.add_parser("screenshot", help="save a PNG of the current viewport")
    sh.add_argument("path", nargs="?", default=None)
    sh.set_defaults(func=cmd_screenshot)

    su = sub.add_parser("url", help="print current url")
    su.set_defaults(func=cmd_url)

    si = sub.add_parser("title", help="print current title")
    si.set_defaults(func=cmd_title)

    sw = sub.add_parser("wait", help="wait until selector or text appears")
    sw.add_argument("what", help="CSS selector or substring of page text")
    sw.add_argument("--timeout", type=float, default=15.0)
    sw.set_defaults(func=cmd_wait)

    sb = sub.add_parser("tabs", help="list my tab + claimable orphan tabs")
    sb.set_defaults(func=cmd_tabs)

    sa = sub.add_parser("claim", help="take ownership of an orphan tab")
    sa.add_argument("tab_id")
    sa.set_defaults(func=cmd_claim)

    sx = sub.add_parser("close", help="close my tab in Chrome")
    sx.set_defaults(func=cmd_close)

    args = p.parse_args(argv)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
