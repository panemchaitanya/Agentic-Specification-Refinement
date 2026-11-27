import pytest
from runway_clearance_interlock import evaluate_runway_clearance

def test_clear_exact_boundaries() -> None:
    res = evaluate_runway_clearance(" AI101 ", False, 550, 30, False)
    assert res == {"flight_id": "AI101", "decision": "cleared_for_takeoff", "cleared": True}

def test_unsafe_hold_short() -> None:
    assert evaluate_runway_clearance("AI102", True, 5000, 5, False)["decision"] == "hold_short"
    assert evaluate_runway_clearance("AI103", False, 549, 5, False)["decision"] == "hold_short"
    assert evaluate_runway_clearance("AI104", False, 5000, 31, False)["decision"] == "hold_short"

def test_emergency_routes_to_supervisor_never_auto_clears() -> None:
    res = evaluate_runway_clearance("AI105", True, 200, 40, True)
    assert res["decision"] == "emergency_supervisor_review"
    assert res["cleared"] is False

def test_invalid_inputs_raise() -> None:
    with pytest.raises(ValueError):
        evaluate_runway_clearance("", False, 1000, 5, False)
    with pytest.raises(ValueError):
        evaluate_runway_clearance("AI106", False, -1, 5, False)
