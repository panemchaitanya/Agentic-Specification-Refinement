"""Hidden acceptance tests for refund_sla_engine."""

from __future__ import annotations

import pytest

from refund_sla_engine import evaluate_refund_sla


def test_auto_approve_exact_boundaries() -> None:
    result = evaluate_refund_sla(
        amount_cents=50000,
        elapsed_hours=72,
        is_chargeback=False,
        idempotency_key="ref-001",
    )
    assert result["decision"] == "auto_approve"
    assert result["sla_breached"] is False
    assert result["idempotency_key"] == "ref-001"


def test_high_value_requires_dual_control() -> None:
    result = evaluate_refund_sla(
        amount_cents=50001,
        elapsed_hours=12,
        is_chargeback=False,
        idempotency_key="ref-002",
    )
    assert result["decision"] == "dual_control_review"
    assert result["sla_breached"] is False


def test_chargeback_always_routes_to_dual_control() -> None:
    result = evaluate_refund_sla(
        amount_cents=500,
        elapsed_hours=96,
        is_chargeback=True,
        idempotency_key="ref-003",
    )
    assert result["decision"] == "dual_control_review"
    assert result["sla_breached"] is True


def test_sla_breach_escalates_small_refund() -> None:
    result = evaluate_refund_sla(
        amount_cents=1200,
        elapsed_hours=73,
        is_chargeback=False,
        idempotency_key="ref-004",
    )
    assert result["decision"] == "sla_escalate"
    assert result["sla_breached"] is True


def test_invalid_inputs_raise_value_error() -> None:
    with pytest.raises(ValueError):
        evaluate_refund_sla(0, 10, False, "ref-bad-1")
    with pytest.raises(ValueError):
        evaluate_refund_sla(100, -1, False, "ref-bad-2")
    with pytest.raises(ValueError):
        evaluate_refund_sla(100, 10, False, "   ")


def test_idempotent_evaluation() -> None:
    first = evaluate_refund_sla(15000, 24, False, "ref-idem")
    second = evaluate_refund_sla(15000, 24, False, "ref-idem")
    assert first == second
