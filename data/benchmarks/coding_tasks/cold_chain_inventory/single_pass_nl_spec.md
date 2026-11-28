# Cold-Chain Inventory — Single-Pass NL Specification

## Scope
Implement `cold_chain_inventory.assess_cold_chain_lot(lot_id: str, temp_celsius: float, excursion_minutes: int) -> dict[str, object]`.

## Functional Requirements
1. Validate `lot_id.strip()` is non-empty, `temp_celsius` is finite, and `excursion_minutes >= 0`; raise `ValueError` otherwise.
2. If `temp_celsius < 0.0` or `temp_celsius > 15.0` or `excursion_minutes > 60`, set `status = "quarantine_lot"`, `shippable = False`.
3. Else if `2.0 <= temp_celsius <= 8.0` and `excursion_minutes == 0`, set `status = "release"`, `shippable = True`.
4. Otherwise, set `status = "qa_stability_review"`, `shippable = False`.
5. Return `{"lot_id": lot_id.strip(), "status": status, "shippable": shippable}`.
