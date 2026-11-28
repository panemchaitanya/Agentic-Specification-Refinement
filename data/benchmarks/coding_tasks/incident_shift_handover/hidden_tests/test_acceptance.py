"""Hidden acceptance tests for incident_shift_handover."""

from __future__ import annotations

import pytest

from incident_shift_handover import validate_shift_handover


def test_complete_handover_when_all_sev1_acked_and_certified() -> None:
    res = validate_shift_handover(
        outgoing_id="sre-west-1",
        incoming_id="sre-eu-2",
        open_sev1_ids=["INC-10", "INC-20"],
        acked_sev1_ids=["INC-20", "INC-10"],
        incoming_certified=True,
    )
    assert res == {"complete": True, "unacked_sev1": [], "reason": "handover_complete"}


def test_missing_sev1_acknowledgement_blocks_handover() -> None:
    res = validate_shift_handover(
        outgoing_id="sre-west-1",
        incoming_id="sre-eu-2",
        open_sev1_ids=["INC-10", "INC-20"],
        acked_sev1_ids=["INC-10"],
        incoming_certified=True,
    )
    assert res["complete"] is False
    assert res["unacked_sev1"] == ["INC-20"]
    assert res["reason"] == "unacknowledged_sev1"


def test_uncertified_or_same_engineer_blocked() -> None:
    same = validate_shift_handover("sre-1", "sre-1", [], [], True)
    assert same["complete"] is False and same["reason"] == "same_engineer"

    uncert = validate_shift_handover("sre-1", "sre-2", [], [], False)
    assert uncert["complete"] is False and uncert["reason"] == "incoming_uncertified"


def test_blank_engineer_ids_raise_value_error() -> None:
    with pytest.raises(ValueError):
        validate_shift_handover("", "sre-2", [], [], True)
    with pytest.raises(ValueError):
        validate_shift_handover("sre-1", "   ", [], [], True)
