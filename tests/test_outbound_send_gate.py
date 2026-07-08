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

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAI_SRC = ROOT.parent / "pai" / "src"
sys.path[:0] = [str(PAI_SRC), str(ROOT), str(ROOT / "lib")]

import pytest  # noqa: E402
import yaml  # noqa: E402

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


# ── WhatsApp approved-handoff outbox (relocated off the tailer's watched tree) ──
# The old design opened a second recursive watchdog Observer on the tailer's own
# MESSAGES_ROOT; macOS FSEvents rejects a duplicate recursive watch on the same
# path ("already scheduled"), killing the outbox emitter thread so live approved
# sends were only ever delivered by the one-time boot scan. Handoffs now land in
# a flat sys/drivers/whatsapp/outbox/ dir, watched non-recursively — no overlap.

class _FakeClient:
    def __init__(self):
        self.sent = []

    async def send(self, jid, text):
        self.sent.append((jid, text))


def _setup_outbox(tmp_path, monkeypatch, token="tok-abc"):
    outbox = tmp_path / "outbox"
    tokfile = tmp_path / "grant.token"
    tokfile.write_text(token + "\n")
    monkeypatch.setattr(wa, "OUTBOX_ROOT", outbox)
    monkeypatch.setattr(wa, "GRANT_TOKEN_PATH", tokfile)
    return outbox, token


def test_stage_and_scan_roundtrip(tmp_path, monkeypatch):
    outbox, token = _setup_outbox(tmp_path, monkeypatch)
    p = wa.stage_approved_handoff("habib", "hello there")
    assert p.parent == outbox                       # flat, driver-internal dir
    assert wa._is_outbox_file(p)
    assert wa._scan_outbox() == [p]
    assert yaml.safe_load(p.read_text()) == {
        "slug": "habib", "text": "hello there", "token": token,
    }


def test_is_outbox_file_rejects_old_spool_location(tmp_path, monkeypatch):
    _setup_outbox(tmp_path, monkeypatch)
    stale = wa.MESSAGES_ROOT / "habib" / ".outbox" / "x.yaml"
    assert not wa._is_outbox_file(stale)


def test_process_outbox_rejects_bad_token(tmp_path, monkeypatch):
    outbox, _ = _setup_outbox(tmp_path, monkeypatch)
    outbox.mkdir(parents=True)
    bad = outbox / "20260101-000000-x.yaml"
    bad.write_text(yaml.safe_dump({"slug": "habib", "text": "hi", "token": "WRONG"}))
    client = _FakeClient()
    asyncio.run(wa._process_outbox(bad, client))
    assert not bad.exists()          # untrusted handoff is dropped
    assert client.sent == []         # and never sent


def test_process_outbox_skips_missing_slug(tmp_path, monkeypatch):
    outbox, token = _setup_outbox(tmp_path, monkeypatch)
    outbox.mkdir(parents=True)
    f = outbox / "20260101-000000-x.yaml"
    f.write_text(yaml.safe_dump({"text": "hi", "token": token}))  # slug absent
    client = _FakeClient()
    asyncio.run(wa._process_outbox(f, client))
    assert not f.exists()
    assert client.sent == []


def test_process_outbox_delivers_valid_handoff(tmp_path, monkeypatch):
    _setup_outbox(tmp_path, monkeypatch)
    monkeypatch.setattr(wa, "MESSAGES_ROOT", tmp_path / "spool")
    monkeypatch.setattr(wa, "_materialize_meta", lambda d: True)
    monkeypatch.setattr(wa, "_load_meta", lambda d: {"channel": "whatsapp"})
    monkeypatch.setattr(wa, "_resolve_jid", lambda meta, slug: "123@s.whatsapp.net")
    monkeypatch.setattr(wa, "_append_canonical", lambda day, text: f"[00:00] me: {text}")
    monkeypatch.setattr(wa, "_emit_sent", lambda *a, **k: None)
    p = wa.stage_approved_handoff("habib", "approved msg")
    client = _FakeClient()
    asyncio.run(wa._process_outbox(p, client))
    assert client.sent == [("123@s.whatsapp.net", "approved msg")]
    assert not p.exists()            # delivered handoff removed
