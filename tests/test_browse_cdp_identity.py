from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load_browse():
    path = ROOT / "bin" / "browse" / "browse.py"
    spec = importlib.util.spec_from_file_location("browse_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


browse = _load_browse()


def test_type_expr_sets_value_via_native_setter_not_direct_assignment():
    # React (and Vue) wrap an input's `value` with a patched setter backed by an
    # internal _valueTracker. A direct `el.value = …` runs through that patch and
    # updates the tracker, so the synthetic `input` event that follows is seen as
    # "no change" and onChange never fires — the form's state stays empty even
    # though the DOM shows the typed text. The fix sets value through the native
    # HTMLInputElement/HTMLTextAreaElement prototype setter, bypassing the tracker.
    js = browse._type_expr(4, "Arda")
    # (a) value is written through the native prototype setter.
    assert "getOwnPropertyDescriptor" in js
    assert "desc.set.call(el" in js
    # (b) the value branch must not open with the bare direct assignment (the bug).
    #     A guarded `else el.value = …` fallback for exotic elements is fine.
    assert "in el) { el.value =" not in js
    assert "in el) {el.value =" not in js


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


def test_host_process_tool_prefers_existing_absolute_path(monkeypatch):
    monkeypatch.setattr(
        browse.Path,
        "exists",
        lambda self: str(self) == "/host/bin/tool",
    )

    assert (
        browse._host_process_tool("/missing/tool", "/host/bin/tool", fallback="tool")
        == "/host/bin/tool"
    )


def test_port_listener_uses_host_process_tools(monkeypatch):
    # PAI installs its own `ps` shim on PATH. The CDP ownership check must use
    # host process tools by absolute path, or it cannot read Chrome's cmdline.
    calls = []

    class Result:
        def __init__(self, stdout: str):
            self.stdout = stdout

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "/usr/sbin/lsof":
            return Result("123\n")
        if cmd[0] == "/bin/ps":
            return Result("chrome --user-data-dir=/p/profile")
        return Result("")

    monkeypatch.setattr(browse, "LSOF_BIN", "/usr/sbin/lsof")
    monkeypatch.setattr(browse, "PS_BIN", "/bin/ps")
    monkeypatch.setattr(browse.subprocess, "run", fake_run)

    assert browse._port_listener_cmdline(9333) == "chrome --user-data-dir=/p/profile"
    assert calls[:2] == [
        ["/usr/sbin/lsof", "-nP", "-iTCP:9333", "-sTCP:LISTEN", "-t"],
        ["/bin/ps", "-p", "123", "-o", "command="],
    ]


def test_ensure_tab_closes_orphan_tabs_before_opening_fresh_tab(
    tmp_path, monkeypatch
):
    tabs = tmp_path / "tabs"
    snaps = tmp_path / "snapshots"
    tabs.mkdir()
    snaps.mkdir()
    orphan = tabs / "old-browse.yaml"
    orphan.write_text(
        yaml.safe_dump(
            {
                "tab_id": "old-tab",
                "owner_slug": "old-browse",
                "owner_status": "orphan",
            }
        )
    )
    (snaps / "old-browse.json").write_text("{}")
    closed = []

    monkeypatch.setattr(browse, "TAB_DIR", tabs)
    monkeypatch.setattr(browse, "SNAP_DIR", snaps)
    monkeypatch.setattr(browse, "_ensure_chrome", lambda: None)
    monkeypatch.setattr(
        browse, "_list_targets", lambda: [{"id": "old-tab", "type": "page"}]
    )
    monkeypatch.setattr(browse, "_close_target", lambda tab_id: closed.append(tab_id))
    monkeypatch.setattr(
        browse,
        "_new_target",
        lambda: {
            "id": "new-tab",
            "url": "about:blank",
            "title": "",
            "webSocketDebuggerUrl": "ws://new-tab",
        },
    )

    tab, ws_url = browse._ensure_tab("new-browse")

    assert closed == ["old-tab"]
    assert not orphan.exists()
    assert not (snaps / "old-browse.json").exists()
    assert tab["tab_id"] == "new-tab"
    assert tab["owner_slug"] == "new-browse"
    assert tab["owner_status"] == "running"
    assert ws_url == "ws://new-tab"


def test_ensure_tab_does_not_reuse_same_slug_orphan(tmp_path, monkeypatch):
    tabs = tmp_path / "tabs"
    tabs.mkdir()
    (tmp_path / "snapshots").mkdir()
    (tabs / "browse.yaml").write_text(
        yaml.safe_dump(
            {
                "tab_id": "old-tab",
                "owner_slug": "browse",
                "owner_status": "orphan",
            }
        )
    )
    closed = []

    monkeypatch.setattr(browse, "TAB_DIR", tabs)
    monkeypatch.setattr(browse, "SNAP_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(browse, "_ensure_chrome", lambda: None)
    monkeypatch.setattr(
        browse, "_list_targets", lambda: [{"id": "old-tab", "type": "page"}]
    )
    monkeypatch.setattr(browse, "_ws_for_tab", lambda tab_id: "ws://old-tab")
    monkeypatch.setattr(browse, "_close_target", lambda tab_id: closed.append(tab_id))
    monkeypatch.setattr(
        browse,
        "_new_target",
        lambda: {
            "id": "new-tab",
            "url": "about:blank",
            "title": "",
            "webSocketDebuggerUrl": "ws://new-tab",
        },
    )

    tab, ws_url = browse._ensure_tab("browse")

    assert closed == ["old-tab"]
    assert tab["tab_id"] == "new-tab"
    assert ws_url == "ws://new-tab"
