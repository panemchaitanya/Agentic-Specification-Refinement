# 0021. Live LLM Coding Agent and VERIFIED_LIVE Evaluation Pipeline

- **Date:** 2026-11-26
- **Status:** Superseded
- **PR / Branch:** `adr/0021-live-llm-coding-agent-and-verified-live-eval-pipeline`
- **Supersedes:** None
- **Superseded By:** ADR-0022

## Context

Phase 4 (ADR-0015 through ADR-0017) delivered the resumable Layers 1-4 evaluation orchestrator (`EvalOrchestrator`), 6 greenfield coding tasks with hidden acceptance suites (`data/benchmarks/coding_tasks/`), pure-Python statistical analysis with mock quarantine (`MockRunQuarantineError`), and SHA-256 release packaging. However, `reqhq eval run --mode live` and `reqhq eval resume` intentionally failed closed with `LiveExecutionUnavailableError` because no concrete live `CodingAgent` implementation or CLI live-hook factory existed yet. Additionally, `EvalOrchestrator._load_verified_gold_documents()` previously loaded `gold/test` whenever present on disk, whereas disciplined development and pilot evaluations must default strictly to the frozen `dev` split until an explicit final study run.

## Decision

We implement a production **Live LLM Coding Agent and `VERIFIED_LIVE` Evaluation Wiring** under `src/reqhq/harness/live_agent.py`, `src/reqhq/evaluation/`, and `src/reqhq/cli.py`:

1. **`LiveLLMCodingAgent` (`src/reqhq/harness/live_agent.py`, re-exported via `src/reqhq/harness/__init__.py`):**
   - Implements the `CodingAgent` protocol (`run_trial(...) -> AgentRunResult`) backed by `ProviderRouter` (local-first Ollama SLM with audited Gemini/Groq escalation).
   - Enforces **strict Condition A / B / C information isolation** per task:
     - **Condition A (`COND_A`, `raw_prd`):** Prompt includes ONLY `raw_prd.md` plus the minimal `<target_module>.<entry_symbol>` contract (never reads `single_pass_nl_spec.md`, `bundle_dir`, or `hidden_tests/`).
     - **Condition B (`COND_B`, `single_pass_nl_spec`):** Prompt includes ONLY `single_pass_nl_spec.md` (never reads `bundle_dir` or `hidden_tests/`).
     - **Condition C (`COND_C`, `reqhq_full_bundle`):** Prompt includes `single_pass_nl_spec.md` plus read-only ReqHQ handoff artifacts (`SPEC.md`, `DESIGN.md`, `PBT_Spec.yaml`, `HANDOFF_INSTRUCTIONS.md`) from `bundle_dir` without ever mutating locked files (`pbt_lock.sha256` remains intact).
   - Enforces **zero hidden-test leakage**: `hidden_tests/test_acceptance.py` is NEVER read into any LLM prompt or repair context; it is executed exclusively post-generation in an isolated Python subprocess (`PYTHONPATH=<workspace_dir>`) to score authentic `hidden_pass_at_1`.
   - Uses strict Pydantic v2 structured output (`_CodeGenPayload`, `ConfigDict(extra="forbid")`) with fenced-code fallback extraction, `ast.parse()` syntax validation, and a bounded counterexample-guided repair loop (`max_repair_rounds=2`) against the generated tests and (for Condition C) the locked `bundle_dir/tests/properties/` Hypothesis suite.
   - Emits authentic unified diffs (`difflib.unified_diff`), real pytest stdout/stderr, parsed `pbt_pass_rate`, `stall_count`, and cumulative token/request telemetry.
2. **Dev-Split-Default Evaluation Config (`EvalRunConfig.split`, `src/reqhq/evaluation/models.py` & `runner.py`):**
   - `EvalRunConfig` adds `split: Literal["dev", "all"] = "dev"` (exposed via `reqhq eval run --split dev|all`). Defaulting to `dev` guarantees development and pilot runs never touch `data/frozen/gold/test/` unless the researcher explicitly invokes `--split all` for a blinded final evaluation.
   - `EvalOrchestrator.run_layer1()` catches `ProviderError` cleanly in live mode and logs `QUOTA_FALLBACK` / `PROVIDER_ERROR` telemetry instead of aborting the study.
3. **Fail-Closed Live Eval CLI Wiring (`_build_live_eval_hooks` in `src/reqhq/cli.py`):**
   - `reqhq eval run --mode live` and `reqhq eval resume` (when checkpoint mode is `live`) automatically run `_run_live_startup_preflight()` and construct `live_router` (`_build_router`), `live_coding_agent` (`LiveLLMCodingAgent`), `live_alloy_analyzer` (`HeadlessAlloyRunner`), `quota_db_path`, and `use_real_mutator=True` (`ASTMutantEngine`).
   - If Java 21 or the Alloy 6.2 JAR (`REQHQ_ALLOY_JAR_PATH`) is unavailable in live mode, `_build_live_eval_hooks()` fails closed with `LiveExecutionUnavailableError`—never silently falling back to `MockAlloyAnalyzer` or `MockCodingAgent`.

## Consequences

- **Positive:** Unlocks genuine `VERIFIED_LIVE` empirical runs (`reqhq eval run --mode live` -> `analyze --strict-live` -> `report` -> `package` -> `verify-bundle`) with authentic hidden-test `pass@1`, locked Hypothesis `pbt_pass_rate`, and Windows `ASTMutantEngine` mutation scores.
- **Positive:** Enforces airtight scientific validity: zero hidden-test leakage, strict Condition A/B/C isolation, read-only Condition C contract locks, dev-split default hygiene, and fail-closed live Alloy/LLM guards.
- **Trade-off:** Live Condition C runs execute real subprocess pytest/Hypothesis and AST mutation passes per replicate, increasing wall-clock duration on `TIER_0` hardware; atomic per-unit checkpointing (`eval resume`) ensures zero lost progress.
- **Obligation:** `src/reqhq/harness/live_agent.py` and all updated evaluation/CLI modules must maintain 100.00% offline line coverage with deterministic Hypothesis PBTs verifying zero hidden-test leakage and bounded `pbt_pass_rate` parsing.

## Supersession Note

Superseded by ADR-0022 (Live LLM Coding Agent, VERIFIED_LIVE Evaluation Pipeline, Path Resolution & Telemetry (v2)) on 2026-11-27.
