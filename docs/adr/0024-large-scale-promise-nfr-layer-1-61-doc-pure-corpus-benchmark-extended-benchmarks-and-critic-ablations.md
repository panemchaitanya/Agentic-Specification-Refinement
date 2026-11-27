# 0024. Large-Scale PROMISE NFR Layer 1, 61-Doc PURE Corpus Benchmark, Extended Benchmarks, and Critic Ablations

- **Date:** 2026-11-27
- **Status:** Accepted
- **PR / Branch:** `feat/large-scale-eval-promise-corpus-ablations`
- **Supersedes:** None

## Context

Phase 5 verified the end-to-end live evaluation pipeline (`ADR-0022`) on the canonical frozen dev pilot (`3` PURE gold projects in Layer 1, `16` cross-domain cases in Layer 2, and `6` greenfield coding tasks in Layer 3). While Layer 2 (`p = 0.0078`, `p = 0.0001`) and Layer 3 (`p = 0.0039`, `p = 0.0005`) achieved strong statistical significance, Layer 1 had a Wilcoxon floor of `p = 0.25` at `n = 3`, and the repository already contained significantly larger human-labeled and real-world industrial datasets on disk (`15` PROMISE NFR projects / `622` requirements and `79` PURE industrial SRS documents) that were not yet exercised at scale. Furthermore, the initial project proposal specified `B3` (zero-human-input) and per-critic ablation evaluations.

## Decision

Extend the ReqHQ v2 evaluation suite with strictly opt-in, backward-compatible large-scale evaluation capabilities across three stages:

1. **Stage A — Zero-Annotation Statistical Power Scaling (`--include-promise` and `reqhq eval corpus-bench`):**
   - Add `load_frozen_promise_nfr_gold()` in `src/reqhq/benchmark/pure_bridge.py` with fail-closed SHA-256 verification against `data/frozen/promise_nfr/FREEZE.json`, grouping the `11` human-labeled PROMISE NFR `dev` projects (`437` requirements; plus `4` held-out `test` projects / `185` requirements under explicit `--split all`) into `FrozenGoldDocument` objects.
   - Expose opt-in `EvalRunConfig.include_promise` (`--include-promise`) on `reqhq eval run` to scale Layer 1 from `n = 3` to `n = 14` on `dev` (`n = 18` on `--split all`), lowering the two-sided Wilcoxon signed-rank significance floor from `0.25` to `< 0.001`.
   - Add `CorpusBenchmarkRunner`, `CorpusDocRecord`, `CorpusBenchSummary`, and `summarize_corpus_records()` in `src/reqhq/evaluation/corpus_bench.py`, wired to `reqhq eval corpus-bench` (`--mode mock|live`, `--split dev|all`, `--max-docs`). The runner executes resumable, atomically checkpointed end-to-end ingestion -> hierarchical chunking -> decomposition -> 5-critic panel -> triage -> Alloy 6.2 verification -> locked PBT export across all `61` PURE `dev` (or `79` `--split all`) industrial SRS documents, measuring zero-truncation across `>120K`-char specifications, Alloy non-vacuity rate, PBT lock verification rate, peak RAM, and p50/p95 wall-clock latency.

2. **Stage B — Extended Benchmark Suites and Proposal Ablations (`--cross-domain-suite extended`, `--coding-suite extended`, and `reqhq eval ablate`):**
   - Add `ExtendedCrossDomainCase`, `ExtendedCrossDomainSuite`, `EXTENDED_CROSS_DOMAIN_PATH`, and `load_extended_cross_domain_suite()` in `src/reqhq/benchmark/models.py` and `src/reqhq/benchmark/cross_domain.py`, backed by `data/benchmarks/cross_domain/cases_extended.json` (`48` balanced projects across `8` domains: 24 one-liners, 24 multi-paragraph), leaving the canonical `16`-case `cases.json` untouched.
   - Add `EXTENDED_CODING_TASKS_DIR`, `EXTENDED_TASK_IDS` (`6` new greenfield tasks: `payment_idempotency_ledger`, `runway_clearance_interlock`, `breakglass_iam_elevation`, `telecom_qos_throttle_gate`, `clinical_dosage_guard`, `customs_export_hold_engine`), and `load_coding_suite("canonical"|"extended")` in `src/reqhq/evaluation/tasks.py`, expanding Layer 3 to `12` greenfield tasks (`108` trials at `replicates=3`) while indexing all `12` tasks in `LiveLLMCodingAgent` with zero hidden-test leakage.
   - Add `AblationRunner`, `ABLATION_ARMS`, `build_critics_for_arm()`, `AblationItemRecord`, `AblationArmSummary`, `AblationReport`, and `summarize_ablation_records()` in `src/reqhq/evaluation/ablations.py`, wired to `reqhq eval ablate` to execute Proposal `B3` (`b3_no_human`) and all five leave-one-critic-out arms (`minus_functional`, `minus_nfr`, `minus_logical`, `minus_process`, `minus_training`) against `full_5_critics` with atomic JSONL checkpointing.

3. **Stage C — One-Shot Blinded Held-Out Test Split Protocol:**
   - Default all CLI commands (`eval run`, `eval corpus-bench`, `eval ablate`) strictly to `--split dev`. Unlocking the held-out `18` PURE `test` SRS documents and `4` PROMISE NFR `test` projects (`185` rows) requires explicit `--split all` invocation ONLY after all code, prompts, and thresholds are frozen on `dev`. Development unit tests must use synthetic `tmp_path` test splits and never inspect real `data/frozen/*/test/` files.

## Consequences

- **Positive:** Unlocks high-power live evaluation (`n = 14..18` Layer 1 gold projects, `n = 61..79` real industrial SRS corpus docs, `n = 48` cross-domain Layer 2 projects, `n = 12` greenfield Layer 3 tasks, and `98..126` critic/B3 ablation evaluations) without requiring manual human annotation or breaking any Phase 1–5 regression test.
- **Positive:** Preserves `100.00%` line coverage, deterministic Hypothesis PBT verification, strict `Path.resolve()` boundary safety, `ignore_cleanup_errors=True` temp-dir hygiene, LF-only data files, and fail-closed `LiveExecutionUnavailableError` enforcement.
- **Trade-off:** Full live execution of Stage A + Stage B + Stage C requires substantially longer local Ollama + Alloy 6.2 wall-clock time; atomic per-unit JSONL checkpointing and `stderr` progress streaming mitigate interruption risk.
