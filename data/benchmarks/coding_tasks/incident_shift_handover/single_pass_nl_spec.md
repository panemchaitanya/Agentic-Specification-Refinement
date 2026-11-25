# Incident Shift Handover — Single-Pass NL Specification

## Scope
Implement `incident_shift_handover.validate_shift_handover(outgoing_id: str, incoming_id: str, open_sev1_ids: list[str], acked_sev1_ids: list[str], incoming_certified: bool) -> dict[str, object]`.

## Functional Requirements
1. Validate `outgoing_id.strip()` and `incoming_id.strip()` are non-empty; raise `ValueError` otherwise.
2. If `outgoing_id.strip() == incoming_id.strip()`, return `{"complete": False, "unacked_sev1": sorted(set(open_sev1_ids)), "reason": "same_engineer"}`.
3. Compute `unacked = sorted(set(open_sev1_ids) - set(acked_sev1_ids))`.
4. If not `incoming_certified`, return `{"complete": False, "unacked_sev1": unacked, "reason": "incoming_uncertified"}`.
5. If `unacked` is non-empty, return `{"complete": False, "unacked_sev1": unacked, "reason": "unacknowledged_sev1"}`.
6. Otherwise return `{"complete": True, "unacked_sev1": [], "reason": "handover_complete"}`.
