"""Reply-parent Message-ID normalization in the macmail outbound driver.

The archive stores RFC Message-IDs verbatim — `<abc@example.com>`, angle
brackets included — and the email skill tells the PAI to copy that value
into `in_reply_to`. Mail.app's AppleScript `message id` property returns
the ID *without* brackets, so an unstripped value can never match and
every reply died with "parent message not found". These tests pin the
normalization that closed that gap.
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
