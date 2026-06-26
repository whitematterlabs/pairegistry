from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAI_SRC = ROOT.parent / "pai" / "src"
sys.path[:0] = [str(PAI_SRC), str(ROOT)]

from drivers.email import shared  # noqa: E402


def test_email_spool_paths_are_reported_in_home_view() -> None:
    assert (
        shared.home_view_path(
            "var/spool/communication/email/owner@example.com/2026-06-23/msg.yaml"
        )
        == "communication/email/owner@example.com/2026-06-23/msg.yaml"
    )


def test_email_draft_spool_paths_are_reported_in_home_view() -> None:
    assert (
        shared.home_view_path("var/spool/communication/email/drafts/re-alex.yaml")
        == "communication/email/drafts/re-alex.yaml"
    )


def test_email_absolute_fhs_paths_are_reported_in_home_view() -> None:
    assert (
        shared.home_view_path(
            "/var/spool/communication/email/owner@example.com/2026-06-23/msg.yaml"
        )
        == "communication/email/owner@example.com/2026-06-23/msg.yaml"
    )


def test_email_home_view_paths_are_left_alone() -> None:
    assert (
        shared.home_view_path("communication/email/owner@example.com/2026-06-23/msg.yaml")
        == "communication/email/owner@example.com/2026-06-23/msg.yaml"
    )


def test_email_nested_spool_paths_are_reported_in_home_view() -> None:
    # mailv2's nested YYYY/MM/DD partition still rewrites to the home view.
    assert (
        shared.home_view_path(
            "var/spool/communication/email/owner@example.com/2026/06/25/msg.yaml"
        )
        == "communication/email/owner@example.com/2026/06/25/msg.yaml"
    )
