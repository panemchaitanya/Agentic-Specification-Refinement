"""Pure-Python stdlib statistical tests for Phase 4 empirical evaluation.

Zero compiled dependencies (no scipy/numpy): uses only math, random, and statistics.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence

from reqhq.analysis.models import BootstrapCIResult


def _validate_paired_finite(a: Sequence[float], b: Sequence[float]) -> list[float]:
    """Validate equal-length finite sequences and return exact pairwise differences a_i - b_i."""
    if len(a) != len(b):
        raise ValueError(f"Paired sequences must have equal length, got {len(a)} and {len(b)}")
    diffs: list[float] = []
    for idx, (x_raw, y_raw) in enumerate(zip(a, b)):
        x = float(x_raw)
        y = float(y_raw)
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError(f"Non-finite value at index {idx}: a={x!r}, b={y!r}")
        diff = x - y
        if not math.isfinite(diff):
            raise ValueError(f"Pairwise difference overflowed at index {idx}: {x!r} - {y!r}")
        diffs.append(diff)
    return diffs


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    """Compute linear-interpolated percentile at quantile q in [0.0, 1.0]."""
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    pos = q * (n - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    weight = pos - lo
    return sorted_vals[lo] + weight * (sorted_vals[hi] - sorted_vals[lo])


def paired_bootstrap_ci(
    a: Sequence[float],
    b: Sequence[float],
    *,
    n_resamples: int = 2000,
    alpha: float = 0.05,
    seed: int = 42,
) -> BootstrapCIResult:
    """Compute a seeded paired bootstrap confidence interval for mean(a_i - b_i).

    Guarantees `ci_low <= mean_diff <= ci_high` across all valid finite inputs,
    including degenerate constant series and small resample counts.
    """
    if n_resamples < 1:
        raise ValueError(f"n_resamples must be >= 1, got {n_resamples}")
    if not (0.0 < alpha < 1.0):
        raise ValueError(f"alpha must be strictly between 0 and 1, got {alpha}")

    diffs = _validate_paired_finite(a, b)
    n = len(diffs)
    if n == 0:
        raise ValueError("Paired sequences must not be empty")

    mean_diff = math.fsum(diffs) / n
    rng = random.Random(seed)

    boot_means: list[float] = []
    for _ in range(n_resamples):
        sample_sum = math.fsum(diffs[rng.randrange(n)] for _ in range(n))
        boot_means.append(sample_sum / n)

    boot_means.sort()
    raw_low = _percentile(boot_means, alpha / 2.0)
    raw_high = _percentile(boot_means, 1.0 - alpha / 2.0)

    ci_low = min(raw_low, mean_diff)
    ci_high = max(raw_high, mean_diff)

    return BootstrapCIResult(
        mean_diff=mean_diff,
        ci_low=ci_low,
        ci_high=ci_high,
        n_resamples=n_resamples,
        alpha=alpha,
        seed=seed,
    )


def mcnemar_exact_p(b_wins: int, c_wins: int) -> float:
    """Exact two-sided McNemar binomial p-value using math.comb.

    Guarantees `0.0 <= p <= 1.0` and exact symmetry `mcnemar_exact_p(b, c) == mcnemar_exact_p(c, b)`.
    """
    if b_wins < 0 or c_wins < 0:
        raise ValueError(f"Discordant counts must be non-negative, got b={b_wins}, c={c_wins}")
    n = b_wins + c_wins
    if n == 0:
        return 1.0
    k = min(b_wins, c_wins)
    if 2 * k == n:
        return 1.0
    tail_sum = sum(math.comb(n, i) for i in range(k + 1))
    p_value = (2 * tail_sum) / (1 << n)
    return max(0.0, min(1.0, p_value))


def wilcoxon_signed_rank(a: Sequence[float], b: Sequence[float]) -> float:
    """Two-sided Wilcoxon signed-rank test on paired differences a_i - b_i.

    - Excludes zero differences (`a_i == b_i`).
    - Uses exact permutation distribution (via subset-sum DP) for non-zero `n <= 15`.
    - Uses normal approximation with continuity correction and tie adjustment for `n > 15`.
    - Guarantees `0.0 <= p <= 1.0` and exact symmetry `wilcoxon_signed_rank(a, b) == wilcoxon_signed_rank(b, a)`.
    """
    diffs = _validate_paired_finite(a, b)
    nonzero = [d for d in diffs if d != 0.0]
    n = len(nonzero)
    if n == 0:
        return 1.0

    # Group by absolute difference to assign average 1-based ranks deterministically.
    abs_sorted = sorted((abs(d), 1 if d > 0.0 else -1) for d in nonzero)
    ranks: list[float] = []
    pos_ranks: list[float] = []
    neg_ranks: list[float] = []
    tie_counts: list[int] = []

    idx = 0
    while idx < n:
        end = idx + 1
        while end < n and abs_sorted[end][0] == abs_sorted[idx][0]:
            end += 1
        group_size = end - idx
        tie_counts.append(group_size)
        avg_rank = ((idx + 1) + end) / 2.0
        for j in range(idx, end):
            ranks.append(avg_rank)
            if abs_sorted[j][1] > 0:
                pos_ranks.append(avg_rank)
            else:
                neg_ranks.append(avg_rank)
        idx = end

    # Exact symmetry: sort each side before fsum and take min(W+, W-).
    w_plus = math.fsum(pos_ranks)
    w_minus = math.fsum(neg_ranks)
    w_stat = min(w_plus, w_minus)

    if n <= 15:
        # Multiply ranks by 2 so half-integer tied ranks become exact integers.
        int_ranks = [round(r * 2.0) for r in ranks]
        total_int = sum(int_ranks)
        target = round(w_stat * 2.0)
        dp = [0] * (total_int + 1)
        dp[0] = 1
        for r_val in int_ranks:
            for s in range(total_int, r_val - 1, -1):
                dp[s] += dp[s - r_val]
        tail_count = sum(dp[: target + 1])
        p_value = (2 * tail_count) / (1 << n)
        return max(0.0, min(1.0, p_value))

    mean_w = n * (n + 1) / 4.0
    tie_corr = sum(t * (t * t - 1) for t in tie_counts if t > 1) / 48.0
    var_w = (n * (n + 1) * (2 * n + 1) / 24.0) - tie_corr
    z_score = max(0.0, abs(w_stat - mean_w) - 0.5) / math.sqrt(var_w)
    p_value = math.erfc(z_score / math.sqrt(2.0))
    return max(0.0, min(1.0, p_value))


__all__ = [
    "mcnemar_exact_p",
    "paired_bootstrap_ci",
    "wilcoxon_signed_rank",
]
