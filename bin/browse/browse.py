#!/usr/bin/env python
"""browse — Playwright verbs against PAI's own persistent Chrome.

The verb surface is unchanged, but the engine is a lazily-spawned Node daemon
(usr/libexec/browse/server.mjs) that owns one Playwright `launchPersistentContext`
session against PAI's dedicated Chrome profile (channel:'chrome' → the real
Google Chrome, no Chromium download) and a Page per PAI slug. PAI invokes
`browse` as one-shot CLI verbs; this file is a thin client that connects to the
daemon's unix socket, sends one JSON request, prints a compact result, and
exits. The daemon gives every verb real keyboard/mouse input (the fix for
controlled React widgets that revert synthetic value-sets), auto-waiting,
frames, and dialogs — none of which the old hand-rolled CDP client had.

First verb of a session spawns the daemon (detached) exactly as Chrome was
launched lazily before; it stays up like Chrome does today.

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
  browse eval "<js>" [--await]
  browse tabs
  browse close
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

PAI_ROOT = Path(os.environ.get("PAI_ROOT", str(Path.home() / ".pai")))
STATE_DIR = PAI_ROOT / "var" / "lib" / "browse"
SOCK_PATH = STATE_DIR / "browse.sock"
SERVER_MJS = PAI_ROOT / "usr" / "libexec" / "browse" / "server.mjs"
SPAWN_LOG = PAI_ROOT / "var" / "log" / "browse-daemon-spawn.log"

# Generous: covers the one-time profile seed (rsync of the owner's Chrome
# profile, up to a minute) that the daemon performs on the first verb. Routine
# verbs return in well under a second.
REQUEST_TIMEOUT_S = 300.0
# The daemon binds its socket before launching the browser, so it answers a
# connect within a couple of seconds of node start (node + playwright import).
DAEMON_UP_TIMEOUT_S = 30.0


# ---------- slug ----------

def _slug() -> str:
    s = os.environ.get("PAI_SLUG")
    if not s:
        sys.exit("browse: $PAI_SLUG not set")
    return s


# ---------- node + daemon lifecycle ----------

def _find_node() -> str:
    """Locate a `node` binary. Prefer PATH (respects a subagent's inherited
    env), then common install dirs, then the latest nvm-managed node — the bin
    can run under a sanitized PATH that misses nvm. Clear error if none."""
    found = shutil.which("node")
    if found:
        return found
    candidates = ["/opt/homebrew/bin/node", "/usr/local/bin/node"]
    nvm = Path.home() / ".nvm" / "versions" / "node"
    if nvm.is_dir():
        versions = sorted(nvm.glob("*/bin/node"))
        if versions:
            candidates.append(str(versions[-1]))
    for c in candidates:
        if Path(c).exists():
            return c
    sys.exit(
        "browse: node not found on PATH. browse drives Chrome through a "
        "Playwright sidecar that needs system Node. Install it (e.g. "
        "'brew install node') and re-run."
    )


def _daemon_alive() -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.5)
        s.connect(str(SOCK_PATH))
        s.close()
        return True
    except OSError:
        return False


def _ensure_daemon() -> None:
    """Connect to the daemon; spawn it (detached) if dead. Mirrors the old lazy
    Chrome launch — pay Node startup once, then verbs are instant."""
    if _daemon_alive():
        return
    if not SERVER_MJS.is_file():
        sys.exit(
            f"browse: daemon not installed at {SERVER_MJS}. Run "
            f"`paiman install bin/browse` to stage the Playwright sidecar."
        )
    node = _find_node()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SPAWN_LOG.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PAI_ROOT": str(PAI_ROOT)}
    logf = open(SPAWN_LOG, "ab")
    try:
        subprocess.Popen(
            [node, str(SERVER_MJS)],
            stdout=logf,
            stderr=logf,
            start_new_session=True,
            env=env,
        )
    finally:
        logf.close()
    deadline = time.time() + DAEMON_UP_TIMEOUT_S
    while time.time() < deadline:
        if _daemon_alive():
            return
        time.sleep(0.3)
    sys.exit(
        f"browse: daemon did not come up within {DAEMON_UP_TIMEOUT_S:.0f}s. "
        f"See {SPAWN_LOG} and {PAI_ROOT / 'var' / 'log' / 'browse-daemon.log'}."
    )


def _request(verb: str, **args) -> dict:
    """Send one verb to the daemon and return its decoded reply. Raises
    SystemExit on a daemon-level error (ok:false)."""
    _ensure_daemon()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(REQUEST_TIMEOUT_S)
    try:
        s.connect(str(SOCK_PATH))
        payload = json.dumps({"slug": _slug(), "verb": verb, "args": args})
        s.sendall(payload.encode("utf-8") + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    except (OSError, socket.timeout) as e:
        sys.exit(f"browse: daemon request failed: {e}")
    finally:
        s.close()
    line = buf.split(b"\n", 1)[0]
    try:
        reply = json.loads(line)
    except json.JSONDecodeError:
        sys.exit(f"browse: malformed daemon reply: {line[:200]!r}")
    if not reply.get("ok", False):
        sys.exit(f"browse: {reply.get('error', 'unknown daemon error')}")
    return reply


def _print_new_lines(new_lines: list, limit: int = 12) -> None:
    """Surface visible text that appeared after an in-page action — validation
    errors, next-step prompts, toasts. The daemon computed the innerText diff;
    here we just render it with a display cap. Callers pass this only when the
    URL did not change (a same-page mutation)."""
    if not new_lines:
        return
    print("  new on page:")
    for s in new_lines[:limit]:
        print(f"    + {s[:200]}")
    if len(new_lines) > limit:
        print(f"    … +{len(new_lines) - limit} more new lines (run `browse text`)")


# ---------- verbs ----------

def cmd_goto(args: argparse.Namespace) -> int:
    url = args.url
    if "://" not in url:
        url = "https://" + url
    r = _request("goto", url=url)
    print(f"ok {r.get('url', '')}")
    if r.get("title"):
        print(f"   {r['title']}")
    return 0


def cmd_text(args: argparse.Namespace) -> int:
    r = _request("text")
    text = r.get("text", "") or ""
    if args.max_chars and len(text) > args.max_chars:
        text = text[: args.max_chars] + f"\n…[truncated, {len(text) - args.max_chars} more chars]"
    print(text)
    return 0


def cmd_dom(args: argparse.Namespace) -> int:
    r = _request("dom")
    items = r.get("items", [])
    for it in items:
        tag = it.get("tag", "?")
        typ = it.get("type") or ""
        if typ:
            tag = f"{tag}[{typ}]"
        text = (it.get("text") or "").strip()
        if not text and it.get("href"):
            text = it["href"]
        line = f"  {it['idx']:>3}  {tag:<14} {text}"
        err = (it.get("err") or "").strip()
        if err:
            line += f"  ⚠ {err}"
        print(line)
    if not items:
        print("(no interactive elements found)")
    return 0


def cmd_click(args: argparse.Namespace) -> int:
    r = _request("click", idx=args.idx)
    status = r.get("status")
    if status == "NOT_FOUND":
        sys.exit(f"browse: idx {args.idx} not found in current DOM")
    if status == "DISABLED":
        sys.exit(
            f"browse: idx {args.idx} is DISABLED — the page is blocking this "
            f"button, so the click did nothing. This is almost always failed "
            f"form validation. Do NOT retry the click. Run `browse dom` and "
            f"read the ⚠ markers for the invalid fields, fix those inputs "
            f"(e.g. a password over the length limit, a missing/duplicate "
            f"field), and the button enables itself once the form is valid."
        )
    print(f"clicked {args.idx} → {r.get('url', '')}")
    _print_new_lines(r.get("new_lines", []))
    return 0


def cmd_type(args: argparse.Namespace) -> int:
    r = _request("type", idx=args.idx, text=args.text, submit=bool(args.submit))
    if r.get("status") == "NOT_FOUND":
        sys.exit(f"browse: idx {args.idx} not found in current DOM")
    print(f"typed into {args.idx}")
    if args.submit:
        _print_new_lines(r.get("new_lines", []))
    return 0


# User-facing key name → Playwright key. Mirrors the old _KEYMAP surface so the
# documented `press` words keep working.
_KEYMAP = {
    "enter": "Enter",
    "return": "Enter",
    "tab": "Tab",
    "escape": "Escape",
    "esc": "Escape",
    "backspace": "Backspace",
    "delete": "Delete",
    "arrowdown": "ArrowDown",
    "arrowup": "ArrowUp",
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
    "down": "ArrowDown",
    "up": "ArrowUp",
    "left": "ArrowLeft",
    "right": "ArrowRight",
    "pagedown": "PageDown",
    "pageup": "PageUp",
    "home": "Home",
    "end": "End",
    "space": "Space",
}


def cmd_press(args: argparse.Namespace) -> int:
    key = args.key.lower()
    if key not in _KEYMAP:
        sys.exit(f"browse: unknown key {args.key!r} (known: {', '.join(sorted(_KEYMAP))})")
    pw_key = _KEYMAP[key]
    r = _request("press", key=pw_key)
    print(f"pressed {r.get('code', pw_key)}")
    _print_new_lines(r.get("new_lines", []))
    return 0


def cmd_scroll(args: argparse.Namespace) -> int:
    direction = args.direction or "down"
    try:
        amount = int(direction)
    except ValueError:
        amount = 800 if direction == "down" else -800 if direction == "up" else 800
    r = _request("scroll", amount=amount)
    print(f"scrolled {r.get('amount', amount)}")
    return 0


def cmd_screenshot(args: argparse.Namespace) -> int:
    slug = _slug()
    out = Path(args.path) if args.path else (PAI_ROOT / "proc" / slug / "screenshot.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    r = _request("screenshot", path=str(out))
    print(f"![viewport]({out}) (saved, {r.get('bytes', 0)} bytes)")
    return 0


def cmd_url(args: argparse.Namespace) -> int:
    r = _request("url")
    print(r.get("url", ""))
    return 0


def cmd_title(args: argparse.Namespace) -> int:
    r = _request("title")
    print(r.get("title", ""))
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    # Last-resort escape hatch: run arbitrary JS in your page. Reach for this
    # ONLY when `dom` can't see an element or you need page state the structured
    # verbs don't expose. Runs in the same Playwright page as every other verb.
    r = _request("eval", expr=args.expr, await_promise=bool(args.await_promise))
    if "js_error" in r:
        print(f"JS error: {r['js_error']}", file=sys.stderr)
        return 1
    if r.get("undef"):
        print("(no value: undefined)")
        return 0
    val = r.get("value")
    if isinstance(val, (dict, list)):
        print(json.dumps(val, indent=2, ensure_ascii=False))
    else:
        print(val)
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    target = args.what
    is_selector = target.startswith(("#", ".", "[")) or any(
        c in target for c in (">", " ", "(", ":")
    )
    r = _request("wait", what=target, timeout=args.timeout, is_selector=is_selector)
    if r.get("found"):
        print(f"found {target!r}")
        return 0
    sys.exit(f"browse: wait timed out after {args.timeout}s for {target!r}")


def cmd_tabs(args: argparse.Namespace) -> int:
    r = _request("tabs")
    tabs = r.get("tabs", [])
    if not tabs:
        print("(no tabs)")
        return 0
    for t in tabs:
        tag = "mine" if t.get("mine") else f"owned by {t.get('slug', '?')}"
        title = t.get("title") or "(untitled)"
        url = t.get("url", "")
        print(f"  {t.get('slug', '?')}  [{tag}]  {title}  {url}")
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    r = _request("close")
    if r.get("closed"):
        print("closed")
    else:
        print("(no tab to close)")
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    # Obsolete under the Playwright daemon: each slug owns exactly one Page
    # automatically, so there are no shared CDP tab ids to claim. Kept as a
    # no-op so the historical verb surface doesn't error.
    print(
        "browse: 'claim' is obsolete under the Playwright daemon — each slug "
        "owns one page automatically; nothing to claim."
    )
    return 0


# ---------- argparse ----------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="browse",
        description="Playwright verbs against PAI's persistent Chrome.",
    )
    sub = p.add_subparsers(dest="verb", required=True)

    sg = sub.add_parser("goto", help="navigate the tab to URL")
    sg.add_argument("url")
    sg.set_defaults(func=cmd_goto)

    st = sub.add_parser("text", help="print current page innerText")
    st.add_argument("--max-chars", type=int, default=8000)
    st.set_defaults(func=cmd_text)

    sev = sub.add_parser("eval", help="LAST RESORT: run JS in the page when dom can't see an element")
    sev.add_argument("expr", help="JavaScript expression to evaluate (its value is printed)")
    sev.add_argument("--await", dest="await_promise", action="store_true",
                     help="await a returned Promise before printing")
    sev.add_argument("--timeout", type=float, default=15.0)
    sev.set_defaults(func=cmd_eval)

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

    sb = sub.add_parser("tabs", help="list my current tab")
    sb.set_defaults(func=cmd_tabs)

    sa = sub.add_parser("claim", help=argparse.SUPPRESS)
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
