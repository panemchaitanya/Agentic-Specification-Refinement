# Refund SLA Engine — Single-Pass NL Specification

## Scope
Implement `refund_sla_engine.evaluate_refund_sla(amount_cents: int, elapsed_hours: int, is_chargeback: bool, idempotency_key: str) -> dict[str, object]`.

## Functional Requirements
1. Validate that `amount_cents > 0`, `elapsed_hours >= 0`, and `idempotency_key` is non-empty; raise `ValueError` otherwise.
2. If `is_chargeback` is True or `amount_cents > 50000`, set `decision = "dual_control_review"` and `sla_breached = elapsed_hours > 72`.
3. Otherwise, if `elapsed_hours <= 72`, set `decision = "auto_approve"` and `sla_breached = False`.
4. Otherwise (`elapsed_hours > 72` and `amount_cents <= 50000` and not `is_chargeback`), set `decision = "sla_escalate"` and `sla_breached = True`.
5. Return `{"decision": decision, "sla_breached": sla_breached, "idempotency_key": idempotency_key.strip()}`.
