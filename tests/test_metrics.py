import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from specpipe.metrics import bootstrap_ci, completeness, prf


def test_prf_perfect():
    m = prf({"a", "b"}, {"a", "b"})
    assert (m.precision, m.recall, m.f1) == (1.0, 1.0, 1.0)


def test_prf_partial():
    m = prf({"a", "b", "c", "d"}, {"a", "b", "x"})
    assert abs(m.precision - 2 / 3) < 1e-9
    assert abs(m.recall - 0.5) < 1e-9
    assert abs(m.f1 - 2 * (2 / 3) * 0.5 / (2 / 3 + 0.5)) < 1e-9


def test_prf_empty_gold_with_predictions_is_zero_precision():
    m = prf(set(), {"x"})
    assert m.precision == 0.0 and m.recall == 0.0  # empty gold: recall defined as 0


def test_prf_both_empty_is_perfect():
    assert prf(set(), set()) == prf({"a"}, {"a"})


def test_completeness():
    assert completeness({"security"}, {"security", "performance"}) == 0.5
    assert completeness(set(), set()) == 1.0


def test_bootstrap_ci_deterministic_and_ordering():
    a = [0.9, 0.8, 0.7, 1.0, 0.6, 0.85, 0.75, 0.95]
    b = [0.7, 0.7, 0.6, 0.8, 0.5, 0.7, 0.65, 0.8]
    m1 = bootstrap_ci(a, b, n_resamples=500, seed=7)
    m2 = bootstrap_ci(a, b, n_resamples=500, seed=7)
    assert m1 == m2
    mean_diff, low, high = m1
    assert low <= mean_diff <= high
    assert mean_diff > 0


def test_bootstrap_ci_rejects_unpaired():
    try:
        bootstrap_ci([0.1], [0.1, 0.2])
    except ValueError:
        return
    raise AssertionError("expected ValueError")
