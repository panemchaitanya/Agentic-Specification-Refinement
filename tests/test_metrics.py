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


# --- eval/summarize.py and eval/audit.py ----------------------------------

from eval.summarize import load_run_log, summarize_runs, score_run
from eval.audit import sample_matches_for_audit


def _run(baseline="B3", item="it1", repeat=0, **kw):
    rec = {"type": "run", "run_id": "r", "baseline": baseline,
           "item_id": item, "repeat": repeat, "n_requirements": 6,
           "n_fr": 3, "n_nfr": 3, "open_rate": 0.5, "questions_asked": 2,
           "llm": {"calls": 4, "total_latency_s": 0.1,
                   "total_chars_in": 100, "total_chars_out": 50}}
    rec.update(kw)
    return rec


def test_summarize_runs_means_over_repeats():
    runs = [_run(repeat=0, open_rate=0.0), _run(repeat=1, open_rate=1.0)]
    out = summarize_runs(runs)
    agg = out["B3/it1"]
    assert agg["repeats"] == 2
    assert agg["open_rate"] == 0.5
    assert agg["mean_llm_calls"] == 4
    assert agg["completeness"] is None  # not present in records


def test_score_run_pairs_verdicts_with_gold():
    gold = {"item_id": "it1", "implied_categories": ["security"],
            "gold_requirements": [
                {"id": "G1", "kind": "security", "text": "x"},
                {"id": "G2", "kind": "performance", "text": "y"}]}
    verdicts = [{"item_id": "it1", "run_id": "r", "pred_ids": ["P1", "P2"],
                 "matches": [["G1", "P1"]], "audited": True}]
    out = score_run([_run()], gold, verdicts)
    assert out["it1"]["recall"] == 0.5
    assert out["it1"]["precision"] == 0.5
    assert out["it1"]["audited"] is True


def test_score_run_requires_pred_ids():
    gold = {"item_id": "it1", "implied_categories": [],
            "gold_requirements": [{"id": "G1", "kind": "FR", "text": "x"}]}
    try:
        score_run([_run()], gold,
                  [{"item_id": "it1", "run_id": "r", "matches": []}])
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_audit_sample_deterministic_and_covers_items():
    verdicts = [
        {"item_id": "a", "run_id": "r", "matches": [["G1", "P1"], ["G2", "P2"]]},
        {"item_id": "b", "run_id": "r", "matches": [["G1", "P9"]]},
    ]
    s1 = sample_matches_for_audit(verdicts, fraction=0.2, seed=11)
    s2 = sample_matches_for_audit(verdicts, fraction=0.2, seed=11)
    assert s1 == s2
    assert {e[0] for e in s1} == {"a", "b"}  # at least one per item
    assert sample_matches_for_audit([], fraction=0.2) == []


# --- eval/report.py ---------------------------------------------------------

from eval.report import deviation_flags, nfr_prf_table, paired, readout_block


def _scored(items, p=0.7, r=0.6, f=0.65):
    return {i: {"precision": p, "recall": r, "f1": f} for i in items}


def test_paired_intersects_and_sorts():
    items, a, b = paired(_scored(["x", "y", "z"], r=0.5),
                         _scored(["y", "z", "w"], r=0.9), "recall")
    assert items == ["y", "z"]
    assert a == [0.5, 0.5] and b == [0.9, 0.9]


def test_nfr_prf_table_renders_means():
    table = nfr_prf_table({"B1": _scored(["a", "b"], p=0.5, r=0.4, f=0.44)})
    assert "| B1 | 2 | 0.500 | 0.400 | 0.440 |" in table
    assert table.startswith("| Baseline |")


def test_readout_block_marks_support():
    block = readout_block({"hypothesis": "H1", "supported": False,
                           "rule": "fixed", "recall_diff_mean": 0.123456})
    assert "NOT supported" in block
    assert "0.1235" in block


def test_deviation_flags_d1_and_conflict_share():
    runs = [
        {"baseline": "B3T", "item_id": "i1", "repeat": 0,
         "n_escalated": 7, "questions_asked": 5,
         "finding_kinds": {"vague": 3, "missing_context": 7}},
        {"baseline": "B1", "item_id": "i1", "repeat": 0,
         "n_escalated": 0, "questions_asked": 0,
         "finding_kinds": {"vague": 1}},
    ]
    flags = deviation_flags(runs)
    assert flags["d1_items"] == ["B3T/i1/rep0"]
    assert flags["finding_kind_totals"] == {"vague": 4, "missing_context": 7}
    assert flags["conflict_share"] == 0.0


# --- eval/validity.py -------------------------------------------------------

from eval.validity import (finding_trace_verbatim_rate,
                           fr_trace_verbatim_rate, verbatim_in)


class _Traced:
    def __init__(self, trace_kind, trace_ref):
        self.trace_kind = trace_kind
        self.trace_ref = trace_ref


def test_verbatim_in_whitespace_normalized():
    assert verbatim_in("The app   must be fast.\nDone.", "the app must be fast."[0:0] + "app must be fast.")
    assert not verbatim_in("The app must be fast.", "app must be slow.")
    assert not verbatim_in("anything", "")


def test_fr_trace_verbatim_rate():
    reqs = [_Traced("prd_sentence", "Users can log in."),
            _Traced("prd_sentence", "invented citation"),
            _Traced("critic", "security")]
    assert fr_trace_verbatim_rate(reqs, "Users can log in. It is blue.") == 0.5
    assert fr_trace_verbatim_rate([_Traced("critic", "x")], "p") is None


class _Finding:
    def __init__(self, prd_sentence):
        self.prd_sentence = prd_sentence


def test_finding_trace_verbatim_rate():
    findings = [_Finding("store user data"), _Finding("not in prd")]
    assert finding_trace_verbatim_rate(findings, "We store user data.") == 0.5
    # case-changed quotes are not verbatim
    assert finding_trace_verbatim_rate([_Finding("Store user data")],
                                       "We store user data.") == 0.0
    assert finding_trace_verbatim_rate([], "p") is None
