"""Strict Pydantic v2 models for Phase 4 pure-Python stats, H1-H4 verdicts, Pareto, and taxonomy."""

from __future__ import annotations

import math
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BootstrapCIResult(BaseModel):
    """Paired bootstrap confidence interval summary for mean pairwise difference (a - b)."""

    model_config = ConfigDict(extra="forbid")

    mean_diff: float
    ci_low: float
    ci_high: float
    n_resamples: int = Field(..., ge=1)
    alpha: float = Field(..., gt=0.0, lt=1.0)
    seed: int

    @model_validator(mode="after")
    def _validate_bounds(self) -> BootstrapCIResult:
        if not (
            math.isfinite(self.mean_diff)
            and math.isfinite(self.ci_low)
            and math.isfinite(self.ci_high)
        ):
            raise ValueError("BootstrapCIResult values must be finite floats")
        if not (self.ci_low <= self.mean_diff <= self.ci_high):
            raise ValueError(
                f"BootstrapCIResult must satisfy ci_low <= mean_diff <= ci_high, "
                f"got ci_low={self.ci_low}, mean_diff={self.mean_diff}, ci_high={self.ci_high}"
            )
        return self


class HypothesisVerdictStatus(str, Enum):
    """Verdict status for empirical research hypotheses H1-H4."""

    SUPPORTED = "SUPPORTED"
    REFUTED = "REFUTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    QUARANTINED_MOCK_OR_INCOMPLETE = "QUARANTINED_MOCK_OR_INCOMPLETE"


class HypothesisResult(BaseModel):
    """Structured statistical verdict and supporting evidence for one hypothesis (H1-H4)."""

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: Literal["H1", "H2", "H3", "H4"]
    title: str = Field(..., min_length=1)
    status: HypothesisVerdictStatus
    metrics: dict[str, Any] = Field(default_factory=dict)
    p_values: dict[str, float] = Field(default_factory=dict)
    bootstrap_cis: dict[str, BootstrapCIResult] = Field(default_factory=dict)
    rationale: str = Field(..., min_length=1)


class ErrorCategory(str, Enum):
    """Standardized 7-category error taxonomy across Layers 1-4."""

    SCHEMA_DRIFT = "SCHEMA_DRIFT"
    ALLOY_SYNTAX = "ALLOY_SYNTAX"
    ALLOY_VACUOUS = "ALLOY_VACUOUS"
    UNSAT_CONTRADICTION = "UNSAT_CONTRADICTION"
    ROUTING_AMBIGUITY = "ROUTING_AMBIGUITY"
    PBT_COUNTEREXAMPLE = "PBT_COUNTEREXAMPLE"
    QUOTA_FALLBACK = "QUOTA_FALLBACK"


class TaxonomyCategorySummary(BaseModel):
    """Aggregated error counts and sample record IDs for one taxonomy category."""

    model_config = ConfigDict(extra="forbid")

    category: ErrorCategory
    count: int = Field(..., ge=0)
    share_pct: float = Field(..., ge=0.0, le=100.0)
    sample_ids: list[str] = Field(default_factory=list)


class ErrorTaxonomyReport(BaseModel):
    """Complete 7-category error taxonomy aggregated across evaluation logs."""

    model_config = ConfigDict(extra="forbid")

    total_failures: int = Field(..., ge=0)
    categories: list[TaxonomyCategorySummary]

    @model_validator(mode="after")
    def _validate_total_consistency(self) -> ErrorTaxonomyReport:
        category_sum = sum(item.count for item in self.categories)
        if category_sum != self.total_failures:
            raise ValueError(
                f"Sum of category counts ({category_sum}) must equal "
                f"total_failures ({self.total_failures})"
            )
        return self


class ParetoPoint(BaseModel):
    """Single quality-vs-cost (or quality-vs-latency) operating point."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., min_length=1)
    quality: float
    cost: float
    is_frontier: bool = False


class AnalysisSummary(BaseModel):
    """Top-level analysis output consumed by Phase 4 reporting and release packaging."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., min_length=1)
    run_mode: str = Field(..., min_length=1)
    quarantined: bool
    quarantine_reasons: list[str] = Field(default_factory=list)
    verdict_status: Literal["VERIFIED_LIVE", "DRY_RUN_UNSCORED"]
    hypotheses: list[HypothesisResult]
    taxonomy: ErrorTaxonomyReport
    pareto_cost_points: list[ParetoPoint] = Field(default_factory=list)
    pareto_latency_points: list[ParetoPoint] = Field(default_factory=list)
    svg_files: dict[str, str] = Field(default_factory=dict)
    layer_metrics: dict[str, Any] = Field(default_factory=dict)
    hardware_profile: dict[str, Any] = Field(default_factory=dict)
    seed: int = 42
    replicates: int = Field(default=0, ge=0)


__all__ = [
    "AnalysisSummary",
    "BootstrapCIResult",
    "ErrorCategory",
    "ErrorTaxonomyReport",
    "HypothesisResult",
    "HypothesisVerdictStatus",
    "ParetoPoint",
    "TaxonomyCategorySummary",
]
