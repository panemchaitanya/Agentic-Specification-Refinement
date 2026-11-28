import pytest
from breakglass_iam_elevation import evaluate_iam_elevation

def test_staging_single_approval_boundary() -> None:
    res = evaluate_iam_elevation("alice", "staging", 60, True, True, 1, "INC-1")
    assert res == {"approved": True, "reason": "granted", "env": "staging"}

def test_prod_and_long_duration_require_two_approvals() -> None:
    assert evaluate_iam_elevation("alice", "prod", 30, True, True, 1, "INC-2")["approved"] is False
    assert evaluate_iam_elevation("alice", "prod", 30, True, True, 2, "INC-2")["approved"] is True
    assert evaluate_iam_elevation("alice", "dev", 61, True, True, 1, "INC-3")["approved"] is False
    assert evaluate_iam_elevation("alice", "dev", 61, True, True, 2, "INC-3")["approved"] is True

def test_missing_mfa_or_training_denied() -> None:
    assert evaluate_iam_elevation("bob", "dev", 15, False, True, 2, "INC-4")["reason"] == "security_prerequisite_failed"
    assert evaluate_iam_elevation("bob", "dev", 15, True, False, 2, "INC-5")["reason"] == "security_prerequisite_failed"

def test_invalid_inputs_raise() -> None:
    with pytest.raises(ValueError):
        evaluate_iam_elevation("bob", "invalid", 15, True, True, 1, "INC-6")
    with pytest.raises(ValueError):
        evaluate_iam_elevation("bob", "dev", 0, True, True, 1, "INC-7")
