# Single-Pass NL Spec: Runway Clearance Interlock

Implement `evaluate_runway_clearance(flight_id: str, runway_occupied: bool, visibility_m: int, crosswind_kt: int, emergency_override: bool) -> dict[str, object]`.

## Requirements
1. Raise `ValueError` if `not flight_id.strip()`, `visibility_m < 0`, or `crosswind_kt < 0`.
2. Compute `unsafe = runway_occupied or visibility_m < 550 or crosswind_kt > 30`.
3. If `unsafe` and `emergency_override`, decision is `"emergency_supervisor_review"` and `cleared` is `False`.
4. If `unsafe` and not `emergency_override`, decision is `"hold_short"` and `cleared` is `False`.
5. Otherwise decision is `"cleared_for_takeoff"` and `cleared` is `True`.
6. Return `{"flight_id": flight_id.strip(), "decision": decision, "cleared": cleared}`.
