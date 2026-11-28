# 0016. Pure-Python Stats, Pareto Charts, and Error Taxonomy

- **Date:** 2026-11-25
- **Status:** Accepted
- **PR / Branch:** `adr/0016-pure-python-stats-pareto-and-error-taxonomy`
- **Supersedes:** None

## Context

Phase 4 requires rigorous non-parametric statistical testing for `H1`–`H4` (paired bootstrap confidence intervals, exact McNemar tests for paired binary outcomes, and Wilcoxon signed-rank tests for paired continuous metrics), Pareto frontier visualizations comparing specification quality against cloud cost and latency, and structured failure categorization across Layers 1–4.

On **Python 3.15.0 on Windows (`win_amd64`)**, adding heavy compiled scientific packages (`scipy`, `matplotlib`) introduces binary-wheel fragility and large transitive native build risks—the same packaging hazard identified in ADR-0004. Furthermore, charts generated during offline `run_mode="mock"` dry-runs must visually prevent misinterpretation as empirical results.

## Decision

We implement a **Zero-Compiled-Dependency Pure-Python Statistical Analysis, Fail-Closed Hypothesis Evaluator, Pure-SVG Charting, and 7-Category Error Taxonomy Package** under `src/reqhq/analysis/` (`models.py`, `stats.py`, `hypotheses.py`, `pareto.py`, `taxonomy.py`, `pipeline.py`), wired into `reqhq eval analyze` in `src/reqhq/cli.py`:

1. **Pure-Python Stdlib Non-Parametric Statistics (`stats.py`):**
   - Implemented exclusively with stdlib `math`, `random`, and `statistics` (zero `scipy`/`numpy` dependency):
     - `paired_bootstrap_ci(a, b, *, n_resamples=2000, alpha=0.05, seed=42) -> BootstrapCIResult`: computes paired differences `d_i = a_i - b_i` under pinned `random.Random(seed)`, guaranteeing `ci_low <= mean_diff <= ci_high` across edge cases.
     - `mcnemar_exact_p(b_wins, c_wins) -> float`: exact two-sided binomial test via `math.comb`, bounded in `[0.0, 1.0]` with exact symmetry `mcnemar_exact_p(b, c) == mcnemar_exact_p(c, b)`.
     - `wilcoxon_signed_rank(a, b)`: deterministic paired signed-rank statistic and p-value handling ties and zero differences cleanly.
2. **Fail-Closed `H1`–`H4` Evaluator & Mock Quarantine (`hypotheses.py`, `pipeline.py`):**
   - Validates `eval_manifest.json` SHA-256 hashes, `run_mode == "live"`, `replicates >= 2`, `gold_verified`, and layer completeness before assigning `HypothesisVerdictStatus` (`SUPPORTED`, `REFUTED`, `INCONCLUSIVE`).
   - Whenever any quarantine precondition fails, emits `AnalysisSummary` with `verdict_status="DRY_RUN_UNSCORED"`, `quarantined=True`, and `HypothesisVerdictStatus.QUARANTINED_MOCK_OR_INCOMPLETE` for all four hypotheses—and raises `MockRunQuarantineError` when invoked with `--strict-live` (`strict=True`).
3. **Pure-Python Valid XML/SVG Pareto & Comparison Charts (`pareto.py`):**
   - Computes exact 2D Pareto frontiers (`ParetoPoint.is_frontier`) and renders four deterministic, zero-dependency XML/SVG figures validated via `xml.etree.ElementTree.fromstring()` with LF endings:
     - `pareto_quality_vs_cloud_cost.svg`
     - `pareto_quality_vs_latency.svg`
     - `cond_abc_comparison.svg`
     - `error_taxonomy_bar.svg`
   - When `quarantined=True` or `run_mode != "live"`, stamps an unmistakable **`DRY-RUN MOCK DATA`** watermark onto every emitted SVG chart.
4. **Standardized 7-Category Error Taxonomy (`taxonomy.py`):**
   - Classifies every failure event across Layers 1–4 into a closed `ErrorCategory` enum (`ConfigDict(extra="forbid")`):
     - `SCHEMA_DRIFT`
     - `ALLOY_SYNTAX`
     - `ALLOY_VACUOUS`
     - `UNSAT_CONTRADICTION`
     - `ROUTING_AMBIGUITY`
     - `PBT_COUNTEREXAMPLE`
     - `QUOTA_FALLBACK`
   - Emits `ErrorTaxonomyReport` (`TaxonomyCategorySummary` counts, `share_pct` summing cleanly, and traced `sample_ids`) persisted into `analysis_summary.json`.

## Consequences

- **Positive:** Installs and executes cleanly on Python 3.15.0 Windows with zero native wheel compilation (`scipy` and `matplotlib` completely avoided) and deterministic bit-for-bit reproducibility under pinned seeds.
- **Positive:** Pure-XML SVG figures render crisply in GitHub Markdown, browsers, and PDF exports, and are machine-validated by `xml.etree.ElementTree` in CI alongside `DRY-RUN MOCK DATA` watermarking.
- **Positive:** The closed 7-category error taxonomy pinpoints where failures originate across the neuro-symbolic stack (SLM schema drift vs. Alloy syntax/vacuity/UNSAT vs. triage ambiguity vs. PBT counterexamples vs. quota fallback).
- **Trade-off:** Pure-Python bootstrap resampling (`n_resamples=2000`) is slower than vectorized C/Fortran routines for massive sample sizes; for ReqHQ's evaluation scale (dozens of PRDs/tasks and replicates), pure Python executes in milliseconds.
- **Obligation:** All statistical functions must continue passing deterministic `Hypothesis` property-based tests (verifying CI ordering, symmetry, `[0, 1]` p-value bounds, and Pareto non-dominance) under `-W error::hypothesis.errors.HypothesisWarning`.
