# Single-Pass NL Spec: Payment Idempotency & Risk Hold Ledger Gate

Implement `authorize_payment_entry(amount_cents: int, currency: str, risk_score: int, idempotency_key: str, seen_keys: set[str] | list[str]) -> dict[str, object]`.

## Requirements
1. Validate inputs: raise `ValueError` if `amount_cents <= 0`, `risk_score < 0` or `risk_score > 100`, `currency.strip().upper() not in {"USD", "EUR", "INR"}`, or `not idempotency_key.strip()`.
2. Normalize `key = idempotency_key.strip()` and `curr = currency.strip().upper()`.
3. If `key` is already present in `seen_keys`, return `{"status": "duplicate_noop", "charged": False, "currency": curr, "idempotency_key": key}` without mutating `seen_keys`.
4. Otherwise, if `risk_score >= 80` or `amount_cents > 250000`, return `{"status": "manual_risk_hold", "charged": False, "currency": curr, "idempotency_key": key}`.
5. Otherwise, return `{"status": "captured", "charged": True, "currency": curr, "idempotency_key": key}`.
