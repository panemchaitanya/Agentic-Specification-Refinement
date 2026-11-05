"""Evaluation metrics for the frozen-split runs (Stage 2/4 infrastructure).

Pure functions over per-item match decisions. Semantic matching itself is
NOT here: matching follows the written rubric (Stage 2 deliverable) and is
hand-audited on 20% of pairs; these functions only consume match verdicts.
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class PRF:
    precision: float
    recall: float
    f1: float


def prf(gold: set, predicted: set) -> PRF:
    """Precision/recall/F1 of predicted items against gold items.

    Items are whatever the matching rubric declared equal (e.g. matched
    requirement ids). Empty gold with empty predictions scores perfect;
    empty gold with predictions scores zero precision.
    """
    tp = len(gold & predicted)
    if not gold and not predicted:
        return PRF(1.0, 1.0, 1.0)
    precision = tp / len(predicted) if predicted else 0.0
    recall = tp / len(gold) if gold else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return PRF(precision, recall, f1)


def completeness(found_categories: set, implied_categories: set) -> float:
    """Share of PRD-implied NFR categories present in the spec (proposal's
    completeness measure). 1.0 when nothing was implied."""
    if not implied_categories:
        return 1.0
    return len(found_categories & implied_categories) / len(implied_categories)


def bootstrap_ci(scores_a, scores_b, n_resamples: int = 2000, seed: int = 0):
    """Paired bootstrap 95% CI for the mean difference (a - b) between two
    systems' per-item scores. Deterministic for a fixed seed. Returns
    (mean_diff, low, high)."""
    if len(scores_a) != len(scores_b) or not scores_a:
        raise ValueError("paired inputs must be non-empty and equal length")
    rng = random.Random(seed)
    diffs = [a - b for a, b in zip(scores_a, scores_b)]
    mean_diff = sum(diffs) / len(diffs)
    n = len(diffs)
    means = []
    for _ in range(n_resamples):
        sample = [diffs[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    low = means[int(0.025 * n_resamples)]
    high = means[min(int(0.975 * n_resamples), n_resamples - 1)]
    return mean_diff, low, high
