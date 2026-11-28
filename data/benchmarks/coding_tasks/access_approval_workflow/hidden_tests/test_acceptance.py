"""Hidden acceptance tests for access_approval_workflow."""

from __future__ import annotations

import pytest

from access_approval_workflow import decide_access_request


def test_self_approval_always_rejected() -> None:
    res = decide_access_request(
        requester_id="alice",
        approver_id="alice",
        role="read_only",
        duration_hours=4,
        manager_approved=True,
        security_approved=True,
    )
    assert res["granted"] is False
    assert res["reason"] == "separation_of_duties_violation"


def test_prod_admin_requires_dual_or_short_break_glass() -> None:
    denied = decide_access_request("alice", "bob", "prod_admin", 8, True, False)
    assert denied["granted"] is False

    dual = decide_access_request("alice", "bob", "prod_admin", 24, True, True)
    assert dual["granted"] is True
    assert dual["reason"] == "dual_approved"

    bg_ok = decide_access_request(
        "alice", "bob", "prod_admin", 4, False, False, break_glass_ticket="INC-999"
    )
    assert bg_ok["granted"] is True
    assert bg_ok["reason"] == "break_glass_granted"

    bg_too_long = decide_access_request(
        "alice", "bob", "prod_admin", 5, False, False, break_glass_ticket="INC-999"
    )
    assert bg_too_long["granted"] is False


def test_standard_role_manager_approval() -> None:
    ok = decide_access_request("alice", "bob", "read_only", 168, True, False)
    assert ok["granted"] is True


def test_invalid_duration_or_blank_ids_raise() -> None:
    with pytest.raises(ValueError):
        decide_access_request("alice", "bob", "read_only", 0, True, False)
    with pytest.raises(ValueError):
        decide_access_request("alice", "bob", "read_only", 169, True, False)
    with pytest.raises(ValueError):
        decide_access_request("", "bob", "read_only", 4, True, False)
