# Raw PRD: Payment Ledger

Build `authorize_payment_entry(amount_cents, currency, risk_score, idempotency_key, seen_keys)` to process payments cleanly, avoid duplicates, and hold risky or large payments.
