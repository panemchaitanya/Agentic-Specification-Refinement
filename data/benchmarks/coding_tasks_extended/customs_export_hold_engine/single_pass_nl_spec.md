# Single-Pass NL Spec: Export Control Customs Hold Gate

Implement `evaluate_export_clearance(destination_country: str, declared_value_usd: int, is_dual_use: bool, has_export_license: bool, dual_signed: bool, sanctioned_countries: set[str] | list[str]) -> dict[str, object]`.

## Requirements
1. Raise `ValueError` if `declared_value_usd <= 0` or `not destination_country.strip()`.
2. Normalize `dest = destination_country.strip().upper()` and `sanctioned = {c.strip().upper() for c in sanctioned_countries}`.
3. If `dest in sanctioned`, return `{"decision": "blocked_sanctions", "cleared": False, "destination": dest}`.
4. If `(is_dual_use and not has_export_license)` or `(declared_value_usd > 100000 and not dual_signed)`, return `{"decision": "compliance_hold", "cleared": False, "destination": dest}`.
5. Otherwise return `{"decision": "cleared", "cleared": True, "destination": dest}`.
