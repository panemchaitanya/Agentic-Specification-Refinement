"""Hidden acceptance tests for triage_escalation_queue."""

from __future__ import annotations

import pytest

from triage_escalation_queue import route_triage_patient


def test_critical_acuity_bypasses_zero_beds() -> None:
    res = route_triage_patient("PAT-101", acuity=1, wait_minutes=0, available_beds=0)
    assert res["route"] == "immediate_resuscitation"
    assert res["requires_charge_nurse"] is True


def test_acuity_three_boundary_at_thirty_minutes() -> None:
    at_30 = route_triage_patient("PAT-102", acuity=3, wait_minutes=30, available_beds=2)
    over_30 = route_triage_patient("PAT-103", acuity=3, wait_minutes=31, available_beds=2)
    assert at_30["route"] == "direct_bed_assign"
    assert over_30["route"] == "charge_nurse_review"
    assert over_30["requires_charge_nurse"] is True


def test_zero_beds_queues_non_critical_patient() -> None:
    res = route_triage_patient("PAT-104", acuity=4, wait_minutes=15, available_beds=0)
    assert res["route"] == "waiting_queue"
    assert res["requires_charge_nurse"] is False


def test_invalid_triage_inputs_raise() -> None:
    with pytest.raises(ValueError):
        route_triage_patient("", 2, 0, 1)
    with pytest.raises(ValueError):
        route_triage_patient("PAT-105", 0, 5, 1)
    with pytest.raises(ValueError):
        route_triage_patient("PAT-106", 6, 5, 1)
    with pytest.raises(ValueError):
        route_triage_patient("PAT-107", 3, -1, 1)
    with pytest.raises(ValueError):
        route_triage_patient("PAT-108", 3, 5, -1)
