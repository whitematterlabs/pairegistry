#!/usr/bin/env python
"""ax — piloting client for the macOS Accessibility driver.

Subcommands map 1:1 to the axd JSON-RPC methods:

  ax attach <bundle_id> [--no-launch]
  ax detach <session_id>
  ax act <session_id> <ref> [press|set_value|show_menu|pick_menu_item] [--value V] [--title T]
  ax expand <session_id> <ref>
  ax list_sessions [--mine]

Connects to $PAI_ROOT/var/run/ax/axd.sock. Sends one line of JSON, reads
one line of JSON back, prints the result to stdout. Exit codes:
  0 — RPC success
  1 — RPC error (server returned ok=false)
  2 — socket unreachable / bad args / transport error

Identification: every request stamps `target_pid` from $PAI_PID. The
sidecar uses this to route events back to the calling PAI via the
kernel's point-to-point router. If $PAI_PID is missing, the call is
refused locally — running ax outside a PAI shell would emit events that
no one is listening for.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import uuid
from pathlib import Path

PAI_ROOT = Path(os.environ.get("PAI_ROOT", str(Path.home() / ".pai")))
SOCKET_PATH = PAI_ROOT / "var" / "run" / "ax" / "axd.sock"


def _resolve_target_pid() -> int:
    raw = os.environ.get("PAI_PID")
    if raw is None:
        print("error: $PAI_PID not set — ax must be invoked from a PAI turn",
              file=sys.stderr)
        sys.exit(2)
    try:
        return int(raw)
    except ValueError:
        print(f"error: $PAI_PID={raw!r} is not an int", file=sys.stderr)
        sys.exit(2)


def _call(method: str, params: dict) -> dict:
    if not SOCKET_PATH.exists():
        print(f"error: axd socket not present at {SOCKET_PATH} — "
              f"is the ax-in driver running? (paictl status ax-in)",
              file=sys.stderr)
        sys.exit(2)

    request_id = uuid.uuid4().hex[:8]
    request = {"request_id": request_id, "method": method, "params": params}
    line = (json.dumps(request) + "\n").encode("utf-8")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(15.0)
    try:
        sock.connect(str(SOCKET_PATH))
    except OSError as e:
        print(f"error: connect {SOCKET_PATH}: {e}", file=sys.stderr)
        sys.exit(2)

    try:
        sock.sendall(line)
        buf = bytearray()
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf.extend(chunk)
    except OSError as e:
        print(f"error: rpc transport: {e}", file=sys.stderr)
        sys.exit(2)
    finally:
        sock.close()

    nl = buf.find(b"\n")
    payload = bytes(buf[:nl] if nl >= 0 else buf)
    try:
        return json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as e:
        print(f"error: bad response: {e!r}: {payload!r}", file=sys.stderr)
        sys.exit(2)


def _print_and_exit(resp: dict) -> None:
    print(json.dumps(resp.get("result", resp), indent=2, sort_keys=False))
    if resp.get("ok") is False:
        sys.exit(1)
    sys.exit(0)


# MARK: - Subcommands


def cmd_attach(args: argparse.Namespace) -> None:
    params = {
        "bundle_id": args.bundle_id,
        "target_pid": _resolve_target_pid(),
        "launch_if_needed": not args.no_launch,
    }
    _print_and_exit(_call("attach", params))


def cmd_detach(args: argparse.Namespace) -> None:
    _print_and_exit(_call("detach", {"session_id": args.session_id}))


def cmd_act(args: argparse.Namespace) -> None:
    rpc_args: dict = {}
    if args.value is not None: rpc_args["value"] = args.value
    if args.title is not None: rpc_args["title"] = args.title
    _print_and_exit(_call("act", {
        "session_id": args.session_id,
        "ref": int(args.ref),
        "action": args.action,
        "args": rpc_args,
    }))


def cmd_expand(args: argparse.Namespace) -> None:
    _print_and_exit(_call("expand", {
        "session_id": args.session_id,
        "ref": int(args.ref),
    }))


def cmd_list_sessions(args: argparse.Namespace) -> None:
    params: dict = {}
    if args.mine:
        params["target_pid"] = _resolve_target_pid()
    _print_and_exit(_call("list_sessions", params))


# MARK: - CLI


def main() -> None:
    p = argparse.ArgumentParser(prog="ax")
    subs = p.add_subparsers(dest="cmd", required=True)

    a = subs.add_parser("attach", help="Attach a session to the main window of an app.")
    a.add_argument("bundle_id")
    a.add_argument("--no-launch", action="store_true",
                   help="Refuse to launch if the app isn't already running.")
    a.set_defaults(func=cmd_attach)

    d = subs.add_parser("detach", help="Tear down a session.")
    d.add_argument("session_id")
    d.set_defaults(func=cmd_detach)

    ac = subs.add_parser("act", help="Perform an action on an element.")
    ac.add_argument("session_id")
    ac.add_argument("ref")
    ac.add_argument("action", nargs="?", default="press",
                    choices=["press", "set_value", "show_menu", "pick_menu_item"])
    ac.add_argument("--value", help="String for set_value.")
    ac.add_argument("--title", help="Item title for pick_menu_item.")
    ac.set_defaults(func=cmd_act)

    e = subs.add_parser("expand", help="Return the children of a ref.")
    e.add_argument("session_id")
    e.add_argument("ref")
    e.set_defaults(func=cmd_expand)

    ls = subs.add_parser("list_sessions", help="List sessions (all, or just this PAI's).")
    ls.add_argument("--mine", action="store_true",
                    help="Filter by the caller's $PAI_PID.")
    ls.set_defaults(func=cmd_list_sessions)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
