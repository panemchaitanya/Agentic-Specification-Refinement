"""Hidden acceptance tests for loan_disbursement_gate."""

from __future__ import annotations

import math
import pytest

from loan_disbursement_gate import evaluate_disbursement_gate


def test_clean_boundary_clears_to_disburse() -> None:
    res = evaluate_disbursement_gate("LN-100", 25000000, 0.43, True, False)
    assert res == {"loan_id": "LN-100", "decision": "clear_to_disburse", "disburse": True}


def test_sanctions_or_unverified_kyc_blocks() -> None:
    sanc = evaluate_disbursement_gate("LN-101", 100000, 0.20, True, True)
    no_kyc = evaluate_disbursement_gate("LN-102", 100000, 0.20, False, False)
    assert sanc["decision"] == "blocked_compliance" and sanc["disburse"] is False
    assert no_kyc["decision"] == "blocked_compliance" and no_kyc["disburse"] is False


def test_high_dti_or_large_principal_escalates() -> None:
    high_dti = evaluate_disbursement_gate("LN-103", 500000, 0.431, True, False)
    large = evaluate_disbursement_gate("LN-104", 25000001, 0.25, True, False)
    assert high_dti["decision"] == "manual_underwriter_review" and high_dti["disburse"] is False
    assert large["decision"] == "manual_underwriter_review" and large["disburse"] is False


def test_invalid_loan_inputs_raise() -> None:
    with pytest.raises(ValueError):
        evaluate_disbursement_gate("", 1000, 0.2, True, False)
    with pytest.raises(ValueError):
        evaluate_disbursement_gate("LN-105", 0, 0.2, True, False)
    with pytest.raises(ValueError):
        evaluate_disbursement_gate("LN-106", 1000, -0.01, True, False)
    with pytest.raises(ValueError):
        evaluate_disbursement_gate("LN-107", 1000, math.nan, True, False)
