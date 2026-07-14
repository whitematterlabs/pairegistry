"""Reply-parent Message-ID normalization in the macmail outbound driver.

The archive stores RFC Message-IDs verbatim — `<abc@example.com>`, angle
brackets included — and the email skill tells the PAI to copy that value
into `in_reply_to`. Mail.app's AppleScript `message id` property returns
the ID *without* brackets, so an unstripped value can never match and
every reply died with "parent message not found". Compounding it,
app-level `every mailbox` enumerates only local ("On My Mac") mailboxes
— account mailboxes, where real mail lives, were never searched. These
tests pin both halves of the fix.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAI_SRC = ROOT.parent / "pai" / "src"
sys.path[:0] = [str(PAI_SRC), str(ROOT), str(ROOT / "lib")]

from drivers.email.macmail import outbound  # noqa: E402


MID = "0100019f5c0121f5-abc-000000@email.amazonses.com"


def _script(in_reply_to: str) -> str:
    return outbound._build_reply_script(
        "owner@example.com",
        {"in_reply_to": in_reply_to, "content": "hi"},
    )


def test_angle_brackets_stripped():
    script = _script(f"<{MID}>")
    assert f'whose message id is "{MID}"' in script
    assert "<" not in script.split("whose message id is")[1].split("\n")[0]


def test_bare_id_unchanged():
    assert f'whose message id is "{MID}"' in _script(MID)


def test_whitespace_and_brackets_stripped():
    assert f'whose message id is "{MID}"' in _script(f"  <{MID}>  ")


def test_searches_account_mailboxes():
    """App-level `every mailbox` sees only local mailboxes; the parent
    lives in an account mailbox, so accounts must be enumerated."""
    script = _script(MID)
    assert "every account" in script
    assert "mailboxes of acc" in script


def test_reply_honors_reply_to():
    """Scripted `reply` targets the From; notification mail (Zulip etc.)
    needs the parent's Reply-To or the reply black-holes at noreply@."""
    script = _script(MID)
    assert "reply to of parentMsg" in script
    assert "extract address from rt" in script
