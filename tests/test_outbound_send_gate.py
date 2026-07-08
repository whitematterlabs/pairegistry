"""Send-gate freeze logic for the iMessage/WhatsApp outbound drivers.

Both drivers gate direct sends on the live `capabilities.<chan>_send` mode,
with the projected freeze file kept only as a fail-closed backstop. These tests
pin the two footguns we closed:

  1. A `yes`->`ask`/`no` downgrade must take effect from config immediately,
     even before the kernel re-projects the freeze file (file may lag absent).
  2. The old `PAI_*_SENDS_FROZEN` env override is gone and must not resurrect —
     it can no longer flip a frozen channel open.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAI_SRC = ROOT.parent / "pai" / "src"
sys.path[:0] = [str(PAI_SRC), str(ROOT), str(ROOT / "lib")]

import pytest  # noqa: E402

from drivers.imessage import outbound as im  # noqa: E402
from drivers.whatsapp import outbound as wa  # noqa: E402

CASES = [(im, "imessage_send"), (wa, "whatsapp_send")]


def _stub(mod, flag, mode, freeze_path, monkeypatch):
    monkeypatch.setattr(mod, "FREEZE_PATH", freeze_path)
    monkeypatch.setattr(mod.config, "capability_modes", lambda: {flag: mode})


@pytest.mark.parametrize("mod,flag", CASES)
@pytest.mark.parametrize("mode", ["no", "ask"])
def test_downgrade_freezes_even_with_no_freeze_file(mod, flag, mode, tmp_path, monkeypatch):
    """yes->no/ask race: config is authoritative, so a not-yet-reprojected
    (absent) freeze file does not let a downgraded send slip out."""
    _stub(mod, flag, mode, tmp_path / "outbound.freeze", monkeypatch)  # file absent
    assert mod._sends_frozen() is True


@pytest.mark.parametrize("mod,flag", CASES)
def test_yes_with_no_freeze_file_sends(mod, flag, tmp_path, monkeypatch):
    _stub(mod, flag, "yes", tmp_path / "outbound.freeze", monkeypatch)  # file absent
    assert mod._sends_frozen() is False


@pytest.mark.parametrize("mod,flag", CASES)
def test_stray_freeze_file_is_failclosed_backstop_under_yes(mod, flag, tmp_path, monkeypatch):
    """Even at mode=yes, a freeze file on disk still stops sends."""
    freeze = tmp_path / "outbound.freeze"
    freeze.write_text("frozen by hand\n")
    _stub(mod, flag, "yes", freeze, monkeypatch)
    assert mod._sends_frozen() is True


@pytest.mark.parametrize("mod,flag", CASES)
def test_removed_env_override_cannot_unfreeze(mod, flag, tmp_path, monkeypatch):
    """The deleted PAI_*_SENDS_FROZEN override must not resurrect: setting it to
    a falsey value can no longer flip a config-frozen channel open."""
    chan = flag.split("_", 1)[0].upper()
    monkeypatch.setenv(f"PAI_{chan}_SENDS_FROZEN", "0")
    _stub(mod, flag, "no", tmp_path / "outbound.freeze", monkeypatch)  # file absent
    assert mod._sends_frozen() is True


@pytest.mark.parametrize("mod,flag", CASES)
def test_freeze_reason_names_config_mode_when_file_absent(mod, flag, tmp_path, monkeypatch):
    _stub(mod, flag, "no", tmp_path / "outbound.freeze", monkeypatch)  # file absent
    reason = mod._freeze_reason()
    assert flag in reason and "mode=no" in reason
