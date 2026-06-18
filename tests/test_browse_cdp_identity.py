from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_browse():
    path = ROOT / "bin" / "browse" / "browse.py"
    spec = importlib.util.spec_from_file_location("browse_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


browse = _load_browse()


def test_pai_uses_dedicated_debug_port_not_9222():
    # The conventional Chrome debug port (9222) is what the owner's own Chrome
    # (or an IDE/login item) is most likely to expose. PAI must not share it.
    assert browse.CDP_PORT != 9222
    assert str(browse.CDP_PORT) in browse.CDP_BASE


def test_recognizes_pai_chrome_by_profile():
    browse.CHROME_PROFILE = "/Users/x/.pai/var/chrome/profile"
    cmd = (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
        f"--remote-debugging-port={browse.CDP_PORT} "
        "--user-data-dir=/Users/x/.pai/var/chrome/profile --no-first-run about:blank"
    )
    assert browse._cmdline_is_pai_chrome(cmd) is True


def test_rejects_owner_real_chrome_default_profile():
    browse.CHROME_PROFILE = "/Users/x/.pai/var/chrome/profile"
    # Owner's everyday Chrome on the debug port, no --user-data-dir (default profile).
    cmd = (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
        f"--remote-debugging-port={browse.CDP_PORT}"
    )
    assert browse._cmdline_is_pai_chrome(cmd) is False


def test_rejects_other_user_data_dir():
    browse.CHROME_PROFILE = "/Users/x/.pai/var/chrome/profile"
    cmd = (
        "Google Chrome "
        f"--remote-debugging-port={browse.CDP_PORT} "
        "--user-data-dir=/Users/x/Library/Application Support/Google/Chrome"
    )
    assert browse._cmdline_is_pai_chrome(cmd) is False


def test_owner_is_pai_delegates_to_listener_cmdline(monkeypatch):
    browse.CHROME_PROFILE = "/p/profile"
    monkeypatch.setattr(
        browse, "_port_listener_cmdline", lambda port: "chrome --user-data-dir=/p/profile"
    )
    assert browse._cdp_owner_is_pai() is True

    monkeypatch.setattr(
        browse, "_port_listener_cmdline", lambda port: "chrome --user-data-dir=/other"
    )
    assert browse._cdp_owner_is_pai() is False


def test_owner_is_pai_is_conservative_when_listener_unknown(monkeypatch):
    # If we cannot introspect who holds the port, never claim it as ours —
    # refusing is safe; silently driving a foreign browser is the bug.
    browse.CHROME_PROFILE = "/p/profile"
    monkeypatch.setattr(browse, "_port_listener_cmdline", lambda port: None)
    assert browse._cdp_owner_is_pai() is False
