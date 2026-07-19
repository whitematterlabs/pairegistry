"""send_allowlist bypass in ask mode: an allowlisted recipient sends
directly; anything else still stages for owner approval. Replies (email)
never bypass — Mail's scripted `reply` addresses the parent's From/Reply-To,
which the driver can't see at gate time."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAI_SRC = ROOT.parent / "pai" / "src"
sys.path[:0] = [str(PAI_SRC), str(ROOT), str(ROOT / "lib")]

import pytest  # noqa: E402

from drivers.email.macmail import outbound as em  # noqa: E402
from drivers.imessage import outbound as im  # noqa: E402
from drivers.whatsapp import outbound as wa  # noqa: E402


def _ask_mode(mod, flag, monkeypatch, allowlist):
    monkeypatch.setattr(mod.config, "capability_modes", lambda: {flag: "ask"})
    monkeypatch.setattr(
        mod.config, "send_allowlist", lambda ch: list(allowlist.get(ch, []))
    )


# ── imessage ────────────────────────────────────────────────────────────────

def _im_env(tmp_path, monkeypatch, meta, allowlist):
    _ask_mode(im, "imessage_send", monkeypatch, allowlist)
    monkeypatch.setattr(im, "FREEZE_PATH", tmp_path / "outbound.freeze")
    monkeypatch.setattr(im, "_load_meta", lambda p: meta)
    thread = tmp_path / "thread"
    thread.mkdir(parents=True)
    day = thread / "2026-07-19.md"
    day.write_text("")
    sent = []
    staged = []

    async def fake_send(m, text):
        sent.append(text)
        return "iMessage"

    monkeypatch.setattr(im, "_send", fake_send)
    monkeypatch.setattr(im, "_emit_sent", lambda *a, **k: None)
    monkeypatch.setattr(
        im.approvals_queue, "stage_pending", lambda ch, act, **k: staged.append(act)
    )
    return day, sent, staged


def test_imessage_allowlisted_handle_sends_in_ask_mode(tmp_path, monkeypatch):
    meta = {"channel": "imessage", "handles": ["+1 (555) 123-4567"]}
    day, sent, staged = _im_env(
        tmp_path, monkeypatch, meta, {"imessage": ["+15551234567"]}
    )
    assert asyncio.run(im._process_send(day, "hi")) is True
    assert sent == ["hi"]
    assert staged == []
    assert "sent (allowlisted recipient)" in day.read_text()


def test_imessage_non_allowlisted_still_stages(tmp_path, monkeypatch):
    meta = {"channel": "imessage", "handles": ["+19998887777"]}
    day, sent, staged = _im_env(
        tmp_path, monkeypatch, meta, {"imessage": ["+15551234567"]}
    )
    assert asyncio.run(im._process_send(day, "hi")) is False
    assert sent == []
    assert staged == [{"thread": "thread", "text": "hi"}]


def test_imessage_group_matches_chat_guid_not_name(tmp_path, monkeypatch):
    guid = "iMessage;+;chat443533398519855587"
    meta = {"channel": "imessage", "group": True, "chat_guid": guid,
            "handles": ["+15551234567"]}
    day, sent, staged = _im_env(
        tmp_path, monkeypatch, meta, {"imessage": [guid]}
    )
    assert asyncio.run(im._process_send(day, "hi")) is True
    assert staged == []
    # A group is NOT allowlisted just because a member handle is.
    day2, sent2, staged2 = _im_env(
        tmp_path / "b", monkeypatch, meta, {"imessage": ["+15551234567"]}
    )
    assert asyncio.run(im._process_send(day2, "hi")) is False
    assert staged2 != []


# ── whatsapp ────────────────────────────────────────────────────────────────

def test_whatsapp_allowlisted_jid_sends_in_ask_mode(tmp_path, monkeypatch):
    _ask_mode(wa, "whatsapp_send", monkeypatch, {"whatsapp": ["+15551234567"]})
    monkeypatch.setattr(wa, "FREEZE_PATH", tmp_path / "outbound.freeze")
    monkeypatch.setattr(wa, "_materialize_meta", lambda d: True)
    monkeypatch.setattr(wa, "_load_meta", lambda p: {"channel": "whatsapp"})
    monkeypatch.setattr(wa, "_resolve_jid", lambda m, s: "15551234567@s.whatsapp.net")
    monkeypatch.setattr(wa, "_emit_sent", lambda *a, **k: None)
    staged = []
    monkeypatch.setattr(
        wa.approvals_queue, "stage_pending", lambda ch, act, **k: staged.append(act)
    )
    thread = tmp_path / "habib"
    thread.mkdir()
    day = thread / "2026-07-19.md"
    day.write_text("")

    class Client:
        def __init__(self):
            self.sent = []

        async def send(self, jid, text):
            self.sent.append((jid, text))

    client = Client()
    assert asyncio.run(wa._process_send(day, "hi", client)) is True
    assert client.sent == [("15551234567@s.whatsapp.net", "hi")]
    assert staged == []
    assert "sent (allowlisted recipient)" in day.read_text()


def test_whatsapp_non_allowlisted_still_stages(tmp_path, monkeypatch):
    _ask_mode(wa, "whatsapp_send", monkeypatch, {"whatsapp": ["+15551234567"]})
    monkeypatch.setattr(wa, "FREEZE_PATH", tmp_path / "outbound.freeze")
    monkeypatch.setattr(wa, "_materialize_meta", lambda d: True)
    monkeypatch.setattr(wa, "_load_meta", lambda p: {"channel": "whatsapp"})
    monkeypatch.setattr(wa, "_resolve_jid", lambda m, s: "19998887777@s.whatsapp.net")
    staged = []
    monkeypatch.setattr(
        wa.approvals_queue, "stage_pending", lambda ch, act, **k: staged.append(act)
    )
    thread = tmp_path / "habib"
    thread.mkdir()
    day = thread / "2026-07-19.md"
    day.write_text("")

    class Client:
        async def send(self, jid, text):
            raise AssertionError("must not send")

    assert asyncio.run(wa._process_send(day, "hi", Client())) is False
    assert staged == [{"thread": "habib", "text": "hi"}]


# ── email ───────────────────────────────────────────────────────────────────

def test_email_new_message_all_recipients_must_match(monkeypatch):
    monkeypatch.setattr(
        em.config, "send_allowlist", lambda ch: ["*@corp.com", "boss@other.com"]
    )
    ok = {"to": ["alice@corp.com"], "cc": ["boss@other.com"]}
    assert em._recipients_allowlisted(ok) is True
    mixed = {"to": ["alice@corp.com", "eve@stranger.com"]}
    assert em._recipients_allowlisted(mixed) is False


def test_email_reply_never_bypasses(monkeypatch):
    monkeypatch.setattr(em.config, "send_allowlist", lambda ch: ["*@corp.com"])
    reply = {"to": ["alice@corp.com"], "in_reply_to": "<msg-id@corp.com>"}
    assert em._recipients_allowlisted(reply) is False


def test_email_string_to_field_tolerated(monkeypatch):
    monkeypatch.setattr(em.config, "send_allowlist", lambda ch: ["a@corp.com"])
    assert em._recipients_allowlisted({"to": "a@corp.com"}) is True


def test_email_empty_recipients_never_match(monkeypatch):
    monkeypatch.setattr(em.config, "send_allowlist", lambda ch: ["*@corp.com"])
    assert em._recipients_allowlisted({}) is False
