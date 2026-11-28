# Single-Pass NL Spec: 5G Slice QoS Gate

Implement `evaluate_slice_qos(slice_tier: str, requested_mbps: int, available_mbps: int, reserved_emergency_mbps: int) -> dict[str, object]`.

## Requirements
1. Raise `ValueError` if `requested_mbps <= 0`, `available_mbps < 0`, `reserved_emergency_mbps < 0`, or `slice_tier.strip().lower() not in {"emergency", "enterprise", "consumer"}`.
2. Normalize `tier = slice_tier.strip().lower()`.
3. Compute `usable = available_mbps if tier == "emergency" else max(0, available_mbps - reserved_emergency_mbps)`.
4. If `requested_mbps <= usable`, return `{"action": "admit", "granted_mbps": requested_mbps, "tier": tier}`.
5. If `tier == "emergency"`, return `{"action": "partial_admit", "granted_mbps": usable, "tier": tier}`.
6. If `tier == "enterprise"`, return `{"action": "queue", "granted_mbps": 0, "tier": tier}`.
7. Otherwise (`consumer`), return `{"action": "throttle", "granted_mbps": usable, "tier": tier}`.
