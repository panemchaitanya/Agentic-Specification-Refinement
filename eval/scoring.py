"""Pure scoring and hypothesis-readout functions.

These consume match verdicts produced under the written rubric
(docs/evaluation.md); semantic matching itself is human/rubric work, not
code. All readout rules below are FIXED before any evaluation data exists
- changing them after seeing results would invalidate the registered-report
claim.
"""
from __future__ import annotations

from dataclasses import dataclass

from specpipe.metrics import PRF, bootstrap_ci, completeness, prf

# Fixed thresholds, verbatim from the submitted proposal's section 3.
H1_MAX_PRECISION_DROP = 0.03
H2_MAX_F1_GAP = 0.05
H3_MIN_COMPLETENESS_RATIO = 0.95
H3_MIN_QUESTION_REDUCTION = 0.50


@dataclass(frozen=True)
class ItemScore:
    prf: PRF
    n_gold: int
    n_pred: int
    n_matches: int
    many_to_one: bool  # more than one pred matched to the same gold id


def score_item(gold_ids, pred_ids, matches) -> ItemScore:
    """Score one item from rubric match verdicts.

    matches: iterable of (gold_id, pred_id) pairs the rubric declared a
    semantic match. Matching is one-to-one: only the first pair for each
    gold id counts; any further pred matched to the same gold is treated
    as unmatched (it still counts against precision) and the item is
    flagged many_to_one so the 20% hand-audit reviews those cases.

    Every predicted id that is not matched counts against precision,
    including predictions echoing suggestions annotators REJECTED: a
    rejection is an affirmative judgment, so a matching extraction is a
    genuine false positive. Documents with UNRESOLVED suggestions
    (skipped, no majority, contested edits) are not scored at all - see
    eval/gold.py::gold_doc_scorable (rubric rule R5, fail-closed).
    """
    gold = set(gold_ids)
    pred = set(pred_ids)
    pairs = [(g, p) for g, p in matches]
    for g, p in pairs:
        if g not in gold:
            raise ValueError(f"match references unknown gold id {g!r}")
        if p not in pred:
            raise ValueError(f"match references unknown predicted id {p!r}")
    matched_golds: set = set()
    matched_preds: set = set()
    for g, p in pairs:
        if g in matched_golds:
            continue  # many-to-one: extra pred falls back to unmatched
        matched_golds.add(g)
        matched_preds.add(p)
    relabeled_pred = matched_golds | (pred - matched_preds)
    m = prf(gold, relabeled_pred)
    return ItemScore(prf=m, n_gold=len(gold), n_pred=len(pred),
                     n_matches=len(matched_golds),
                     many_to_one=len(matched_golds) < len(pairs))


def nfr_kinds(requirements) -> set:
    """NFR categories present in a requirement list (FRs excluded)."""
    return {r.kind for r in requirements if getattr(r, "kind", "FR") != "FR"}


def open_items(requirements) -> list:
    """B3 placeholder lines ('OPEN: ...') - counted separately, never
    treated as satisfied requirements (rubric rule R4)."""
    return [r for r in requirements
            if getattr(r, "text", "").startswith("OPEN:")]


def open_rate(requirements) -> float:
    reqs = list(requirements)
    return len(open_items(reqs)) / len(reqs) if reqs else 0.0


def spec_completeness(requirements, implied_categories) -> float:
    """Proposal's completeness: share of PRD-implied NFR categories the
    spec covers. OPEN lines do not count as coverage."""
    covered = nfr_kinds([r for r in requirements
                         if not getattr(r, "text", "").startswith("OPEN:")])
    return completeness(covered, set(implied_categories))


def h1_readout(b3_recalls, b1_recalls, b3_precisions, b1_precisions,
               n_resamples: int = 2000, seed: int = 0) -> dict:
    """H1: B3 (pipeline, no human) vs B1 (single LLM), per-item paired.

    Fixed rule: H1 is supported iff the mean NFR-recall difference
    (B3 - B1) is positive with a paired bootstrap 95% CI above 0, AND the
    mean precision difference is >= -0.03 (drop no more than 0.03).
    """
    rd, r_lo, r_hi = bootstrap_ci(b3_recalls, b1_recalls,
                                  n_resamples=n_resamples, seed=seed)
    pd_, p_lo, p_hi = bootstrap_ci(b3_precisions, b1_precisions,
                                   n_resamples=n_resamples, seed=seed)
    supported = (r_lo > 0.0) and (pd_ >= -H1_MAX_PRECISION_DROP)
    return {
        "hypothesis": "H1",
        "recall_diff_mean": rd, "recall_ci95": [r_lo, r_hi],
        "precision_diff_mean": pd_, "precision_ci95": [p_lo, p_hi],
        "max_precision_drop_allowed": H1_MAX_PRECISION_DROP,
        "supported": supported,
        "rule": ("supported iff CI95(B3-B1 NFR recall) excludes 0 above "
                 "and mean precision drop <= 0.03; fixed pre-data"),
    }


def h2_readout(slm_f1s, llm_f1s, slm_costs, llm_costs,
               slm_latencies, llm_latencies,
               n_resamples: int = 2000, seed: int = 0) -> dict:
    """H2: SLM vs LLM critics in the same roles, per-item paired NFR F1.

    Fixed rule: H2 is supported iff the mean F1 difference (SLM - LLM) is
    >= -0.05. Cost and latency are reported as means and ratios; the
    proposal says 'much lower' without a numeric threshold, so no
    pass/fail is attached to them (documented in docs/evaluation.md).
    """
    fd, f_lo, f_hi = bootstrap_ci(slm_f1s, llm_f1s,
                                  n_resamples=n_resamples, seed=seed)
    cost_ratio = (round(sum(slm_costs) / sum(llm_costs), 6)
                  if sum(llm_costs) else None)
    lat_ratio = (round(sum(slm_latencies) / sum(llm_latencies), 6)
                 if sum(llm_latencies) else None)
    return {
        "hypothesis": "H2",
        "f1_diff_mean": fd, "f1_ci95": [f_lo, f_hi],
        "max_f1_gap_allowed": H2_MAX_F1_GAP,
        "supported": fd >= -H2_MAX_F1_GAP,
        "cost_ratio_slm_over_llm": cost_ratio,
        "latency_ratio_slm_over_llm": lat_ratio,
        "rule": ("supported iff mean SLM-LLM NFR F1 diff >= -0.05; cost/"
                 "latency reported descriptively; fixed pre-data"),
    }


def h3_eligible(gold: dict) -> bool:
    """Whether a gold item may enter the H3 completeness comparison.

    Empty implied_categories with provenance.implied_categories_verified
    true means annotators confirmed the PRD implies no NFR categories -
    the item is eligible (completeness 1.0 by definition). Empty implied
    categories WITHOUT that marker means the categories were never
    reviewed: the item is excluded from H3 aggregation (and counted
    separately), because a vacuous 1.0 would silently inflate the ratio.
    """
    if gold.get("implied_categories"):
        return True
    return bool((gold.get("provenance") or {}).get(
        "implied_categories_verified"))


def h3_readout(targeted_completeness, askall_completeness,
               targeted_questions, askall_questions) -> dict:
    """H3: targeted questioning vs ask-everything review, per-item paired.

    Fixed rule: H3 is supported iff the mean per-item completeness ratio
    (targeted / ask-everything, 1.0 when ask-everything is 0) is >= 0.95
    AND the mean per-item question reduction (1 - targeted/askall;
    1.0 when ask-everything asked 0 questions and targeted did too)
    is >= 0.50.
    """
    if not (len(targeted_completeness) == len(askall_completeness)
            == len(targeted_questions) == len(askall_questions)):
        raise ValueError("paired inputs must be equal length")
    comp_ratios = [(t / a) if a > 0 else 1.0
                   for t, a in zip(targeted_completeness, askall_completeness)]
    q_reductions = [(1.0 - t / a) if a > 0 else (1.0 if t == 0 else 0.0)
                    for t, a in zip(targeted_questions, askall_questions)]
    comp_ratio = sum(comp_ratios) / len(comp_ratios)
    q_reduction = sum(q_reductions) / len(q_reductions)
    return {
        "hypothesis": "H3",
        "completeness_ratio_mean": comp_ratio,
        "question_reduction_mean": q_reduction,
        "min_completeness_ratio": H3_MIN_COMPLETENESS_RATIO,
        "min_question_reduction": H3_MIN_QUESTION_REDUCTION,
        "supported": (comp_ratio >= H3_MIN_COMPLETENESS_RATIO
                      and q_reduction >= H3_MIN_QUESTION_REDUCTION),
        "rule": ("supported iff mean targeted/ask-all completeness ratio "
                 ">= 0.95 and mean question reduction >= 0.50; fixed pre-data"),
    }

