"""Deterministic audit sampling for match verdicts (rubric: 20% checked
by hand). Pure and seeded so the audit sample itself is reproducible.
"""
from __future__ import annotations

import random


def sample_matches_for_audit(verdicts: list, fraction: float = 0.2,
                             seed: int = 0) -> list:
    """Return a deterministic audit sample of (item_id, gold_id, pred_id).

    Samples at least one match per item that has any, then fills to the
    fraction across the whole verdict set. many_to_one items are always
    included (rubric rule).
    """
    rng = random.Random(seed)
    flat = []
    for v in verdicts:
        for g, p in v["matches"]:
            flat.append((v["item_id"], g, p))
    if not flat:
        return []
    target = max(1, round(len(flat) * fraction))
    by_item: dict = {}
    for entry in flat:
        by_item.setdefault(entry[0], []).append(entry)
    sample = {rng.choice(entries) for entries in by_item.values()}
    rest = [e for e in flat if e not in sample]
    rng.shuffle(rest)
    for e in rest:
        if len(sample) >= target:
            break
        sample.add(e)
    return sorted(sample)
