# Privileged Access Approval Workflow — Single-Pass NL Specification

## Scope
Implement `access_approval_workflow.decide_access_request(requester_id: str, approver_id: str, role: str, duration_hours: int, manager_approved: bool, security_approved: bool, break_glass_ticket: str | None = None) -> dict[str, object]`.

## Functional Requirements
1. Validate `requester_id.strip()`, `approver_id.strip()`, and `role.strip()` are non-empty, and `1 <= duration_hours <= 168`; raise `ValueError` otherwise.
2. If `requester_id.strip() == approver_id.strip()`, return `{"granted": False, "reason": "separation_of_duties_violation"}`.
3. If `role == "prod_admin"`:
   - If `break_glass_ticket` is non-empty and `duration_hours <= 4`, grant with `reason = "break_glass_granted"`.
   - Else if `manager_approved` and `security_approved`, grant with `reason = "dual_approved"`.
   - Else reject with `reason = "missing_privileged_approvals"`.
4. For non-`prod_admin` roles, grant with `reason = "manager_approved"` iff `manager_approved` is True; else reject with `reason = "missing_manager_approval"`.
