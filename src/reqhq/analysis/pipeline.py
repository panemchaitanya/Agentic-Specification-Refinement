"""End-to-end Phase 4 analysis pipeline orchestrating quarantine, H1-H4, taxonomy, and SVG charts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from reqhq.analysis.hypotheses import evaluate_hypotheses
from reqhq.analysis.models import AnalysisSummary
from reqhq.analysis.pareto import compute_pareto_frontier, generate_all_charts
from reqhq.analysis.taxonomy import build_error_taxonomy


def analyze_evaluation(
    eval_dir: Path | str,
    out_dir: Path | str,
    *,
    strict_live: bool = False,
    seed: int = 42,
    n_resamples: int = 2000,
) -> AnalysisSummary:
    """Run complete Phase 4 empirical analysis and emit `analysis_summary.json` + 4 SVG charts.

    Enforces fail-closed mock/tamper quarantine: if `strict_live=True` and any quarantine gate
    fails, raises `MockRunQuarantineError` without writing unscored verdicts.
    """
    eval_path = Path(eval_dir)
    out_path = Path(out_dir)

    quarantined, quarantine_reasons, hypotheses, ctx = evaluate_hypotheses(
        eval_path,
        strict=strict_live,
        seed=seed,
        n_resamples=n_resamples,
    )

    taxonomy = build_error_taxonomy(eval_path)
    pareto_cost = compute_pareto_frontier(ctx["pareto_cost_points"])
    pareto_latency = compute_pareto_frontier(ctx["pareto_latency_points"])

    out_path.mkdir(parents=True, exist_ok=True)
    chart_paths = generate_all_charts(
        out_path,
        cost_points=pareto_cost,
        latency_points=pareto_latency,
        cond_metrics=ctx["cond_metrics"],
        taxonomy=taxonomy,
        dry_run_watermark=quarantined,
    )

    verdict_status: Literal["VERIFIED_LIVE", "DRY_RUN_UNSCORED"] = (
        "DRY_RUN_UNSCORED" if quarantined else "VERIFIED_LIVE"
    )
    summary = AnalysisSummary(
        run_id=ctx["run_id"],
        run_mode=ctx["run_mode"],
        quarantined=quarantined,
        quarantine_reasons=quarantine_reasons,
        verdict_status=verdict_status,
        hypotheses=hypotheses,
        taxonomy=taxonomy,
        pareto_cost_points=pareto_cost,
        pareto_latency_points=pareto_latency,
        svg_files={key: path.name for key, path in chart_paths.items()},
        layer_metrics=ctx["layer_metrics"],
        hardware_profile=ctx["hardware_profile"],
        seed=ctx["seed"],
        replicates=ctx["replicates"],
    )

    summary_path = out_path / "analysis_summary.json"
    serialized = json.dumps(summary.model_dump(mode="json"), indent=2) + "\n"
    with summary_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(serialized)

    return summary


__all__ = [
    "analyze_evaluation",
]
