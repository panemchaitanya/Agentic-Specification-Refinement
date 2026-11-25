"""Fail-closed H1-H4 empirical hypothesis evaluator with strict mock/tamper quarantine."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from reqhq.analysis.models import (
    BootstrapCIResult,
    HypothesisResult,
    HypothesisVerdictStatus,
    ParetoPoint,
)
from reqhq.analysis.stats import (
    mcnemar_exact_p,
    paired_bootstrap_ci,
    wilcoxon_signed_rank,
)

_REQUIRED_LAYERS: tuple[int, ...] = (1, 2, 3, 4)
_ALPHA = 0.05


class MockRunQuarantineError(RuntimeError):
    """Raised when `strict=True` and an evaluation bundle fails live-verdict quarantine gates."""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().lower()


def _mean(values: list[float]) -> float:
    return math.fsum(values) / len(values) if values else 0.0


def _match_layer_number(key: str, filename: str) -> int | None:
    lower_key = key.strip().lower()
    lower_name = filename.strip().lower()
    for layer_num in _REQUIRED_LAYERS:
        token = f"layer{layer_num}"
        if lower_key in (str(layer_num), token, f"{token}.jsonl") or token in lower_name:
            return layer_num
    return None


def _load_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except ValueError as exc:
            errors.append(f"Invalid JSON in {path.name}:{line_no}: {exc}")
            continue
        if not isinstance(parsed, dict):
            errors.append(f"Non-object JSON line in {path.name}:{line_no}")
            continue
        records.append(parsed)
    return records, errors


def _quarantined_hypotheses(reasons: list[str]) -> list[HypothesisResult]:
    reason_text = "QUARANTINED: " + "; ".join(reasons)
    titles = (
        ("H1", "Iterative Gap Elicitation & Question Efficiency"),
        ("H2", "Local-First Quality Parity & >=80% Cloud Reduction"),
        ("H3", "Alloy Formal Verification & Socio-Technical Routing Quality"),
        ("H4", "Downstream Code-Gen: Cond C > Cond A and Cond C > Cond B"),
    )
    return [
        HypothesisResult(
            hypothesis_id=hid,
            title=title,
            status=HypothesisVerdictStatus.QUARANTINED_MOCK_OR_INCOMPLETE,
            metrics={},
            p_values={},
            bootstrap_cis={},
            rationale=reason_text,
        )
        for hid, title in titles
    ]


def _evaluate_h1(
    l1_records: list[dict[str, Any]],
    *,
    seed: int,
    n_resamples: int,
) -> HypothesisResult:
    title = "Iterative Gap Elicitation & Question Efficiency"
    if not l1_records:
        return HypothesisResult(
            hypothesis_id="H1",
            title=title,
            status=HypothesisVerdictStatus.QUARANTINED_MOCK_OR_INCOMPLETE,
            rationale="Missing Layer 1 records for H1 evaluation.",
        )

    iter_recalls = [float(r["iterative_recall"]) for r in l1_records]
    single_recalls = [float(r["single_pass_recall"]) for r in l1_records]
    questions = [float(r["questions_asked"]) for r in l1_records]
    baselines = [float(r["exhaustive_baseline_questions"]) for r in l1_records]

    recall_ci = paired_bootstrap_ci(
        iter_recalls, single_recalls, n_resamples=n_resamples, alpha=_ALPHA, seed=seed
    )
    recall_p = wilcoxon_signed_rank(iter_recalls, single_recalls)
    questions_ci = paired_bootstrap_ci(
        questions, baselines, n_resamples=n_resamples, alpha=_ALPHA, seed=seed + 1
    )
    questions_p = wilcoxon_signed_rank(questions, baselines)

    mean_iter = _mean(iter_recalls)
    mean_single = _mean(single_recalls)
    mean_q = _mean(questions)
    mean_base = _mean(baselines)

    recall_dir = mean_iter > mean_single
    questions_dir = mean_q < mean_base
    recall_sig = recall_ci.ci_low > 0.0 or recall_p < _ALPHA
    questions_sig = questions_ci.ci_high < 0.0 or questions_p < _ALPHA

    if not (recall_dir and questions_dir):
        status = HypothesisVerdictStatus.REFUTED
        rationale = (
            f"Directional threshold failed: iterative_recall={mean_iter:.3f} vs "
            f"single_pass_recall={mean_single:.3f}; questions={mean_q:.2f} vs "
            f"exhaustive={mean_base:.2f}."
        )
    elif recall_sig and questions_sig:
        status = HypothesisVerdictStatus.SUPPORTED
        rationale = (
            f"Iterative recall ({mean_iter:.3f}) significantly exceeds single-pass "
            f"({mean_single:.3f}, p={recall_p:.4f}) with fewer questions "
            f"({mean_q:.2f} < {mean_base:.2f}, p={questions_p:.4f})."
        )
    else:
        status = HypothesisVerdictStatus.INCONCLUSIVE
        rationale = (
            f"Point estimates favor H1 (recall {mean_iter:.3f} > {mean_single:.3f}, "
            f"questions {mean_q:.2f} < {mean_base:.2f}) without statistical significance "
            f"(recall_p={recall_p:.4f}, questions_p={questions_p:.4f})."
        )

    return HypothesisResult(
        hypothesis_id="H1",
        title=title,
        status=status,
        metrics={
            "n_items": len(l1_records),
            "mean_iterative_recall": round(mean_iter, 6),
            "mean_single_pass_recall": round(mean_single, 6),
            "mean_questions_asked": round(mean_q, 6),
            "mean_exhaustive_baseline_questions": round(mean_base, 6),
        },
        p_values={
            "recall_wilcoxon_p": recall_p,
            "questions_wilcoxon_p": questions_p,
        },
        bootstrap_cis={
            "recall_diff": recall_ci,
            "questions_diff": questions_ci,
        },
        rationale=rationale,
    )


def _compute_reduction_pct(used: float, baseline: float) -> float:
    if baseline <= 0.0:
        return 0.0
    return max(0.0, min(100.0, (1.0 - (used / baseline)) * 100.0))


def _evaluate_h2(
    l1_records: list[dict[str, Any]],
    l4_records: list[dict[str, Any]],
    *,
    seed: int,
    n_resamples: int,
) -> HypothesisResult:
    title = "Local-First Quality Parity & >=80% Cloud Reduction"
    if not l1_records or not l4_records:
        return HypothesisResult(
            hypothesis_id="H2",
            title=title,
            status=HypothesisVerdictStatus.QUARANTINED_MOCK_OR_INCOMPLETE,
            rationale="Missing Layer 1 or Layer 4 records for H2 evaluation.",
        )

    local_f1s = [float(r["iterative_f1"]) for r in l1_records]
    cloud_f1s = [float(r["all_cloud_f1"]) for r in l1_records]
    f1_ci = paired_bootstrap_ci(
        local_f1s, cloud_f1s, n_resamples=n_resamples, alpha=_ALPHA, seed=seed + 2
    )
    f1_p = wilcoxon_signed_rank(local_f1s, cloud_f1s)

    mean_local_f1 = _mean(local_f1s)
    mean_cloud_f1 = _mean(cloud_f1s)

    l4_base_req = math.fsum(float(r.get("all_cloud_baseline_requests", 0.0)) for r in l4_records)
    l4_used_req = math.fsum(float(r.get("cloud_requests", 0.0)) for r in l4_records)
    l4_base_tok = math.fsum(float(r.get("all_cloud_baseline_tokens", 0.0)) for r in l4_records)
    l4_used_tok = math.fsum(float(r.get("cloud_tokens", 0.0)) for r in l4_records)

    if l4_base_req > 0.0:
        req_red_pct = _compute_reduction_pct(l4_used_req, l4_base_req)
    else:
        req_red_pct = _compute_reduction_pct(
            math.fsum(float(r.get("cloud_requests", 0.0)) for r in l1_records),
            math.fsum(float(r.get("all_cloud_requests", 0.0)) for r in l1_records),
        )

    if l4_base_tok > 0.0:
        tok_red_pct = _compute_reduction_pct(l4_used_tok, l4_base_tok)
    else:
        tok_red_pct = _compute_reduction_pct(
            math.fsum(float(r.get("cloud_tokens", 0.0)) for r in l1_records),
            math.fsum(float(r.get("all_cloud_tokens", 0.0)) for r in l1_records),
        )

    f1_parity = mean_local_f1 >= (mean_cloud_f1 - 0.05)
    req_ok = req_red_pct >= 80.0
    tok_ok = tok_red_pct >= 80.0

    if not (f1_parity and req_ok and tok_ok):
        status = HypothesisVerdictStatus.REFUTED
        rationale = (
            f"Threshold missed: local_f1={mean_local_f1:.3f} vs cloud_f1-0.05="
            f"{mean_cloud_f1 - 0.05:.3f}; req_reduction={req_red_pct:.1f}%, "
            f"tok_reduction={tok_red_pct:.1f}% (target >= 80.0%)."
        )
    elif f1_ci.ci_low >= -0.05:
        status = HypothesisVerdictStatus.SUPPORTED
        rationale = (
            f"Local-first F1 ({mean_local_f1:.3f}) stays within 0.05 of all-cloud "
            f"({mean_cloud_f1:.3f}, CI low={f1_ci.ci_low:.4f}) while cutting cloud "
            f"requests by {req_red_pct:.1f}% and tokens by {tok_red_pct:.1f}%."
        )
    else:
        status = HypothesisVerdictStatus.INCONCLUSIVE
        rationale = (
            f"Point estimates meet H2 (F1 diff={f1_ci.mean_diff:.4f}, req_red={req_red_pct:.1f}%, "
            f"tok_red={tok_red_pct:.1f}%), but bootstrap CI lower bound ({f1_ci.ci_low:.4f}) "
            f"crosses -0.05."
        )

    return HypothesisResult(
        hypothesis_id="H2",
        title=title,
        status=status,
        metrics={
            "mean_local_first_f1": round(mean_local_f1, 6),
            "mean_all_cloud_f1": round(mean_cloud_f1, 6),
            "f1_diff": round(mean_local_f1 - mean_cloud_f1, 6),
            "cloud_request_reduction_pct": round(req_red_pct, 4),
            "cloud_token_reduction_pct": round(tok_red_pct, 4),
        },
        p_values={"f1_wilcoxon_p": f1_p},
        bootstrap_cis={"f1_diff": f1_ci},
        rationale=rationale,
    )


def _evaluate_h3(
    l2_records: list[dict[str, Any]],
    *,
    seed: int,
    n_resamples: int,
) -> HypothesisResult:
    title = "Alloy Formal Verification & Socio-Technical Routing Quality"
    if not l2_records:
        return HypothesisResult(
            hypothesis_id="H3",
            title=title,
            status=HypothesisVerdictStatus.QUARANTINED_MOCK_OR_INCOMPLETE,
            rationale="Missing Layer 2 records for H3 evaluation.",
        )

    alloy_total = sum(int(r["alloy_contradictions_caught"]) for r in l2_records)
    llm_total = sum(int(r["llm_only_contradictions_caught"]) for r in l2_records)
    b_wins = sum(
        1
        for r in l2_records
        if bool(r["alloy_caught_binary"]) and not bool(r["llm_only_caught_binary"])
    )
    c_wins = sum(
        1
        for r in l2_records
        if not bool(r["alloy_caught_binary"]) and bool(r["llm_only_caught_binary"])
    )
    contradictions_p = mcnemar_exact_p(b_wins, c_wins)

    routing_scores = [float(r["routing_adr_rubric_score"]) for r in l2_records]
    sw_scores = [float(r["software_only_rubric_score"]) for r in l2_records]
    rubric_ci = paired_bootstrap_ci(
        routing_scores, sw_scores, n_resamples=n_resamples, alpha=_ALPHA, seed=seed + 3
    )
    rubric_p = wilcoxon_signed_rank(routing_scores, sw_scores)

    mean_routing = _mean(routing_scores)
    mean_sw = _mean(sw_scores)

    alloy_dir = alloy_total > llm_total
    rubric_dir = mean_routing > mean_sw
    rubric_sig = rubric_ci.ci_low > 0.0 or rubric_p < _ALPHA

    if not (alloy_dir and rubric_dir):
        status = HypothesisVerdictStatus.REFUTED
        rationale = (
            f"Directional threshold failed: alloy_contradictions={alloy_total} vs "
            f"llm_only={llm_total}; routing_rubric={mean_routing:.3f} vs "
            f"software_only={mean_sw:.3f}."
        )
    elif contradictions_p < _ALPHA and rubric_sig:
        status = HypothesisVerdictStatus.SUPPORTED
        rationale = (
            f"Alloy catches significantly more contradictions ({alloy_total} > {llm_total}, "
            f"McNemar p={contradictions_p:.4f}) and socio-technical routing improves ADR "
            f"rubric ({mean_routing:.3f} > {mean_sw:.3f}, p={rubric_p:.4f})."
        )
    else:
        status = HypothesisVerdictStatus.INCONCLUSIVE
        rationale = (
            f"Point estimates favor H3 (Alloy {alloy_total} > {llm_total}, rubric "
            f"{mean_routing:.3f} > {mean_sw:.3f}) without full significance "
            f"(mcnemar_p={contradictions_p:.4f}, rubric_p={rubric_p:.4f})."
        )

    return HypothesisResult(
        hypothesis_id="H3",
        title=title,
        status=status,
        metrics={
            "n_cases": len(l2_records),
            "alloy_contradictions_caught": alloy_total,
            "llm_only_contradictions_caught": llm_total,
            "mcnemar_b_wins": b_wins,
            "mcnemar_c_wins": c_wins,
            "mean_routing_adr_rubric_score": round(mean_routing, 6),
            "mean_software_only_rubric_score": round(mean_sw, 6),
        },
        p_values={
            "contradictions_mcnemar_p": contradictions_p,
            "rubric_wilcoxon_p": rubric_p,
        },
        bootstrap_cis={"rubric_diff": rubric_ci},
        rationale=rationale,
    )


def _evaluate_h4(
    l3_records: list[dict[str, Any]],
    *,
    seed: int,
    n_resamples: int,
) -> tuple[HypothesisResult, dict[str, dict[str, float]]]:
    title = "Downstream Code-Gen: Cond C > Cond A and Cond C > Cond B"
    by_cond: dict[str, dict[tuple[str, int], dict[str, Any]]] = {
        "raw_prd": {},
        "single_pass_nl_spec": {},
        "reqhq_full_bundle": {},
    }
    cond_lists: dict[str, list[dict[str, Any]]] = {
        "raw_prd": [],
        "single_pass_nl_spec": [],
        "reqhq_full_bundle": [],
    }
    for rec in l3_records:
        cond = str(rec.get("condition", ""))
        if cond in by_cond:
            key = (str(rec["task_id"]), int(rec["replicate"]))
            by_cond[cond][key] = rec
            cond_lists[cond].append(rec)

    cond_metrics: dict[str, dict[str, float]] = {}
    for cond, recs in cond_lists.items():
        cond_metrics[cond] = {
            "hidden_pass_at_1": _mean([1.0 if bool(r["hidden_pass_at_1"]) else 0.0 for r in recs]),
            "pbt_pass_rate": _mean([float(r["pbt_pass_rate"]) for r in recs]),
            "mutation_score": _mean([float(r["mutation_score"]) for r in recs]),
            "wall_clock_sec": _mean([float(r.get("wall_clock_sec", 0.0)) for r in recs]),
            "cloud_requests": _mean([float(r.get("cloud_requests", 0.0)) for r in recs]),
        }

    keys_ca = sorted(set(by_cond["reqhq_full_bundle"]) & set(by_cond["raw_prd"]))
    keys_cb = sorted(set(by_cond["reqhq_full_bundle"]) & set(by_cond["single_pass_nl_spec"]))

    if not keys_ca or not keys_cb:
        return (
            HypothesisResult(
                hypothesis_id="H4",
                title=title,
                status=HypothesisVerdictStatus.QUARANTINED_MOCK_OR_INCOMPLETE,
                rationale="Missing paired Layer 3 records across Conditions A, B, and C.",
            ),
            cond_metrics,
        )

    def _compare_pair(
        keys: list[tuple[str, int]],
        other_cond: str,
        seed_offset: int,
    ) -> dict[str, Any]:
        c_pass = [bool(by_cond["reqhq_full_bundle"][k]["hidden_pass_at_1"]) for k in keys]
        o_pass = [bool(by_cond[other_cond][k]["hidden_pass_at_1"]) for k in keys]
        b_wins = sum(1 for cp, op in zip(c_pass, o_pass) if cp and not op)
        c_wins = sum(1 for cp, op in zip(c_pass, o_pass) if not cp and op)
        pass_p = mcnemar_exact_p(b_wins, c_wins)

        c_pbt = [float(by_cond["reqhq_full_bundle"][k]["pbt_pass_rate"]) for k in keys]
        o_pbt = [float(by_cond[other_cond][k]["pbt_pass_rate"]) for k in keys]
        pbt_ci = paired_bootstrap_ci(
            c_pbt, o_pbt, n_resamples=n_resamples, alpha=_ALPHA, seed=seed + seed_offset
        )
        pbt_p = wilcoxon_signed_rank(c_pbt, o_pbt)

        c_mut = [float(by_cond["reqhq_full_bundle"][k]["mutation_score"]) for k in keys]
        o_mut = [float(by_cond[other_cond][k]["mutation_score"]) for k in keys]
        mut_ci = paired_bootstrap_ci(
            c_mut, o_mut, n_resamples=n_resamples, alpha=_ALPHA, seed=seed + seed_offset + 1
        )
        mut_p = wilcoxon_signed_rank(c_mut, o_mut)

        return {
            "pass_rate_c": _mean([1.0 if v else 0.0 for v in c_pass]),
            "pass_rate_other": _mean([1.0 if v else 0.0 for v in o_pass]),
            "pass_mcnemar_p": pass_p,
            "pbt_mean_c": _mean(c_pbt),
            "pbt_mean_other": _mean(o_pbt),
            "pbt_ci": pbt_ci,
            "pbt_p": pbt_p,
            "mut_mean_c": _mean(c_mut),
            "mut_mean_other": _mean(o_mut),
            "mut_ci": mut_ci,
            "mut_p": mut_p,
        }

    ca = _compare_pair(keys_ca, "raw_prd", 10)
    cb = _compare_pair(keys_cb, "single_pass_nl_spec", 20)

    all_dir_ok = (
        ca["pass_rate_c"] > ca["pass_rate_other"]
        and cb["pass_rate_c"] > cb["pass_rate_other"]
        and ca["pbt_mean_c"] > ca["pbt_mean_other"]
        and cb["pbt_mean_c"] > cb["pbt_mean_other"]
        and ca["mut_mean_c"] > ca["mut_mean_other"]
        and cb["mut_mean_c"] > cb["mut_mean_other"]
    )
    all_sig_ok = (
        ca["pass_mcnemar_p"] < _ALPHA
        and cb["pass_mcnemar_p"] < _ALPHA
        and (ca["pbt_ci"].ci_low > 0.0 or ca["pbt_p"] < _ALPHA)
        and (cb["pbt_ci"].ci_low > 0.0 or cb["pbt_p"] < _ALPHA)
        and (ca["mut_ci"].ci_low > 0.0 or ca["mut_p"] < _ALPHA)
        and (cb["mut_ci"].ci_low > 0.0 or cb["mut_p"] < _ALPHA)
    )

    if not all_dir_ok:
        status = HypothesisVerdictStatus.REFUTED
        rationale = (
            f"Cond C did not beat both Cond A and Cond B on all 3 metrics "
            f"(Pass@1 C={ca['pass_rate_c']:.2f}, A={ca['pass_rate_other']:.2f}, "
            f"B={cb['pass_rate_other']:.2f})."
        )
    elif all_sig_ok:
        status = HypothesisVerdictStatus.SUPPORTED
        rationale = (
            f"Cond C significantly outperforms Cond A and Cond B on Hidden Pass@1 "
            f"(p_CA={ca['pass_mcnemar_p']:.4f}, p_CB={cb['pass_mcnemar_p']:.4f}), "
            f"PBT pass rate, and mutation score."
        )
    else:
        status = HypothesisVerdictStatus.INCONCLUSIVE
        rationale = (
            f"Cond C beats Cond A and Cond B directionally across all 3 metrics, "
            f"but not all paired tests reach p < {_ALPHA}."
        )

    bootstrap_cis: dict[str, BootstrapCIResult] = {
        "pbt_c_vs_a": ca["pbt_ci"],
        "pbt_c_vs_b": cb["pbt_ci"],
        "mutation_c_vs_a": ca["mut_ci"],
        "mutation_c_vs_b": cb["mut_ci"],
    }
    return (
        HypothesisResult(
            hypothesis_id="H4",
            title=title,
            status=status,
            metrics={
                "n_pairs_c_vs_a": len(keys_ca),
                "n_pairs_c_vs_b": len(keys_cb),
                "cond_a_pass_at_1": round(cond_metrics["raw_prd"]["hidden_pass_at_1"], 6),
                "cond_b_pass_at_1": round(cond_metrics["single_pass_nl_spec"]["hidden_pass_at_1"], 6),
                "cond_c_pass_at_1": round(cond_metrics["reqhq_full_bundle"]["hidden_pass_at_1"], 6),
                "cond_a_pbt_pass_rate": round(cond_metrics["raw_prd"]["pbt_pass_rate"], 6),
                "cond_b_pbt_pass_rate": round(cond_metrics["single_pass_nl_spec"]["pbt_pass_rate"], 6),
                "cond_c_pbt_pass_rate": round(cond_metrics["reqhq_full_bundle"]["pbt_pass_rate"], 6),
                "cond_a_mutation_score": round(cond_metrics["raw_prd"]["mutation_score"], 6),
                "cond_b_mutation_score": round(cond_metrics["single_pass_nl_spec"]["mutation_score"], 6),
                "cond_c_mutation_score": round(cond_metrics["reqhq_full_bundle"]["mutation_score"], 6),
            },
            p_values={
                "pass_at_1_c_vs_a_mcnemar_p": ca["pass_mcnemar_p"],
                "pass_at_1_c_vs_b_mcnemar_p": cb["pass_mcnemar_p"],
                "pbt_c_vs_a_wilcoxon_p": ca["pbt_p"],
                "pbt_c_vs_b_wilcoxon_p": cb["pbt_p"],
                "mutation_c_vs_a_wilcoxon_p": ca["mut_p"],
                "mutation_c_vs_b_wilcoxon_p": cb["mut_p"],
            },
            bootstrap_cis=bootstrap_cis,
            rationale=rationale,
        ),
        cond_metrics,
    )


def _build_pareto_points(
    l1_records: list[dict[str, Any]],
    cond_metrics: dict[str, dict[str, float]],
    l3_present_conds: set[str],
) -> tuple[list[ParetoPoint], list[ParetoPoint]]:
    cost_points: list[ParetoPoint] = []
    latency_points: list[ParetoPoint] = []

    cond_labels = (
        ("raw_prd", "Cond A (Raw PRD)"),
        ("single_pass_nl_spec", "Cond B (Single-Pass NL)"),
        ("reqhq_full_bundle", "Cond C (reqhq Full Bundle)"),
    )
    for key, label in cond_labels:
        if key in l3_present_conds:
            cm = cond_metrics[key]
            composite_q = (
                cm["hidden_pass_at_1"] + cm["pbt_pass_rate"] + cm["mutation_score"]
            ) / 3.0
            cost_points.append(
                ParetoPoint(label=label, quality=round(composite_q, 6), cost=round(cm["cloud_requests"], 6))
            )
            latency_points.append(
                ParetoPoint(label=label, quality=round(composite_q, 6), cost=round(cm["wall_clock_sec"], 6))
            )

    if l1_records:
        local_q = _mean([float(r["iterative_f1"]) for r in l1_records])
        cloud_q = _mean([float(r["all_cloud_f1"]) for r in l1_records])
        local_req = _mean([float(r.get("cloud_requests", 0.0)) for r in l1_records])
        cloud_req = _mean([float(r.get("all_cloud_requests", 0.0)) for r in l1_records])
        local_lat = _mean([float(r.get("wall_clock_sec", 0.0)) for r in l1_records])

        cost_points.append(
            ParetoPoint(label="Layer1 Local-First", quality=round(local_q, 6), cost=round(local_req, 6))
        )
        cost_points.append(
            ParetoPoint(label="Layer1 All-Cloud", quality=round(cloud_q, 6), cost=round(cloud_req, 6))
        )
        latency_points.append(
            ParetoPoint(label="Layer1 Local-First", quality=round(local_q, 6), cost=round(local_lat, 6))
        )

    return cost_points, latency_points


def evaluate_hypotheses(
    eval_dir: Path | str,
    *,
    strict: bool = False,
    seed: int = 42,
    n_resamples: int = 2000,
) -> tuple[bool, list[str], list[HypothesisResult], dict[str, Any]]:
    """Evaluate H1-H4 fail-closed against `eval_dir/eval_manifest.json` and Layer 1-4 JSONLs.

    Returns `(quarantined, quarantine_reasons, hypotheses, context_dict)`.
    When `strict=True` and ANY quarantine condition triggers, raises `MockRunQuarantineError`.
    """
    eval_path = Path(eval_dir)
    manifest_path = eval_path / "eval_manifest.json"
    quarantine_reasons: list[str] = []

    if not manifest_path.is_file():
        quarantine_reasons.append(f"Missing eval_manifest.json in {eval_path}")
        if strict:
            raise MockRunQuarantineError("; ".join(quarantine_reasons))
        empty_ctx: dict[str, Any] = {
            "run_id": "unknown",
            "run_mode": "unknown",
            "seed": seed,
            "replicates": 0,
            "hardware_profile": {},
            "layer_metrics": {},
            "cond_metrics": {},
            "pareto_cost_points": [],
            "pareto_latency_points": [],
        }
        return True, quarantine_reasons, _quarantined_hypotheses(quarantine_reasons), empty_ctx

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        quarantine_reasons.append(f"Corrupt eval_manifest.json: {exc}")
        if strict:
            raise MockRunQuarantineError("; ".join(quarantine_reasons))
        corrupt_ctx: dict[str, Any] = {
            "run_id": "unknown",
            "run_mode": "unknown",
            "seed": seed,
            "replicates": 0,
            "hardware_profile": {},
            "layer_metrics": {},
            "cond_metrics": {},
            "pareto_cost_points": [],
            "pareto_latency_points": [],
        }
        return True, quarantine_reasons, _quarantined_hypotheses(quarantine_reasons), corrupt_ctx

    run_id = str(manifest.get("run_id") or "unknown")
    run_mode = str(manifest.get("run_mode") or "unknown").strip().lower()
    replicates = int(manifest.get("replicates", 0))
    gold_verified = manifest.get("gold_verified")
    layers_completed = manifest.get("layers_completed", [])
    manifest_seed = int(manifest.get("seed", seed))
    hardware_profile = manifest.get("hardware_profile")
    if not isinstance(hardware_profile, dict):
        hardware_profile = {}

    if run_mode != "live":
        quarantine_reasons.append(f"run_mode is '{run_mode}' (requires 'live')")
    if replicates < 2:
        quarantine_reasons.append(f"replicates is {replicates} (requires >= 2)")
    if gold_verified is not True:
        quarantine_reasons.append("gold_verified is False (frozen gold dataset unverified)")

    completed_set = {int(x) for x in layers_completed} if isinstance(layers_completed, list) else set()
    missing_layers = sorted(set(_REQUIRED_LAYERS) - completed_set)
    if missing_layers:
        quarantine_reasons.append(f"Incomplete layers_completed: missing {missing_layers}")

    log_files = manifest.get("log_files")
    log_sha256 = manifest.get("log_sha256")
    if not isinstance(log_files, dict) or not log_files:
        quarantine_reasons.append("Missing or empty log_files in eval_manifest.json")
        log_files = {}
    if not isinstance(log_sha256, dict):
        quarantine_reasons.append("Missing log_sha256 in eval_manifest.json")
        log_sha256 = {}

    layer_paths: dict[int, Path] = {}
    for key, rel_or_abs in sorted(log_files.items()):
        path = Path(str(rel_or_abs))
        if not path.is_absolute():
            path = eval_path / path
        layer_num = _match_layer_number(str(key), path.name)
        if layer_num is not None:
            layer_paths[layer_num] = path
        if not path.is_file():
            quarantine_reasons.append(f"Missing log file for '{key}': {path.name}")
            continue
        expected_hash = log_sha256.get(key)
        if not isinstance(expected_hash, str) or len(expected_hash.strip()) != 64:
            quarantine_reasons.append(f"Missing valid SHA-256 digest for log '{key}'")
            continue
        actual_hash = _sha256_file(path)
        if actual_hash != expected_hash.strip().lower():
            quarantine_reasons.append(
                f"SHA-256 mismatch for log '{key}': expected {expected_hash}, got {actual_hash}"
            )

    layer_records: dict[int, list[dict[str, Any]]] = {1: [], 2: [], 3: [], 4: []}
    for layer_num in _REQUIRED_LAYERS:
        lpath = layer_paths.get(layer_num)
        if lpath is None or not lpath.is_file():
            quarantine_reasons.append(f"Layer {layer_num} log file unavailable")
            continue
        recs, parse_errs = _load_jsonl(lpath)
        quarantine_reasons.extend(parse_errs)
        layer_records[layer_num] = recs
        if not recs:
            quarantine_reasons.append(f"Layer {layer_num} log contains 0 records")

    if run_mode == "live":
        non_live_records = any(
            str(r.get("run_mode", "live")).strip().lower() != "live"
            for recs in layer_records.values()
            for r in recs
        )
        if non_live_records:
            quarantine_reasons.append("Found non-live run_mode record inside live evaluation logs")

    quarantined = len(quarantine_reasons) > 0
    if quarantined and strict:
        raise MockRunQuarantineError("; ".join(quarantine_reasons))

    h1 = _evaluate_h1(layer_records[1], seed=seed, n_resamples=n_resamples)
    h2 = _evaluate_h2(layer_records[1], layer_records[4], seed=seed, n_resamples=n_resamples)
    h3 = _evaluate_h3(layer_records[2], seed=seed, n_resamples=n_resamples)
    h4, cond_metrics = _evaluate_h4(layer_records[3], seed=seed, n_resamples=n_resamples)

    hypotheses = [h1, h2, h3, h4]
    if quarantined:
        quarantine_prefix = "QUARANTINED (" + "; ".join(quarantine_reasons) + ") | "
        hypotheses = [
            HypothesisResult(
                hypothesis_id=h.hypothesis_id,
                title=h.title,
                status=HypothesisVerdictStatus.QUARANTINED_MOCK_OR_INCOMPLETE,
                metrics=h.metrics,
                p_values=h.p_values,
                bootstrap_cis=h.bootstrap_cis,
                rationale=quarantine_prefix + h.rationale,
            )
            for h in hypotheses
        ]

    l3_present_conds = {str(r.get("condition", "")) for r in layer_records[3]}
    pareto_cost_points, pareto_latency_points = _build_pareto_points(
        layer_records[1], cond_metrics, l3_present_conds
    )

    layer_metrics: dict[str, Any] = {
        "layer1_count": len(layer_records[1]),
        "layer2_count": len(layer_records[2]),
        "layer3_count": len(layer_records[3]),
        "layer4_count": len(layer_records[4]),
        "cond_metrics": cond_metrics,
    }
    context: dict[str, Any] = {
        "run_id": run_id,
        "run_mode": run_mode,
        "seed": manifest_seed,
        "replicates": max(replicates, 0),
        "hardware_profile": hardware_profile,
        "layer_metrics": layer_metrics,
        "cond_metrics": cond_metrics,
        "pareto_cost_points": pareto_cost_points,
        "pareto_latency_points": pareto_latency_points,
    }
    return quarantined, quarantine_reasons, hypotheses, context


__all__ = [
    "MockRunQuarantineError",
    "evaluate_hypotheses",
]
