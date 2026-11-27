# Single-Pass NL Spec: Pediatric Clinical Dosage Guard

Implement `evaluate_dosage_order(weight_kg: float, dose_mg: float, max_mg_per_kg: float, patient_has_allergy: bool) -> dict[str, object]`.

## Requirements
1. Raise `ValueError` if `weight_kg <= 0`, `dose_mg <= 0`, or `max_mg_per_kg <= 0`.
2. Compute `max_safe_mg = round(weight_kg * max_mg_per_kg, 4)`.
3. If `patient_has_allergy` is `True`, return `{"status": "blocked_allergy", "safe_to_dispense": False, "max_safe_mg": max_safe_mg}`.
4. If `dose_mg > max_safe_mg`, return `{"status": "blocked_overdose", "safe_to_dispense": False, "max_safe_mg": max_safe_mg}`.
5. If `dose_mg > round(0.8 * max_safe_mg, 4)`, return `{"status": "pharmacist_dual_check", "safe_to_dispense": False, "max_safe_mg": max_safe_mg}`.
6. Otherwise return `{"status": "approved", "safe_to_dispense": True, "max_safe_mg": max_safe_mg}`.
