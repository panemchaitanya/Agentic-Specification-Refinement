import pytest
from customs_export_hold_engine import evaluate_export_clearance

def test_cleared_exact_value_boundary() -> None:
    res = evaluate_export_clearance(" de ", 100000, False, False, False, {"KP", "SY"})
    assert res == {"decision": "cleared", "cleared": True, "destination": "DE"}

def test_sanctions_always_block() -> None:
    res = evaluate_export_clearance("kp", 500, False, True, True, {"KP"})
    assert res == {"decision": "blocked_sanctions", "cleared": False, "destination": "KP"}

def test_dual_use_and_high_value_holds() -> None:
    assert evaluate_export_clearance("JP", 5000, True, False, True, set())["decision"] == "compliance_hold"
    assert evaluate_export_clearance("JP", 5000, True, True, False, set())["decision"] == "cleared"
    assert evaluate_export_clearance("JP", 100001, False, False, False, set())["decision"] == "compliance_hold"
    assert evaluate_export_clearance("JP", 100001, False, False, True, set())["decision"] == "cleared"

def test_invalid_inputs_raise() -> None:
    with pytest.raises(ValueError):
        evaluate_export_clearance("", 1000, False, False, False, set())
    with pytest.raises(ValueError):
        evaluate_export_clearance("US", 0, False, False, False, set())
