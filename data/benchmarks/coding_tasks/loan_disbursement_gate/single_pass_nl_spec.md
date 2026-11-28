# Loan Disbursement Compliance Gate — Single-Pass NL Specification

## Scope
Implement `loan_disbursement_gate.evaluate_disbursement_gate(loan_id: str, principal_cents: int, dti_ratio: float, kyc_verified: bool, sanctions_hit: bool) -> dict[str, object]`.

## Functional Requirements
1. Validate `loan_id.strip()` is non-empty, `principal_cents > 0`, and `0.0 <= dti_ratio <= 1.0` (finite); raise `ValueError` otherwise.
2. If `sanctions_hit` is True or not `kyc_verified`, return `{"loan_id": loan_id.strip(), "decision": "blocked_compliance", "disburse": False}`.
3. If `dti_ratio > 0.43` or `principal_cents > 25000000`, return `{"loan_id": loan_id.strip(), "decision": "manual_underwriter_review", "disburse": False}`.
4. Otherwise return `{"loan_id": loan_id.strip(), "decision": "clear_to_disburse", "disburse": True}`.
