# Single-Pass NL Spec: Break-Glass IAM Elevation Gate

Implement `evaluate_iam_elevation(principal: str, target_env: str, duration_min: int, mfa_verified: bool, training_current: bool, dual_approval_count: int, ticket_id: str) -> dict[str, object]`.

## Requirements
1. Raise `ValueError` if `not principal.strip()`, `not ticket_id.strip()`, `duration_min <= 0`, `dual_approval_count < 0`, or `target_env.strip().lower() not in {"dev", "staging", "prod"}`.
2. Normalize `env = target_env.strip().lower()`.
3. If not `mfa_verified` or not `training_current`, return `{"approved": False, "reason": "security_prerequisite_failed", "env": env}`.
4. Compute `required_approvals = 2 if (env == "prod" or duration_min > 60) else 1`.
5. If `dual_approval_count < required_approvals`, return `{"approved": False, "reason": "insufficient_approvals", "env": env}`.
6. Otherwise return `{"approved": True, "reason": "granted", "env": env}`.
