import pytest
from clinical_dosage_guard import evaluate_dosage_order

def test_approved_exact_80_pct_boundary() -> None:
    res = evaluate_dosage_order(20.0, 160.0, 10.0, False)
    assert res == {"status": "approved", "safe_to_dispense": True, "max_safe_mg": 200.0}

def test_dual_check_and_overdose_boundaries() -> None:
    assert evaluate_dosage_order(20.0, 160.1, 10.0, False)["status"] == "pharmacist_dual_check"
    assert evaluate_dosage_order(20.0, 200.0, 10.0, False)["status"] == "pharmacist_dual_check"
    assert evaluate_dosage_order(20.0, 200.1, 10.0, False)["status"] == "blocked_overdose"

def test_allergy_always_blocks_first() -> None:
    res = evaluate_dosage_order(20.0, 10.0, 10.0, True)
    assert res["status"] == "blocked_allergy"
    assert res["safe_to_dispense"] is False

def test_invalid_inputs_raise() -> None:
    for args in [(0.0, 10.0, 5.0, False), (10.0, 0.0, 5.0, False), (10.0, 10.0, -1.0, False)]:
        with pytest.raises(ValueError):
            evaluate_dosage_order(*args)
