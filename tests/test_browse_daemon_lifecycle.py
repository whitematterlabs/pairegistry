"""_spawn_daemon_proc must self-heal a proc dir left in a terminal state.

The kernel:restart shutdown sweep resolves `restart: always` procs to
`stopped`, and boot does not resurrect stopped procs. Before the
reap-and-respawn fix, P.spawn's ProcessExists made every future browse verb
wait on a socket nobody would ever create ("daemon did not come up within
30s") until someone deleted /proc/browse-daemon by hand.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

import boot.processes as P

ROOT = Path(__file__).resolve().parents[1]


def _load_browse():
    path = ROOT / "bin" / "browse" / "browse.py"
    spec = importlib.util.spec_from_file_location("browse_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


browse = _load_browse()


@pytest.fixture
def proc_dir(monkeypatch, tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(P, "PROC_DIR", proc)
    # Keep the unit test off the real FHS: fake an installed sidecar and node.
    server = tmp_path / "server.mjs"
    server.write_text("// sidecar\n")
    monkeypatch.setattr(browse, "SERVER_MJS", server)
    monkeypatch.setattr(browse, "_find_node", lambda: "/opt/homebrew/bin/node")
    return proc


def _write_proc(proc_dir: Path, status: str, spec: dict) -> Path:
    d = proc_dir / browse.DAEMON_SLUG
    d.mkdir()
    with (d / "spec.yaml").open("w") as f:
        yaml.safe_dump(spec, f, sort_keys=False)
    (d / "status").write_text(f"{status}\n")
    (d / "log.md").write_text("[00:00] spawned\n")
    return d


def test_spawn_reaps_terminal_proc_and_respawns(proc_dir):
    stale = {"kind": "infra", "run": ["node", "old-server.mjs"], "restart": "always"}
    d = _write_proc(proc_dir, "stopped", stale)

    browse._spawn_daemon_proc()

    spec = yaml.safe_load((d / "spec.yaml").read_text())
    assert spec["run"] == ["/opt/homebrew/bin/node", str(browse.SERVER_MJS)]
    assert spec["restart"] == "always"
    assert (d / "status").read_text().strip() == "running"


@pytest.mark.parametrize("status", sorted(P.TERMINAL_STATUSES))
def test_spawn_reaps_every_terminal_status(proc_dir, status):
    d = _write_proc(proc_dir, status, {"kind": "infra", "run": ["x"], "restart": "always"})

    browse._spawn_daemon_proc()

    assert (d / "status").read_text().strip() == "running"


def test_spawn_leaves_active_proc_alone(proc_dir):
    live = {"kind": "infra", "run": ["node", "live-server.mjs"], "restart": "always"}
    d = _write_proc(proc_dir, "running", live)

    browse._spawn_daemon_proc()

    spec = yaml.safe_load((d / "spec.yaml").read_text())
    assert spec["run"] == ["node", "live-server.mjs"]  # untouched
    assert (d / "status").read_text().strip() == "running"


def test_spawn_fresh_when_no_proc_exists(proc_dir):
    browse._spawn_daemon_proc()

    d = proc_dir / browse.DAEMON_SLUG
    spec = yaml.safe_load((d / "spec.yaml").read_text())
    assert spec["run"] == ["/opt/homebrew/bin/node", str(browse.SERVER_MJS)]
    assert (d / "status").read_text().strip() == "running"
