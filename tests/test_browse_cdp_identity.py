"""Unit tests for the browse CLI client (Playwright-daemon era).

browse.py is now a thin socket client to a Node/Playwright daemon; the old
CDP-port identity guards (_cmdline_is_pai_chrome, _cdp_owner_is_pai, port 9333)
were deleted with the hand-rolled CDP engine. These tests cover the surface
that remains in Python: verb→request shaping, the press key-map, daemon path
resolution, node discovery, and the stdout formats the subagent prompt expects.
"""

from __future__ import annotations

import importlib.util
import json
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_browse():
    path = ROOT / "bin" / "browse" / "browse.py"
    spec = importlib.util.spec_from_file_location("browse_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


browse = _load_browse()


# ---------- daemon path resolution ----------

def test_daemon_paths_live_under_pai_root():
    assert browse.SOCK_PATH == browse.STATE_DIR / "browse.sock"
    assert browse.STATE_DIR == browse.PAI_ROOT / "var" / "lib" / "browse"
    assert browse.SERVER_MJS == browse.PAI_ROOT / "usr" / "libexec" / "browse" / "server.mjs"


def test_default_pai_root_ignores_sandbox_home(monkeypatch):
    monkeypatch.delenv("PAI_ROOT", raising=False)
    monkeypatch.setenv("HOME", "/tmp/pai-sandbox-home")

    mod = _load_browse()

    assert mod.PAI_ROOT == mod.REAL_HOME / ".pai"


# ---------- node discovery ----------

def test_find_node_prefers_path(monkeypatch):
    monkeypatch.setattr(browse.shutil, "which", lambda name: "/path/bin/node")
    assert browse._find_node() == "/path/bin/node"


def test_find_node_falls_back_to_homebrew(monkeypatch):
    monkeypatch.setattr(browse.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        browse.Path, "exists", lambda self: str(self) == "/opt/homebrew/bin/node"
    )
    # no nvm dir
    monkeypatch.setattr(browse.Path, "is_dir", lambda self: False)
    assert browse._find_node() == "/opt/homebrew/bin/node"


def test_find_node_missing_exits(monkeypatch):
    monkeypatch.setattr(browse.shutil, "which", lambda name: None)
    monkeypatch.setattr(browse.Path, "exists", lambda self: False)
    monkeypatch.setattr(browse.Path, "is_dir", lambda self: False)
    with pytest.raises(SystemExit):
        browse._find_node()


# ---------- press key-map ----------

def test_keymap_maps_to_playwright_key_names():
    # Real Playwright key names, not CDP virtual-key codes.
    assert browse._KEYMAP["enter"] == "Enter"
    assert browse._KEYMAP["return"] == "Enter"
    assert browse._KEYMAP["down"] == "ArrowDown"
    assert browse._KEYMAP["space"] == "Space"
    assert browse._KEYMAP["esc"] == "Escape"


def test_press_unknown_key_exits(monkeypatch):
    monkeypatch.setattr(browse, "_request", lambda *a, **k: {"ok": True})
    ns = type("NS", (), {"key": "frobnicate"})()
    with pytest.raises(SystemExit):
        browse.cmd_press(ns)


def test_press_sends_mapped_key(monkeypatch):
    sent = {}

    def fake_request(verb, **args):
        sent.update(verb=verb, **args)
        return {"ok": True, "code": "ArrowDown", "new_lines": []}

    monkeypatch.setattr(browse, "_request", fake_request)
    browse.cmd_press(type("NS", (), {"key": "DOWN"})())
    assert sent == {"verb": "press", "key": "ArrowDown"}


# ---------- verb → request shaping ----------

def test_goto_normalizes_scheme(monkeypatch):
    sent = {}
    monkeypatch.setattr(
        browse, "_request",
        lambda verb, **a: sent.update(verb=verb, **a) or {"ok": True, "url": "x", "title": ""},
    )
    browse.cmd_goto(type("NS", (), {"url": "example.com"})())
    assert sent["url"] == "https://example.com"
    # already-schemed urls are left alone
    sent.clear()
    browse.cmd_goto(type("NS", (), {"url": "http://example.com"})())
    assert sent["url"] == "http://example.com"


def test_type_passes_submit_flag(monkeypatch):
    sent = {}
    monkeypatch.setattr(
        browse, "_request",
        lambda verb, **a: sent.update(verb=verb, **a) or {"ok": True, "status": "OK", "new_lines": []},
    )
    browse.cmd_type(type("NS", (), {"idx": 17, "text": "New York", "submit": True})())
    assert sent == {"verb": "type", "idx": 17, "text": "New York", "submit": True}


def test_click_not_found_exits(monkeypatch):
    monkeypatch.setattr(browse, "_request", lambda *a, **k: {"ok": True, "status": "NOT_FOUND"})
    with pytest.raises(SystemExit) as e:
        browse.cmd_click(type("NS", (), {"idx": 9})())
    assert "not found" in str(e.value)


def test_click_disabled_exits_with_guidance(monkeypatch):
    monkeypatch.setattr(
        browse, "_request", lambda *a, **k: {"ok": True, "status": "DISABLED", "tag": "BUTTON"}
    )
    with pytest.raises(SystemExit) as e:
        browse.cmd_click(type("NS", (), {"idx": 3})())
    assert "DISABLED" in str(e.value)
    assert "Do NOT retry" in str(e.value)


def test_click_ok_prints_url_and_new_lines(monkeypatch, capsys):
    monkeypatch.setattr(
        browse, "_request",
        lambda *a, **k: {"ok": True, "status": "OK", "url": "https://x/y",
                         "title": "Y", "new_lines": ["Required Field"]},
    )
    browse.cmd_click(type("NS", (), {"idx": 5})())
    out = capsys.readouterr().out
    assert "clicked 5 → https://x/y" in out
    assert "new on page:" in out
    assert "+ Required Field" in out


def test_wait_selector_heuristic(monkeypatch):
    sent = {}
    monkeypatch.setattr(
        browse, "_request",
        lambda verb, **a: sent.update(verb=verb, **a) or {"ok": True, "found": True},
    )
    browse.cmd_wait(type("NS", (), {"what": "#login", "timeout": 5.0})())
    assert sent["is_selector"] is True
    sent.clear()
    # A bare word (no selector punctuation, no spaces) is treated as page text.
    browse.cmd_wait(type("NS", (), {"what": "Welcome", "timeout": 5.0})())
    assert sent["is_selector"] is False


# ---------- _ensure_daemon / _request ----------

def test_ensure_daemon_missing_server_exits(monkeypatch):
    monkeypatch.setattr(browse, "_daemon_alive", lambda: False)
    monkeypatch.setattr(browse.Path, "is_file", lambda self: False)
    with pytest.raises(SystemExit) as e:
        browse._ensure_daemon()
    assert "paiman install bin/browse" in str(e.value)


def test_request_raises_on_daemon_error(monkeypatch):
    monkeypatch.setattr(browse, "_ensure_daemon", lambda: None)
    monkeypatch.setenv("PAI_SLUG", "pai")

    class FakeSock:
        def settimeout(self, *_): pass
        def connect(self, *_): pass
        def sendall(self, *_): pass
        def recv(self, *_): return json.dumps({"ok": False, "error": "boom"}).encode() + b"\n"
        def close(self): pass

    monkeypatch.setattr(browse.socket, "socket", lambda *a, **k: FakeSock())
    with pytest.raises(SystemExit) as e:
        browse._request("goto", url="x")
    assert "boom" in str(e.value)


def test_request_shapes_payload_with_slug_and_args(monkeypatch):
    monkeypatch.setattr(browse, "_ensure_daemon", lambda: None)
    monkeypatch.setenv("PAI_SLUG", "browse-7")
    captured = {}

    class FakeSock:
        def settimeout(self, *_): pass
        def connect(self, *_): pass
        def sendall(self, data): captured["sent"] = data
        def recv(self, *_): return json.dumps({"ok": True, "url": "u"}).encode() + b"\n"
        def close(self): pass

    monkeypatch.setattr(browse.socket, "socket", lambda *a, **k: FakeSock())
    reply = browse._request("goto", url="https://x")
    assert reply["url"] == "u"
    payload = json.loads(captured["sent"].decode().strip())
    assert payload == {"slug": "browse-7", "verb": "goto", "args": {"url": "https://x"}}


# ---------- new-line rendering ----------

def test_print_new_lines_caps_and_counts(capsys):
    lines = [f"line {i}" for i in range(15)]
    browse._print_new_lines(lines, limit=12)
    out = capsys.readouterr().out
    assert "new on page:" in out
    assert out.count("    + ") == 12
    assert "+3 more new lines" in out


def test_print_new_lines_empty_is_silent(capsys):
    browse._print_new_lines([])
    assert capsys.readouterr().out == ""
