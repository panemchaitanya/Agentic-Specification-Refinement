# Emergency Triage Escalation Queue — Single-Pass NL Specification

## Scope
Implement `triage_escalation_queue.route_triage_patient(patient_id: str, acuity: int, wait_minutes: int, available_beds: int) -> dict[str, object]`.

## Functional Requirements
1. Validate `patient_id.strip()` is non-empty, `1 <= acuity <= 5`, `wait_minutes >= 0`, and `available_beds >= 0`; raise `ValueError` otherwise.
2. If `acuity <= 2`, return `route = "immediate_resuscitation"`, `requires_charge_nurse = True`.
3. If `acuity == 3` and `wait_minutes > 30`, return `route = "charge_nurse_review"`, `requires_charge_nurse = True`.
4. If `available_beds > 0`, return `route = "direct_bed_assign"`, `requires_charge_nurse = False`.
5. Otherwise (`available_beds == 0` and `acuity >= 3`), return `route = "waiting_queue"`, `requires_charge_nurse = False`.
