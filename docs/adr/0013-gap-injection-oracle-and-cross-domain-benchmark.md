# 0013. Gap-Injection Oracle and Cross-Domain Benchmark

- **Date:** 2026-11-25
- **Status:** Accepted
- **PR / Branch:** `adr/0013-gap-injection-oracle-and-cross-domain-benchmark`
- **Supersedes:** None

## Context

Evaluating Layer 1 interactive elicitation convergence (ADR-0007) and Layer 2 socio-technical triage quality (ADR-0008) requires rigorous, leak-proof benchmarks:

1. **Automated Elicitation Recall Measurement:** Manually simulating stakeholder Q&A across dozens of trials is slow and inconsistent, whereas naive LLM-simulated users accidentally leak hidden ground-truth details or volunteer unasked answers, inflating recall artificially.
2. **Legacy Frozen Dataset Integrity:** `data/frozen/` preserves the mid-submission PURE and PROMISE NFR evaluation splits (`cctns`, `gamma_j`, `dii`, `promise_nfr/FREEZE.json`, per ADR-0002). Reusing these frozen splits in v2 must remain strictly read-only and fail closed against unverified LLM-suggested labels or skipped verdicts.
3. **Cross-Domain Generalization & Inter-Rater Reliability:** Evaluating only one domain or using a single rater risks overfitting and subjective rubric drift; human adjudication requires exact, auditable inter-rater agreement (`Cohen's kappa`).

## Decision

We implement an **Offline Gap-Injection Oracle, Fail-Closed Frozen Dataset Bridge, Curated 16-Case Cross-Domain Benchmark, and Dual-Annotator Adjudication Suite** under `src/reqhq/benchmark/` (`models.py`, `gap_injector.py`, `oracle_sim.py`, `cross_domain.py`, `adjudication.py`, `pure_bridge.py`) and `data/benchmarks/cross_domain/cases.json`, wired into `reqhq benchmark run` and `reqhq benchmark kappa`:

1. **Seeded 20–40% Gap-Injection Generator (`GapInjector`):**
   - Deterministically strips `round(len(clauses) * drop_ratio)` clauses (`0.20 <= drop_ratio <= 0.40`, seeded via `seed=42`) from a structured ground-truth specification into a `degraded_prd` and hidden `GoldGapManifest`.
   - Enforces a hard **zero-verbatim-leakage guard** raising `ValueError` if any stripped clause text appears verbatim in `degraded_prd`.
2. **Leak-Proof Frozen Oracle Simulator (`FrozenOracleSimulator`):**
   - Answers elicitation questions exclusively by matching against hidden `GoldGapManifest.stripped_clauses`—never volunteering unasked facts.
   - Returns exact `NOT_SPECIFIED_REPLY = "Not specified in ground truth"` when no hidden clause matches, flags repeat queries of already-revealed clauses (`is_redundant=True`), and computes exact `OracleEvalMetrics` (`gap_recall`, `precision`, `f1`, `questions_to_convergence`, `redundant_question_rate`).
3. **Fail-Closed Read-Only Bridge to `data/frozen/` (`pure_bridge.py`):**
   - Reads `data/frozen/gold/dev/` (human-verified `FROZEN` docs `cctns`, `gamma_j`, `dii`) and `data/frozen/promise_nfr/FREEZE.json` strictly read-only without ever mutating `data/frozen/`.
   - `validate_gold_record_dict()` fails closed (`ValueError`) on unverified LLM-suggested labels, missing `verified_by` attestations, non-`FROZEN` status, or skipped verdicts.
4. **Curated 16-Case Cross-Domain Socio-Technical Benchmark (`data/benchmarks/cross_domain/cases.json`):**
   - Provides 16 realistic benchmark cases across 4 domains (`fintech_settlements`, `healthcare_operations`, `inventory_logistics`, `enterprise_it_hr`)—4 cases per domain (2 `one_liner` + 2 `multi_paragraph` = 8 one-liners + 8 multi-paragraph PRDs)—spanning `software`, `process`, `training`, and `hybrid` tracks with gold atomic requirements and ADR rubric anchors, runnable via `reqhq benchmark run --suite cross_domain|gap_injection`.
5. **Dual-Annotator Cohen's Kappa Tooling (`adjudication.py`):**
   - Computes exact `cohens_kappa(labels_a, labels_b)` (bounded in `[-1.0, 1.0]`, symmetric, returning `1.0` on perfect including single-class agreement) and `adjudicate_annotations()` with fail-closed refusal (`ValueError`) on `skipped=True`, `verified=False`, mismatched item IDs, or empty intersections, exposed via `reqhq benchmark kappa <file_a> <file_b>`.

## Consequences

- **Positive:** Enables repeatable, leak-proof automated measurement of elicitation convergence recall and question efficiency without human fatigue or LLM-simulator leakage.
- **Positive:** Bridges Phase 1/v1 `data/frozen/` assets safely into v2 benchmarking while enforcing zero mutation and fail-closed provenance checks.
- **Positive:** Standardizes cross-domain socio-technical evaluation and dual-annotator adjudication (`Cohen's kappa`) directly inside the `reqhq benchmark` CLI.
- **Trade-off:** Keyword/semantic matching inside an offline `FrozenOracleSimulator` is intentionally conservative compared to an open-ended human stakeholder; this ensures zero false leakage at the cost of requiring focused, specific elicitation questions.
- **Obligation:** All benchmark suites must remain deterministic under explicit `--seed` values and must never modify `data/frozen/` or legacy `src/specpipe/` evaluation artifacts.
