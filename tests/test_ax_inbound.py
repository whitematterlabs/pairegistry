from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAI_SRC = ROOT.parent / "pai" / "src"
sys.path[:0] = [str(PAI_SRC), str(ROOT)]

from drivers.ax.inbound import _public_event_from_sidecar  # noqa: E402


def _event(kind: str, **fields):
    data = {"kind": kind, "target_pid": 7, "session_id": "s1"}
    data.update(fields)
    return _public_event_from_sidecar(data)


def test_suppresses_synchronous_success_confirmations():
    assert _event("ax:scope_attached", pid=123, tree=[]) is None
    assert _event("ax:action_result", request_id="r1", ok=True) is None
    assert _event("ax:scope_lost", reason="detached") is None


def test_forwards_failed_action_result():
    public = _event(
        "ax:action_result",
        request_id="r1",
        ok=False,
        error="EFOREGROUND",
    )

    assert public == (
        {
            "source": "ax",
            "kind": "action_result",
            "session_id": "s1",
            "request_id": "r1",
            "ok": False,
            "error": "EFOREGROUND",
        },
        7,
    )


def test_forwards_async_session_updates():
    assert _event(
        "ax:tree_changed",
        delta={"added": [], "removed": [], "changed": [{"ref": 1}]},
    ) == (
        {
            "source": "ax",
            "kind": "tree_changed",
            "session_id": "s1",
            "delta": {"added": [], "removed": [], "changed": [{"ref": 1}]},
        },
        7,
    )

    for reason in ("window_closed", "app_terminated", "grant_revoked", "paused", "resumed"):
        assert _event("ax:scope_lost", reason=reason) == (
            {
                "source": "ax",
                "kind": "scope_lost",
                "session_id": "s1",
                "reason": reason,
            },
            7,
        )


def test_forwards_unknown_valid_ax_events():
    assert _event("ax:future_signal", value=None, detail="kept") == (
        {
            "source": "ax",
            "kind": "future_signal",
            "session_id": "s1",
            "detail": "kept",
        },
        7,
    )


def test_rejects_malformed_or_untargeted_events():
    assert _public_event_from_sidecar({"kind": "scope_lost", "target_pid": 7}) is None
    assert _public_event_from_sidecar({"kind": "ax:scope_lost"}) is None
    assert _public_event_from_sidecar({"kind": "ax:scope_lost", "target_pid": "7"}) is None
