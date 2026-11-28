"""Phase 4 pure-Python statistics, fail-closed H1-H4 evaluator, Pareto SVGs, and error taxonomy."""

from __future__ import annotations

from reqhq.analysis.hypotheses import MockRunQuarantineError, evaluate_hypotheses
from reqhq.analysis.models import (
    AnalysisSummary,
    BootstrapCIResult,
    ErrorCategory,
    ErrorTaxonomyReport,
    HypothesisResult,
    HypothesisVerdictStatus,
    ParetoPoint,
    TaxonomyCategorySummary,
)
from reqhq.analysis.pareto import (
    compute_pareto_frontier,
    generate_all_charts,
    render_cond_abc_svg,
    render_pareto_svg,
    render_taxonomy_bar_svg,
)
from reqhq.analysis.pipeline import analyze_evaluation
from reqhq.analysis.stats import (
    mcnemar_exact_p,
    paired_bootstrap_ci,
    wilcoxon_signed_rank,
)
from reqhq.analysis.taxonomy import build_error_taxonomy, classify_error_event

__all__ = [
    "AnalysisSummary",
    "BootstrapCIResult",
    "ErrorCategory",
    "ErrorTaxonomyReport",
    "HypothesisResult",
    "HypothesisVerdictStatus",
    "MockRunQuarantineError",
    "ParetoPoint",
    "TaxonomyCategorySummary",
    "analyze_evaluation",
    "build_error_taxonomy",
    "classify_error_event",
    "compute_pareto_frontier",
    "evaluate_hypotheses",
    "generate_all_charts",
    "mcnemar_exact_p",
    "paired_bootstrap_ci",
    "render_cond_abc_svg",
    "render_pareto_svg",
    "render_taxonomy_bar_svg",
    "wilcoxon_signed_rank",
]
