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


# --- Evaluation-lane extensions (eval/scoring.py, pure functions) ---------

sys.path.insert(0, str(Path(__file__).parent.parent))

from eval.scoring import (H1_MAX_PRECISION_DROP, H3_MIN_COMPLETENESS_RATIO,
                          H3_MIN_QUESTION_REDUCTION, h1_readout, h2_readout,
                          h3_readout, nfr_kinds, open_items, open_rate,
                          score_item, spec_completeness)


class _Req:
    """Minimal stand-in for specpipe.schemas.Requirement."""

    def __init__(self, kind, text):
        self.kind = kind
        self.text = text


def test_score_item_perfect_match():
    s = score_item({"G1", "G2"}, {"P1", "P2"}, [("G1", "P1"), ("G2", "P2")])
    assert (s.prf.precision, s.prf.recall, s.prf.f1) == (1.0, 1.0, 1.0)
    assert not s.many_to_one


def test_score_item_partial_and_extra_preds():
    s = score_item({"G1", "G2", "G3"}, {"P1", "P9"}, [("G1", "P1")])
    assert abs(s.prf.precision - 0.5) < 1e-9
    assert abs(s.prf.recall - 1 / 3) < 1e-9


def test_score_item_many_to_one_counts_gold_once():
    s = score_item({"G1"}, {"P1", "P2"}, [("G1", "P1"), ("G1", "P2")])
    assert s.many_to_one
    assert s.prf.recall == 1.0
    assert abs(s.prf.precision - 0.5) < 1e-9


def test_score_item_rejects_unknown_ids():
    for matches in ([("GX", "P1")], [("G1", "PX")]):
        try:
            score_item({"G1"}, {"P1"}, matches)
        except ValueError:
            continue
        raise AssertionError("expected ValueError")


def test_open_items_and_rate():
    reqs = [_Req("security", "OPEN: load unspecified - add target"),
            _Req("FR", "Users can log in."),
            _Req("performance", "p95 under 2s.")]
    assert len(open_items(reqs)) == 1
    assert abs(open_rate(reqs) - 1 / 3) < 1e-9
    assert open_rate([]) == 0.0


def test_spec_completeness_excludes_open_lines():
    reqs = [_Req("security", "OPEN: missing - suggestion"),
            _Req("performance", "p95 under 2s.")]
    assert spec_completeness(reqs, {"security", "performance"}) == 0.5
    assert spec_completeness(reqs, {"performance"}) == 1.0
    assert spec_completeness(reqs, set()) == 1.0


def test_nfr_kinds_excludes_fr():
    reqs = [_Req("FR", "x"), _Req("security", "y"), _Req("usability", "z")]
    assert nfr_kinds(reqs) == {"security", "usability"}


def test_h1_readout_supported_when_recall_up_and_precision_held():
    out = h1_readout(b3_recalls=[0.8, 0.9, 0.7, 0.85],
                     b1_recalls=[0.5, 0.6, 0.5, 0.6],
                     b3_precisions=[0.7, 0.7, 0.7, 0.7],
                     b1_precisions=[0.72, 0.72, 0.72, 0.72],
                     n_resamples=500, seed=3)
    assert out["supported"]
    assert out["recall_diff_mean"] > 0
    assert out["precision_diff_mean"] >= -H1_MAX_PRECISION_DROP


def test_h1_readout_fails_on_excess_precision_drop():
    out = h1_readout(b3_recalls=[0.8, 0.9, 0.7, 0.85],
                     b1_recalls=[0.5, 0.6, 0.5, 0.6],
                     b3_precisions=[0.5, 0.5, 0.5, 0.5],
                     b1_precisions=[0.7, 0.7, 0.7, 0.7],
                     n_resamples=500, seed=3)
    assert not out["supported"]
    assert out["precision_diff_mean"] < -H1_MAX_PRECISION_DROP


def test_h1_readout_fails_without_recall_gain():
    same = [0.6, 0.7, 0.65, 0.7]
    out = h1_readout(same, list(same), same, list(same),
                     n_resamples=500, seed=3)
    assert not out["supported"]
    assert out["recall_ci95"][0] <= 0 <= out["recall_ci95"][1]


def test_h2_readout_within_gap_and_cost_reported():
    out = h2_readout(slm_f1s=[0.70, 0.72, 0.68], llm_f1s=[0.73, 0.74, 0.72],
                     slm_costs=[10, 10, 10], llm_costs=[100, 100, 100],
                     slm_latencies=[0.2, 0.2, 0.2],
                     llm_latencies=[1.0, 1.0, 1.0],
                     n_resamples=500, seed=5)
    assert out["supported"]  # mean diff -0.03 >= -0.05
    assert out["cost_ratio_slm_over_llm"] == 0.1
    assert out["latency_ratio_slm_over_llm"] == 0.2


def test_h2_readout_fails_beyond_gap():
    out = h2_readout(slm_f1s=[0.5, 0.5], llm_f1s=[0.7, 0.7],
                     slm_costs=[1, 1], llm_costs=[1, 1],
                     slm_latencies=[1, 1], llm_latencies=[1, 1],
                     n_resamples=200, seed=5)
    assert not out["supported"]


def test_h3_readout_supported():
    out = h3_readout(targeted_completeness=[1.0, 1.0, 0.5, 1.0],
                     askall_completeness=[1.0, 1.0, 0.5, 1.0],
                     targeted_questions=[2, 1, 2, 1],
                     askall_questions=[5, 4, 6, 3])
    assert out["supported"]
    assert out["completeness_ratio_mean"] == 1.0
    assert out["question_reduction_mean"] >= H3_MIN_QUESTION_REDUCTION


def test_h3_readout_fails_on_completeness_loss():
    out = h3_readout(targeted_completeness=[0.5, 0.5, 0.5, 0.5],
                     askall_completeness=[1.0, 1.0, 1.0, 1.0],
                     targeted_questions=[1, 1, 1, 1],
                     askall_questions=[5, 5, 5, 5])
    assert not out["supported"]
    assert out["completeness_ratio_mean"] < H3_MIN_COMPLETENESS_RATIO


def test_h3_readout_zero_askall_questions_edge():
    out = h3_readout(targeted_completeness=[1.0], askall_completeness=[1.0],
                     targeted_questions=[0], askall_questions=[0])
    assert out["completeness_ratio_mean"] == 1.0
    assert out["question_reduction_mean"] == 1.0


def test_h3_readout_rejects_unpaired():
    try:
        h3_readout([1.0], [1.0, 1.0], [0], [0])
    except ValueError:
        return
    raise AssertionError("expected ValueError")
