import pytest
from telecom_qos_throttle_gate import evaluate_slice_qos

def test_emergency_uses_reserved_pool() -> None:
    res = evaluate_slice_qos("emergency", 80, 100, 40)
    assert res == {"action": "admit", "granted_mbps": 80, "tier": "emergency"}

def test_non_emergency_respects_reserved_headroom() -> None:
    assert evaluate_slice_qos("enterprise", 60, 100, 40)["action"] == "admit"
    assert evaluate_slice_qos("enterprise", 61, 100, 40) == {"action": "queue", "granted_mbps": 0, "tier": "enterprise"}
    assert evaluate_slice_qos("consumer", 70, 100, 40) == {"action": "throttle", "granted_mbps": 60, "tier": "consumer"}

def test_invalid_inputs_raise() -> None:
    with pytest.raises(ValueError):
        evaluate_slice_qos("vip", 10, 100, 20)
    with pytest.raises(ValueError):
        evaluate_slice_qos("consumer", 0, 100, 20)
