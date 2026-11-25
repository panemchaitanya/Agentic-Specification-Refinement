"""Hidden acceptance tests for cold_chain_inventory."""

from __future__ import annotations

import math
import pytest

from cold_chain_inventory import assess_cold_chain_lot


def test_nominal_range_boundaries_release() -> None:
    low = assess_cold_chain_lot("LOT-01", 2.0, 0)
    high = assess_cold_chain_lot("LOT-02", 8.0, 0)
    assert low == {"lot_id": "LOT-01", "status": "release", "shippable": True}
    assert high == {"lot_id": "LOT-02", "status": "release", "shippable": True}


def test_severe_excursion_quarantines() -> None:
    frozen = assess_cold_chain_lot("LOT-03", -0.1, 5)
    overheated = assess_cold_chain_lot("LOT-04", 15.1, 1)
    prolonged = assess_cold_chain_lot("LOT-05", 9.0, 61)
    assert frozen["status"] == "quarantine_lot" and frozen["shippable"] is False
    assert overheated["status"] == "quarantine_lot" and overheated["shippable"] is False
    assert prolonged["status"] == "quarantine_lot" and prolonged["shippable"] is False


def test_mild_excursion_routes_to_qa_review() -> None:
    res = assess_cold_chain_lot("LOT-06", 10.5, 60)
    assert res["status"] == "qa_stability_review"
    assert res["shippable"] is False


def test_invalid_cold_chain_inputs_raise() -> None:
    with pytest.raises(ValueError):
        assess_cold_chain_lot("  ", 4.0, 0)
    with pytest.raises(ValueError):
        assess_cold_chain_lot("LOT-07", 4.0, -5)
    with pytest.raises(ValueError):
        assess_cold_chain_lot("LOT-08", math.nan, 0)
