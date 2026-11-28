"""Unit and deterministic Hypothesis PBT tests for Phase 4 analysis package."""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from reqhq.analysis import (
    AnalysisSummary,
    BootstrapCIResult,
    ErrorCategory,
    ErrorTaxonomyReport,
    HypothesisVerdictStatus,
    MockRunQuarantineError,
    ParetoPoint,
    TaxonomyCategorySummary,
    analyze_evaluation,
    build_error_taxonomy,
    classify_error_event,
    compute_pareto_frontier,
    evaluate_hypotheses,
    generate_all_charts,
    mcnemar_exact_p,
    paired_bootstrap_ci,
    render_cond_abc_svg,
    render_pareto_svg,
    render_taxonomy_bar_svg,
    wilcoxon_signed_rank,
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> str:
    content = "".join(json.dumps(r) + "\n" for r in records)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    return _sha256_bytes(path.read_bytes())


def _build_synthetic_eval_dir(
    eval_dir: Path,
    *,
    run_mode: str = "live",
    replicates: int = 2,
    gold_verified: bool = True,
    layers_completed: list[int] | None = None,
    h1_mode: str = "supported",
    h2_mode: str = "supported",
    h3_mode: str = "supported",
    h4_mode: str = "supported",
    l4_zero_baselines: bool = False,
    record_run_mode: str | None = None,
) -> Path:
    """Create a complete synthetic evaluation directory with SHA-256 verified JSONL logs."""
    eval_dir.mkdir(parents=True, exist_ok=True)
    rec_mode = record_run_mode if record_run_mode is not None else run_mode

    # Layer 1 records (10 items for clear significance)
    l1_records: list[dict[str, Any]] = []
    for i in range(10):
        if h1_mode == "supported":
            iter_rec, single_rec = 0.86 + 0.01 * (i % 3), 0.55 + 0.01 * (i % 3)
            q_asked, q_base = 4 + (i % 2), 14 + (i % 3)
        elif h1_mode == "refuted":
            iter_rec, single_rec = 0.45, 0.70
            q_asked, q_base = 16, 12
        else:  # inconclusive: small n=2 or tiny noisy edge
            iter_rec = 0.72 if i == 0 else 0.70
            single_rec = 0.70
            q_asked = 9 if i == 0 else 10
            q_base = 10

        if h2_mode == "supported":
            local_f1, cloud_f1 = 0.85, 0.87
            c_req, base_req = 2, 20
            c_tok, base_tok = 500, 6000
        elif h2_mode == "refuted":
            local_f1, cloud_f1 = 0.60, 0.88
            c_req, base_req = 10, 20
            c_tok, base_tok = 3500, 6000
        else:  # inconclusive: mean_diff >= -0.05, but high variance pushes ci_low < -0.05
            local_f1 = 0.70 if i < 3 else 0.90
            cloud_f1 = 0.88
            c_req, base_req = 2, 20
            c_tok, base_tok = 500, 6000

        l1_records.append(
            {
                "record_id": f"l1_{i}",
                "item_id": f"pure_{i}",
                "split": "test",
                "seed": 42,
                "run_mode": rec_mode,
                "gold_verified": gold_verified,
                "single_pass_recall": single_rec,
                "iterative_recall": iter_rec,
                "iterative_precision": 0.88,
                "iterative_f1": local_f1,
                "all_cloud_f1": cloud_f1,
                "questions_asked": q_asked,
                "exhaustive_baseline_questions": q_base,
                "redundant_question_rate": 0.05,
                "cloud_requests": c_req,
                "all_cloud_requests": base_req,
                "cloud_tokens": c_tok,
                "all_cloud_tokens": base_tok,
                "wall_clock_sec": 1.2 + 0.1 * (i % 3),
                "error_events": ["QUOTA_FALLBACK: 429 rate limit"] if i == 0 else [],
            }
        )

    # Layer 2 records (12 cases)
    l2_records: list[dict[str, Any]] = []
    for i in range(12):
        if h3_mode == "supported":
            alloy_c, llm_c = 2, 0
            alloy_bin, llm_bin = True, False
            route_rubric, sw_rubric = 0.90 + 0.01 * (i % 2), 0.62 + 0.01 * (i % 2)
        elif h3_mode == "refuted":
            alloy_c, llm_c = 0, 2
            alloy_bin, llm_bin = False, True
            route_rubric, sw_rubric = 0.55, 0.75
        else:  # inconclusive: directional favors alloy + routing, but only 2 discordant pairs (p=0.5)
            alloy_c, llm_c = (1, 0) if i < 2 else (1, 1)
            alloy_bin, llm_bin = (True, False) if i < 2 else (True, True)
            route_rubric = 0.80 if i == 0 else 0.70
            sw_rubric = 0.70

        l2_records.append(
            {
                "record_id": f"l2_{i}",
                "case_id": f"case_{i}",
                "domain": "fintech_settlements",
                "input_kind": "multi_paragraph",
                "run_mode": rec_mode,
                "route_accuracy": 0.92,
                "alloy_contradictions_caught": alloy_c,
                "llm_only_contradictions_caught": llm_c,
                "alloy_caught_binary": alloy_bin,
                "llm_only_caught_binary": llm_bin,
                "routing_adr_rubric_score": route_rubric,
                "software_only_rubric_score": sw_rubric,
                "cohens_kappa": 0.81,
                "wall_clock_sec": 2.4,
                "error_events": ["UNSAT_CONTRADICTION detected"] if i == 0 else [],
            }
        )

    # Layer 3 records: 6 tasks x replicates x 3 conditions
    l3_records: list[dict[str, Any]] = []
    tasks = [f"task_{idx}" for idx in range(6)]
    for t_idx, task_id in enumerate(tasks):
        for rep in range(1, max(replicates, 1) + 1):
            pair_idx = t_idx * max(replicates, 1) + (rep - 1)
            if h4_mode == "supported":
                specs = {
                    "raw_prd": (False, 0.45, 0.40, 6, 4.5),
                    "single_pass_nl_spec": (pair_idx < 2, 0.65, 0.60, 8, 5.2),
                    "reqhq_full_bundle": (True, 0.95, 0.90, 2, 3.8),
                }
            elif h4_mode == "refuted":
                specs = {
                    "raw_prd": (True, 0.90, 0.85, 6, 4.5),
                    "single_pass_nl_spec": (True, 0.88, 0.82, 8, 5.2),
                    "reqhq_full_bundle": (False, 0.50, 0.45, 2, 3.8),
                }
            else:  # inconclusive: directional C > A and C > B, but only 2 discordant pairs (p=0.5)
                specs = {
                    "raw_prd": (pair_idx >= 2, 0.70, 0.70, 6, 4.5),
                    "single_pass_nl_spec": (pair_idx >= 2, 0.72, 0.72, 8, 5.2),
                    "reqhq_full_bundle": (
                        True,
                        0.80 if pair_idx == 0 else 0.72,
                        0.80 if pair_idx == 0 else 0.72,
                        2,
                        3.8,
                    ),
                }
            for cond, (passed, pbt_rate, mut_score, c_req, wall) in specs.items():
                l3_records.append(
                    {
                        "record_id": f"l3_{task_id}_{cond}_r{rep}",
                        "task_id": task_id,
                        "domain": "fintech",
                        "condition": cond,
                        "replicate": rep,
                        "seed": 42 + rep,
                        "run_mode": rec_mode,
                        "alloy_formalized": cond == "reqhq_full_bundle",
                        "alloy_non_vacuous": cond == "reqhq_full_bundle",
                        "lock_verified": True,
                        "hidden_pass_at_1": passed,
                        "pbt_pass_rate": pbt_rate,
                        "mutation_score": mut_score,
                        "stall_count": 0,
                        "wall_clock_sec": wall,
                        "cloud_requests": c_req,
                        "cloud_tokens": c_req * 300,
                        "error_events": (
                            ["PBT_COUNTEREXAMPLE: falsified invariant"]
                            if (cond == "raw_prd" and pair_idx == 0)
                            else []
                        ),
                    }
                )

    # Layer 4 telemetry records
    if l4_zero_baselines:
        base_req_l4, used_req_l4 = 0, 0
        base_tok_l4, used_tok_l4 = 0, 0
    elif h2_mode == "refuted":
        base_req_l4, used_req_l4 = 100, 60
        base_tok_l4, used_tok_l4 = 10000, 6000
    else:
        base_req_l4, used_req_l4 = 100, 10
        base_tok_l4, used_tok_l4 = 10000, 1200

    l4_records = [
        {
            "stage": "layer4_summary",
            "run_mode": rec_mode,
            "hardware_tier": "TIER_0",
            "gpu_name": "Intel Iris Xe",
            "vram_gb": 0.0,
            "ram_gb": 16.0,
            "total_steps": 100,
            "local_only_steps": 90,
            "local_only_pct": 90.0,
            "cloud_requests": used_req_l4,
            "all_cloud_baseline_requests": base_req_l4,
            "cloud_tokens": used_tok_l4,
            "all_cloud_baseline_tokens": base_tok_l4,
            "cloud_reduction_pct": 90.0,
            "wall_clock_sec": 12.5,
            "peak_ram_mb": 210.4,
            "peak_vram_mb": 0.0,
            "error_events": [],
        }
    ]

    files = {
        "layer1": "layer1.jsonl",
        "layer2": "layer2.jsonl",
        "layer3": "layer3.jsonl",
        "layer4": "layer4_telemetry.jsonl",
    }
    hashes = {
        "layer1": _write_jsonl(eval_dir / files["layer1"], l1_records),
        "layer2": _write_jsonl(eval_dir / files["layer2"], l2_records),
        "layer3": _write_jsonl(eval_dir / files["layer3"], l3_records),
        "layer4": _write_jsonl(eval_dir / files["layer4"], l4_records),
    }

    manifest = {
        "run_id": "run_synth_001",
        "run_mode": run_mode,
        "seed": 42,
        "replicates": replicates,
        "layers_completed": [1, 2, 3, 4] if layers_completed is None else layers_completed,
        "gold_verified": gold_verified,
        "hardware_profile": {"tier": "TIER_0", "ram_gb": 16.0},
        "log_files": files,
        "log_sha256": hashes,
        "record_counts": {
            "layer1": len(l1_records),
            "layer2": len(l2_records),
            "layer3": len(l3_records),
            "layer4": len(l4_records),
        },
        "created_at": "2026-11-25T10:00:00Z",
    }
    manifest_path = eval_dir / "eval_manifest.json"
    with manifest_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(manifest, indent=2) + "\n")
    return eval_dir


# ---------------------------------------------------------------------------
# Mandatory Deterministic Hypothesis Property-Based Tests
# ---------------------------------------------------------------------------


_FINITE_FLOAT = st.floats(
    min_value=-1e6,
    max_value=1e6,
    allow_nan=False,
    allow_infinity=False,
)


@given(
    pairs=st.lists(st.tuples(_FINITE_FLOAT, _FINITE_FLOAT), min_size=1, max_size=30),
    n_resamples=st.integers(min_value=1, max_value=100),
    alpha=st.floats(min_value=0.01, max_value=0.5, allow_nan=False, allow_infinity=False),
    seed=st.integers(min_value=0, max_value=10_000),
)
@settings(derandomize=True, max_examples=50)
def test_pbt_paired_bootstrap_ci_invariants(
    pairs: list[tuple[float, float]],
    n_resamples: int,
    alpha: float,
    seed: int,
) -> None:
    a = [p[0] for p in pairs]
    b = [p[1] for p in pairs]
    res1 = paired_bootstrap_ci(a, b, n_resamples=n_resamples, alpha=alpha, seed=seed)
    res2 = paired_bootstrap_ci(a, b, n_resamples=n_resamples, alpha=alpha, seed=seed)

    assert math.isfinite(res1.mean_diff)
    assert math.isfinite(res1.ci_low)
    assert math.isfinite(res1.ci_high)
    assert res1.ci_low <= res1.mean_diff <= res1.ci_high
    assert res1 == res2


@given(
    b_wins=st.integers(min_value=0, max_value=200),
    c_wins=st.integers(min_value=0, max_value=200),
)
@settings(derandomize=True, max_examples=50)
def test_pbt_mcnemar_bounds_and_exact_symmetry(b_wins: int, c_wins: int) -> None:
    p_bc = mcnemar_exact_p(b_wins, c_wins)
    p_cb = mcnemar_exact_p(c_wins, b_wins)

    assert 0.0 <= p_bc <= 1.0
    assert p_bc == p_cb


@given(
    pairs=st.lists(st.tuples(_FINITE_FLOAT, _FINITE_FLOAT), min_size=0, max_size=30),
)
@settings(derandomize=True, max_examples=50)
def test_pbt_wilcoxon_bounds_and_exact_symmetry(pairs: list[tuple[float, float]]) -> None:
    a = [p[0] for p in pairs]
    b = [p[1] for p in pairs]
    p_ab = wilcoxon_signed_rank(a, b)
    p_ba = wilcoxon_signed_rank(b, a)

    assert 0.0 <= p_ab <= 1.0
    assert p_ab == p_ba


@given(
    raw_points=st.lists(
        st.tuples(_FINITE_FLOAT, _FINITE_FLOAT),
        min_size=0,
        max_size=25,
    )
)
@settings(derandomize=True, max_examples=50)
def test_pbt_pareto_frontier_dominance_invariant(raw_points: list[tuple[float, float]]) -> None:
    points = [
        ParetoPoint(label=f"pt_{i}", quality=q, cost=c)
        for i, (q, c) in enumerate(raw_points)
    ]
    evaluated = compute_pareto_frontier(points)
    assert len(evaluated) == len(points)
    assert [p.label for p in evaluated] == [p.label for p in points]

    frontier = [p for p in evaluated if p.is_frontier]
    dominated = [p for p in evaluated if not p.is_frontier]

    for fp in frontier:
        assert not any(
            (other.quality >= fp.quality and other.cost <= fp.cost)
            and (other.quality > fp.quality or other.cost < fp.cost)
            for other in evaluated
        )

    for dp in dominated:
        assert any(
            (fp.quality >= dp.quality and fp.cost <= dp.cost)
            and (fp.quality > dp.quality or fp.cost < dp.cost)
            for fp in frontier
        )


@given(
    records_events=st.lists(
        st.lists(
            st.one_of(
                st.sampled_from(
                    [
                        "SCHEMA_DRIFT: extra field",
                        "alloy syntax parse error",
                        "vacuous model check",
                        "unsat contradiction core",
                        "routing ambiguity in triage",
                        "hypothesis pbt counterexample",
                        "quota 429 fallback triggered",
                        "unexpected misc error",
                    ]
                ),
                st.text(min_size=1, max_size=40),
            ),
            min_size=0,
            max_size=5,
        ),
        min_size=0,
        max_size=12,
    )
)
@settings(derandomize=True, max_examples=50, deadline=None)
def test_pbt_error_taxonomy_counts_sum_to_total(records_events: list[list[str]]) -> None:
    expected_total = sum(len(ev) for ev in records_events)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        jsonl_records = [
            {"record_id": f"rec_{idx}", "error_events": events}
            for idx, events in enumerate(records_events)
        ]
        _write_jsonl(tmp_path / "layer1.jsonl", jsonl_records)
        report = build_error_taxonomy(tmp_path)

    assert report.total_failures == expected_total
    assert sum(c.count for c in report.categories) == report.total_failures
    assert len(report.categories) == len(ErrorCategory)


# ---------------------------------------------------------------------------
# Unit Tests: Models & Stats Edge Cases
# ---------------------------------------------------------------------------


def test_models_validation_errors() -> None:
    with pytest.raises(ValidationError):
        BootstrapCIResult(
            mean_diff=float("nan"),
            ci_low=0.0,
            ci_high=1.0,
            n_resamples=10,
            alpha=0.05,
            seed=42,
        )
    with pytest.raises(ValidationError):
        BootstrapCIResult(
            mean_diff=0.5,
            ci_low=0.6,
            ci_high=0.8,
            n_resamples=10,
            alpha=0.05,
            seed=42,
        )
    with pytest.raises(ValidationError):
        ErrorTaxonomyReport(
            total_failures=5,
            categories=[
                TaxonomyCategorySummary(
                    category=ErrorCategory.SCHEMA_DRIFT,
                    count=2,
                    share_pct=100.0,
                    sample_ids=["r1"],
                )
            ],
        )


def test_stats_validation_and_known_values() -> None:
    with pytest.raises(ValueError, match="equal length"):
        paired_bootstrap_ci([1.0], [1.0, 2.0])
    with pytest.raises(ValueError, match="must not be empty"):
        paired_bootstrap_ci([], [])
    with pytest.raises(ValueError, match="n_resamples"):
        paired_bootstrap_ci([1.0], [0.0], n_resamples=0)
    with pytest.raises(ValueError, match="alpha"):
        paired_bootstrap_ci([1.0], [0.0], alpha=1.0)
    with pytest.raises(ValueError, match="Non-finite"):
        paired_bootstrap_ci([float("inf")], [0.0])
    with pytest.raises(ValueError, match="overflowed"):
        paired_bootstrap_ci([1e308], [-1e308])

    # Single resample hits _percentile n==1 branch; exact quantile hits lo==hi branch
    ci_one = paired_bootstrap_ci([2.0, 4.0], [1.0, 2.0], n_resamples=1, alpha=0.05, seed=7)
    assert ci_one.ci_low <= ci_one.mean_diff <= ci_one.ci_high
    ci_exact_idx = paired_bootstrap_ci([1.0, 2.0, 3.0], [0.0, 0.0, 0.0], n_resamples=5, alpha=0.5, seed=7)
    assert ci_exact_idx.ci_low <= ci_exact_idx.mean_diff <= ci_exact_idx.ci_high

    with pytest.raises(ValueError, match="non-negative"):
        mcnemar_exact_p(-1, 3)
    assert mcnemar_exact_p(0, 0) == 1.0
    assert mcnemar_exact_p(4, 4) == 1.0
    assert math.isclose(mcnemar_exact_p(6, 0), 2.0 / 64.0, rel_tol=1e-12)

    assert wilcoxon_signed_rank([1.0, 2.0], [1.0, 2.0]) == 1.0
    assert math.isclose(
        wilcoxon_signed_rank([2.0, 4.0, 6.0, 8.0, 10.0, 12.0], [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]),
        2.0 / 64.0,
        rel_tol=1e-12,
    )
    # Normal approximation branch (n > 15) with ties
    a_large = [float(i // 2 + 2) for i in range(20)]
    b_large = [float(i // 2) for i in range(20)]
    p_large = wilcoxon_signed_rank(a_large, b_large)
    assert 0.0 < p_large < 0.001
    assert p_large == wilcoxon_signed_rank(b_large, a_large)


# ---------------------------------------------------------------------------
# Unit Tests: Error Taxonomy
# ---------------------------------------------------------------------------


def test_classify_error_event_all_categories() -> None:
    assert classify_error_event("SCHEMA_DRIFT: extra field") == ErrorCategory.SCHEMA_DRIFT
    assert classify_error_event("ALLOY_SYNTAX line 4") == ErrorCategory.ALLOY_SYNTAX
    assert classify_error_event("ALLOY_VACUOUS trivial") == ErrorCategory.ALLOY_VACUOUS
    assert classify_error_event("UNSAT_CONTRADICTION") == ErrorCategory.UNSAT_CONTRADICTION
    assert classify_error_event("ROUTING_AMBIGUITY") == ErrorCategory.ROUTING_AMBIGUITY
    assert classify_error_event("PBT_COUNTEREXAMPLE") == ErrorCategory.PBT_COUNTEREXAMPLE
    assert classify_error_event("QUOTA_FALLBACK") == ErrorCategory.QUOTA_FALLBACK

    assert classify_error_event("HTTP 429 rate limit exceeded") == ErrorCategory.QUOTA_FALLBACK
    assert classify_error_event("Alloy vacuity check failed") == ErrorCategory.ALLOY_VACUOUS
    assert classify_error_event("Deadlock conflict found") == ErrorCategory.UNSAT_CONTRADICTION
    assert classify_error_event("Failed parsing .als file") == ErrorCategory.ALLOY_SYNTAX
    assert classify_error_event("Hypothesis falsified invariant") == ErrorCategory.PBT_COUNTEREXAMPLE
    assert classify_error_event("Triage track ambiguous") == ErrorCategory.ROUTING_AMBIGUITY
    assert classify_error_event("unclassified validation glitch") == ErrorCategory.SCHEMA_DRIFT


def test_build_error_taxonomy_manifest_and_malformed_lines(tmp_path: Path) -> None:
    # Corrupt manifest falls back to glob("*.jsonl")
    (tmp_path / "eval_manifest.json").write_text("{not valid json", encoding="utf-8")
    log_file = tmp_path / "layer1.jsonl"
    log_file.write_text(
        "\n"
        "{bad json}\n"
        "\"not a dict\"\n"
        + json.dumps({"record_id": "r_no_list", "error_events": "bad"})
        + "\n"
        + json.dumps({"record_id": "r1", "error_events": ["429 quota", "429 quota"]})
        + "\n"
        + json.dumps({"stage": "s2", "error_events": ["429 quota"]})
        + "\n"
        + json.dumps({"error_events": ["429 quota"]})
        + "\n",
        encoding="utf-8",
    )
    report = build_error_taxonomy(tmp_path, max_samples_per_category=2)
    assert report.total_failures == 4
    quota_cat = next(c for c in report.categories if c.category == ErrorCategory.QUOTA_FALLBACK)
    assert quota_cat.count == 4
    assert quota_cat.share_pct == 100.0
    assert quota_cat.sample_ids == ["r1", "s2"]

    # Valid manifest with absolute path entry
    manifest_dir = tmp_path / "with_manifest"
    manifest_dir.mkdir()
    abs_log = manifest_dir / "abs_layer.jsonl"
    abs_log.write_text(json.dumps({"record_id": "a1", "error_events": []}) + "\n", encoding="utf-8")
    (manifest_dir / "eval_manifest.json").write_text(
        json.dumps({"log_files": {"layer1": str(abs_log.resolve())}}),
        encoding="utf-8",
    )
    empty_report = build_error_taxonomy(manifest_dir)
    assert empty_report.total_failures == 0
    assert all(c.share_pct == 0.0 for c in empty_report.categories)


# ---------------------------------------------------------------------------
# Unit Tests: Pareto & Pure-Python SVG Charts
# ---------------------------------------------------------------------------


def test_pareto_and_svg_generation_valid_xml(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        compute_pareto_frontier([ParetoPoint(label="bad", quality=float("inf"), cost=1.0)])

    # Empty points & zero-span constant points & special XML chars
    empty_svg = render_pareto_svg(
        [],
        title="Empty <Pareto> & 'Test'",
        x_label="Cost & Tokens",
        y_label="Quality <1.0>",
        dry_run_watermark=True,
    )
    ET.fromstring(empty_svg)
    assert "DRY-RUN MOCK DATA" in empty_svg
    assert "&lt;Pareto&gt;" in empty_svg

    const_svg = render_pareto_svg(
        [ParetoPoint(label='A "single" <pt>', quality=0.8, cost=5.0)],
        title="Constant Bounds",
        x_label="Cost",
        y_label="Quality",
        dry_run_watermark=False,
    )
    ET.fromstring(const_svg)
    assert "DRY-RUN MOCK DATA" not in const_svg

    # Full 4-chart generation: both watermarked dry-run and clean live
    cost_pts = [
        ParetoPoint(label="Cond A", quality=0.45, cost=6.0),
        ParetoPoint(label="Cond B", quality=0.65, cost=8.0),
        ParetoPoint(label="Cond C", quality=0.92, cost=2.0),
        ParetoPoint(label="All-Cloud", quality=0.95, cost=20.0),
    ]
    cond_metrics = {
        "raw_prd": {"hidden_pass_at_1": 0.2, "pbt_pass_rate": 0.45, "mutation_score": 0.4},
        "single_pass_nl_spec": {"hidden_pass_at_1": 0.5, "pbt_pass_rate": 0.65, "mutation_score": 0.6},
        "reqhq_full_bundle": {"hidden_pass_at_1": 0.95, "pbt_pass_rate": 0.96, "mutation_score": 0.91},
    }
    taxonomy = ErrorTaxonomyReport(
        total_failures=3,
        categories=[
            TaxonomyCategorySummary(
                category=cat,
                count=3 if cat == ErrorCategory.ALLOY_SYNTAX else 0,
                share_pct=100.0 if cat == ErrorCategory.ALLOY_SYNTAX else 0.0,
                sample_ids=["r1"] if cat == ErrorCategory.ALLOY_SYNTAX else [],
            )
            for cat in ErrorCategory
        ],
    )

    dry_paths = generate_all_charts(
        tmp_path / "dry",
        cost_points=cost_pts,
        latency_points=cost_pts,
        cond_metrics=cond_metrics,
        taxonomy=taxonomy,
        dry_run_watermark=True,
    )
    assert set(dry_paths) == {
        "pareto_quality_vs_cloud_cost",
        "pareto_quality_vs_latency",
        "cond_abc_comparison",
        "error_taxonomy_bar",
    }
    for path in dry_paths.values():
        raw = path.read_bytes()
        assert b"\r\n" not in raw
        text = raw.decode("utf-8")
        ET.fromstring(text)
        assert "DRY-RUN MOCK DATA" in text

    live_paths = generate_all_charts(
        tmp_path / "live",
        cost_points=cost_pts,
        latency_points=cost_pts,
        cond_metrics=cond_metrics,
        taxonomy=taxonomy,
        dry_run_watermark=False,
    )
    for path in live_paths.values():
        text = path.read_text(encoding="utf-8")
        ET.fromstring(text)
        assert "DRY-RUN MOCK DATA" not in text

    # Direct calls to render_cond_abc_svg and render_taxonomy_bar_svg
    ET.fromstring(render_cond_abc_svg(cond_metrics, dry_run_watermark=False))
    ET.fromstring(render_taxonomy_bar_svg(taxonomy, dry_run_watermark=False))


# ---------------------------------------------------------------------------
# Unit Tests: Fail-Closed Quarantine & Live H1-H4 Verdicts
# ---------------------------------------------------------------------------


def test_quarantine_gates_fail_closed(tmp_path: Path) -> None:
    # 1. Missing manifest
    missing_dir = tmp_path / "missing"
    missing_dir.mkdir()
    q, reasons, hyps, _ = evaluate_hypotheses(missing_dir, strict=False, n_resamples=50)
    assert q is True
    assert any("Missing eval_manifest.json" in r for r in reasons)
    assert all(h.status == HypothesisVerdictStatus.QUARANTINED_MOCK_OR_INCOMPLETE for h in hyps)
    with pytest.raises(MockRunQuarantineError, match="Missing eval_manifest.json"):
        evaluate_hypotheses(missing_dir, strict=True, n_resamples=50)

    # 2. Corrupt manifest JSON
    corrupt_dir = tmp_path / "corrupt"
    corrupt_dir.mkdir()
    (corrupt_dir / "eval_manifest.json").write_text("{corrupt", encoding="utf-8")
    q, reasons, hyps, _ = evaluate_hypotheses(corrupt_dir, strict=False, n_resamples=50)
    assert q is True
    assert any("Corrupt eval_manifest.json" in r for r in reasons)
    with pytest.raises(MockRunQuarantineError, match="Corrupt eval_manifest.json"):
        evaluate_hypotheses(corrupt_dir, strict=True, n_resamples=50)

    # 3. Mock run mode
    mock_dir = _build_synthetic_eval_dir(tmp_path / "mock_run", run_mode="mock")
    summary = analyze_evaluation(mock_dir, tmp_path / "mock_out", strict_live=False, n_resamples=50)
    assert summary.quarantined is True
    assert summary.verdict_status == "DRY_RUN_UNSCORED"
    assert all(h.status == HypothesisVerdictStatus.QUARANTINED_MOCK_OR_INCOMPLETE for h in summary.hypotheses)
    for svg_name in summary.svg_files.values():
        svg_text = (tmp_path / "mock_out" / svg_name).read_text(encoding="utf-8")
        ET.fromstring(svg_text)
        assert "DRY-RUN MOCK DATA" in svg_text
    with pytest.raises(MockRunQuarantineError, match="run_mode"):
        analyze_evaluation(mock_dir, tmp_path / "mock_out_strict", strict_live=True, n_resamples=50)

    # 4. Replicates < 2
    low_rep = _build_synthetic_eval_dir(tmp_path / "low_rep", run_mode="live", replicates=1)
    with pytest.raises(MockRunQuarantineError, match="replicates"):
        evaluate_hypotheses(low_rep, strict=True, n_resamples=50)

    # 5. gold_verified=False
    unverified = _build_synthetic_eval_dir(tmp_path / "unverified", run_mode="live", gold_verified=False)
    with pytest.raises(MockRunQuarantineError, match="gold_verified"):
        evaluate_hypotheses(unverified, strict=True, n_resamples=50)

    # 6. Incomplete layers
    incomplete = _build_synthetic_eval_dir(
        tmp_path / "incomplete", run_mode="live", layers_completed=[1, 2, 3]
    )
    with pytest.raises(MockRunQuarantineError, match="Incomplete layers_completed"):
        evaluate_hypotheses(incomplete, strict=True, n_resamples=50)

    # 7. Tampered log SHA-256 mismatch
    tampered = _build_synthetic_eval_dir(tmp_path / "tampered", run_mode="live")
    l1_file = tampered / "layer1.jsonl"
    l1_file.write_text(l1_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(MockRunQuarantineError, match="SHA-256 mismatch"):
        evaluate_hypotheses(tampered, strict=True, n_resamples=50)

    # 8. Mixed mock record inside live manifest
    mixed = _build_synthetic_eval_dir(
        tmp_path / "mixed", run_mode="live", record_run_mode="mock"
    )
    with pytest.raises(MockRunQuarantineError, match="non-live run_mode record"):
        evaluate_hypotheses(mixed, strict=True, n_resamples=50)


def test_quarantine_edge_manifest_structures(tmp_path: Path) -> None:
    # Manifest with non-dict log_files/log_sha256/hardware_profile -> missing all layers gracefully
    bad_struct = tmp_path / "bad_struct"
    bad_struct.mkdir()
    (bad_struct / "eval_manifest.json").write_text(
        json.dumps(
            {
                "run_id": "bad_1",
                "run_mode": "live",
                "replicates": 2,
                "gold_verified": True,
                "layers_completed": [1, 2, 3, 4],
                "hardware_profile": "not-a-dict",
                "log_files": [],
                "log_sha256": [],
            }
        ),
        encoding="utf-8",
    )
    q, reasons, hyps, _ = evaluate_hypotheses(bad_struct, strict=False, n_resamples=50)
    assert q is True
    assert any("log_files" in r for r in reasons)
    assert all(h.status == HypothesisVerdictStatus.QUARANTINED_MOCK_OR_INCOMPLETE for h in hyps)

    # Manifest with missing file, short hash, unknown log key, corrupt/non-dict JSONL lines, and 0-record log
    edge_dir = tmp_path / "edge_logs"
    edge_dir.mkdir()
    empty_l1 = edge_dir / "layer1.jsonl"
    empty_l1.write_text("\n", encoding="utf-8")
    corrupt_l2 = edge_dir / "layer2.jsonl"
    corrupt_l2.write_text("{bad\n123\n", encoding="utf-8")
    abs_extra = edge_dir / "notes.jsonl"
    abs_extra.write_text(json.dumps({"note": "ok"}) + "\n", encoding="utf-8")

    (edge_dir / "eval_manifest.json").write_text(
        json.dumps(
            {
                "run_id": "edge_1",
                "run_mode": "live",
                "replicates": 2,
                "gold_verified": True,
                "layers_completed": [1, 2, 3, 4],
                "log_files": {
                    "layer1": "layer1.jsonl",
                    "layer2": "layer2.jsonl",
                    "layer3": "missing_layer3.jsonl",
                    "extra_meta": str(abs_extra.resolve()),
                },
                "log_sha256": {
                    "layer1": _sha256_bytes(empty_l1.read_bytes()),
                    "layer2": "short_hash",
                    "extra_meta": _sha256_bytes(abs_extra.read_bytes()),
                },
            }
        ),
        encoding="utf-8",
    )
    q2, reasons2, hyps2, _ = evaluate_hypotheses(edge_dir, strict=False, n_resamples=50)
    assert q2 is True
    assert any("Missing log file" in r for r in reasons2)
    assert any("Missing valid SHA-256" in r for r in reasons2)
    assert any("Invalid JSON" in r for r in reasons2)
    assert any("Non-object JSON" in r for r in reasons2)
    assert any("0 records" in r for r in reasons2)
    assert all(h.status == HypothesisVerdictStatus.QUARANTINED_MOCK_OR_INCOMPLETE for h in hyps2)


def test_verified_live_all_supported_and_pipeline_outputs(tmp_path: Path) -> None:
    live_dir = _build_synthetic_eval_dir(tmp_path / "live_supported", run_mode="live")
    out_dir = tmp_path / "live_out"
    summary = analyze_evaluation(live_dir, out_dir, strict_live=True, seed=42, n_resamples=200)

    assert isinstance(summary, AnalysisSummary)
    assert summary.quarantined is False
    assert summary.quarantine_reasons == []
    assert summary.verdict_status == "VERIFIED_LIVE"
    assert summary.replicates == 2
    assert {h.hypothesis_id: h.status for h in summary.hypotheses} == {
        "H1": HypothesisVerdictStatus.SUPPORTED,
        "H2": HypothesisVerdictStatus.SUPPORTED,
        "H3": HypothesisVerdictStatus.SUPPORTED,
        "H4": HypothesisVerdictStatus.SUPPORTED,
    }
    assert summary.taxonomy.total_failures == 3
    assert len(summary.pareto_cost_points) == 5
    assert len(summary.pareto_latency_points) == 4

    summary_json = out_dir / "analysis_summary.json"
    raw = summary_json.read_bytes()
    assert b"\r\n" not in raw
    reloaded = AnalysisSummary.model_validate_json(raw.decode("utf-8"))
    assert reloaded.verdict_status == "VERIFIED_LIVE"

    for svg_filename in summary.svg_files.values():
        svg_text = (out_dir / svg_filename).read_text(encoding="utf-8")
        ET.fromstring(svg_text)
        assert "DRY-RUN MOCK DATA" not in svg_text


def test_verified_live_refuted_and_inconclusive_verdicts(tmp_path: Path) -> None:
    # All 4 REFUTED + Layer 4 zero baselines fallback to Layer 1 baselines
    refuted_dir = _build_synthetic_eval_dir(
        tmp_path / "live_refuted",
        run_mode="live",
        h1_mode="refuted",
        h2_mode="refuted",
        h3_mode="refuted",
        h4_mode="refuted",
        l4_zero_baselines=True,
    )
    q, _, hyps, _ = evaluate_hypotheses(refuted_dir, strict=True, n_resamples=100)
    assert q is False
    assert {h.hypothesis_id: h.status for h in hyps} == {
        "H1": HypothesisVerdictStatus.REFUTED,
        "H2": HypothesisVerdictStatus.REFUTED,
        "H3": HypothesisVerdictStatus.REFUTED,
        "H4": HypothesisVerdictStatus.REFUTED,
    }

    # All 4 INCONCLUSIVE
    inconc_dir = _build_synthetic_eval_dir(
        tmp_path / "live_inconclusive",
        run_mode="live",
        h1_mode="inconclusive",
        h2_mode="inconclusive",
        h3_mode="inconclusive",
        h4_mode="inconclusive",
    )
    q2, _, hyps2, _ = evaluate_hypotheses(inconc_dir, strict=True, n_resamples=200)
    assert q2 is False
    assert {h.hypothesis_id: h.status for h in hyps2} == {
        "H1": HypothesisVerdictStatus.INCONCLUSIVE,
        "H2": HypothesisVerdictStatus.INCONCLUSIVE,
        "H3": HypothesisVerdictStatus.INCONCLUSIVE,
        "H4": HypothesisVerdictStatus.INCONCLUSIVE,
    }


def test_h2_zero_baselines_everywhere(tmp_path: Path) -> None:
    eval_dir = _build_synthetic_eval_dir(
        tmp_path / "zero_base",
        run_mode="live",
        l4_zero_baselines=True,
    )
    # Zero out Layer 1 baselines too and re-hash layer1.jsonl
    l1_path = eval_dir / "layer1.jsonl"
    recs = [json.loads(line) for line in l1_path.read_text(encoding="utf-8").splitlines() if line]
    for r in recs:
        r["all_cloud_requests"] = 0
        r["all_cloud_tokens"] = 0
    new_hash = _write_jsonl(l1_path, recs)
    manifest_path = eval_dir / "eval_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["log_sha256"]["layer1"] = new_hash
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    q, _, hyps, _ = evaluate_hypotheses(eval_dir, strict=True, n_resamples=50)
    assert q is False
    h2 = next(h for h in hyps if h.hypothesis_id == "H2")
    assert h2.status == HypothesisVerdictStatus.REFUTED
    assert h2.metrics["cloud_request_reduction_pct"] == 0.0
    assert h2.metrics["cloud_token_reduction_pct"] == 0.0
