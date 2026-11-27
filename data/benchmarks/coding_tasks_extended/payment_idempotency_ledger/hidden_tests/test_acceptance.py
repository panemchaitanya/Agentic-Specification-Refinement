import pytest
from payment_idempotency_ledger import authorize_payment_entry

def test_capture_exact_boundaries() -> None:
    res = authorize_payment_entry(250000, "usd", 79, " k-1 ", set())
    assert res == {"status": "captured", "charged": True, "currency": "USD", "idempotency_key": "k-1"}

def test_risk_and_amount_holds() -> None:
    assert authorize_payment_entry(1000, "EUR", 80, "k-2", set())["status"] == "manual_risk_hold"
    assert authorize_payment_entry(250001, "INR", 10, "k-3", set())["status"] == "manual_risk_hold"

def test_duplicate_idempotency_noop() -> None:
    seen = {"k-1"}
    res = authorize_payment_entry(5000, "USD", 99, "k-1", seen)
    assert res["status"] == "duplicate_noop"
    assert res["charged"] is False

def test_invalid_inputs_raise() -> None:
    for args in [(0, "USD", 10, "k", set()), (100, "GBP", 10, "k", set()), (100, "USD", 101, "k", set()), (100, "USD", 10, "   ", set())]:
        with pytest.raises(ValueError):
            authorize_payment_entry(*args)
