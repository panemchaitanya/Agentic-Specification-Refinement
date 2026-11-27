# 0022. Live LLM Coding Agent, VERIFIED_LIVE Evaluation Pipeline, Path Resolution & Telemetry (v2)

- **Date:** 2026-11-27
- **Status:** Accepted
- **PR / Branch:** `fix/p5-live-eval-paths-telemetry-progress`
- **Supersedes:** ADR-0021

## Context

ADR-0021 (v1) introduced `LiveLLMCodingAgent`, `EvalRunConfig.split`, and `_build_live_eval_hooks` to enable `VERIFIED_LIVE` Phase 5 evaluations. During the first full 36-trial live study invoked with a CWD-relative `--out eval_live_full` path, three defects surfaced that directly conflicted with ADR-0021 v1 path and quota-telemetry behavior:

1. `DEF-P5-002`: `write_contract_lock()` doubled CWD-relative paths already residing inside `root_resolved`.
2. `DEF-P5-003`: `LiveLLMCodingAgent._run_pytest()` passed CWD-relative `test_targets` and `bundle_dir` alongside `cwd=workspace_dir`, causing pytest exit code 4 (`file or directory not found`), `pbt_pass_rate=0.0`, and spurious repair stalls even while Condition C achieved 12/12 (100%) unseen `hidden_pass@1`.
3. `DEF-P5-004`: ADR-0021 v1's cumulative token/request counting mislabeled local Ollama calls as cloud quota in `LiveLLMCodingAgent`, `ExperimentHarness` double-counted shared SQLite `QuotaLedger` deltas, and `EvalOrchestrator.run_layer4()` triple-counted Layer 3 `QuotaLedger` deltas onto Layers 1–3, artificially dragging H2 cloud reduction from >90% down to 49.9%. Multi-minute silent runs also obscured live progress.

This v2 ADR supersedes ADR-0021 (archived in `docs/adr/archive/`) and consolidates the complete Phase 5 live evaluation architecture into a single, conflict-free record.

## Decision

We establish the unified **v2 Live LLM Coding Agent and `VERIFIED_LIVE` Evaluation Pipeline** across `src/reqhq/harness/live_agent.py`, `src/reqhq/harness/experiment.py`, `src/reqhq/pbt/contract_lock.py`, `src/reqhq/evaluation/{models,telemetry,runner}.py`, and `src/reqhq/cli.py` (extending ADR-0015):

1. **`LiveLLMCodingAgent` & Strict Condition A/B/C Isolation (`src/reqhq/harness/live_agent.py`, re-exported via `src/reqhq/harness/__init__.py`):**
   - Implements the `CodingAgent` protocol backed by `ProviderRouter` (ADR-0023 v2) with strict Pydantic v2 structured output (`_CodeGenPayload`, `ConfigDict(extra="forbid")`), fenced-code fallback extraction, `ast.parse()` syntax validation, and a bounded counterexample-guided repair loop (`max_repair_rounds=2`).
   - Enforces strict prompt isolation: `COND_A` (`raw_prd.md` only), `COND_B` (`single_pass_nl_spec.md` only), `COND_C` (`single_pass_nl_spec.md` + read-only handoff bundle `SPEC.md`, `DESIGN.md`, `PBT_Spec.yaml`, `HANDOFF_INSTRUCTIONS.md`; `pbt_lock.sha256` remains untouched).
   - Enforces zero hidden-test leakage: `hidden_tests/test_acceptance.py` is never read into any prompt or repair context and runs exclusively post-generation in an isolated subprocess.
2. **Canonical Absolute Path Resolution Across All Subprocess, Harness & Lock Boundaries (`DEF-P5-002`, `DEF-P5-003`):**
   - `EvalOrchestrator.__init__()`/`resume()`, `ExperimentHarness.run_trial()`, `LiveLLMCodingAgent.run_trial()`, and `LiveLLMCodingAgent._run_pytest()` resolve `out_dir`, `workspace_dir`, `bundle_dir`, `cwd`, `test_targets`, `pythonpath_entries`, and `hidden_test_path` via `Path.resolve()` before any lock verification or subprocess execution.
   - `write_contract_lock()` checks `candidate.resolve().relative_to(root_resolved)` before prefixing `root_path`.
3. **Cloud-Only Quota Attribution & Zero Cross-Layer Telemetry Contamination (`DEF-P5-004`):**
   - Superseding ADR-0021 v1's cumulative counting, `LiveLLMCodingAgent.run_trial()` increments `quota_requests_used` and `quota_tokens_used` strictly when `resp.cloud_verified` is `True` (local Ollama calls report `0` cloud usage).
   - `EvalOrchestrator.run_layer3()` passes `quota_ledger=None` to `ExperimentHarness` in `RunMode.LIVE` so Layer 3 cloud calls are never double-counted against the shared SQLite `QuotaLedger`.
   - `StageTelemetryCollector.finish_stage()` adds `include_ledger_delta: bool = True`, and `EvalOrchestrator.run_layer4()` passes `include_ledger_delta=False` so persisted JSONL aggregates from Layers 1–3 are never contaminated by cross-layer `QuotaLedger` deltas across resumed runs.
4. **Dev-Split-Default Config, Fail-Closed Live CLI Wiring & Real-Time Progress Streaming:**
   - `EvalRunConfig.split: Literal["dev", "all"] = "dev"` (`--split dev|all`) keeps development and pilot runs strictly on `data/frozen/gold/dev/` unless `--split all` is explicitly requested; Layer 1 catches `ProviderError` gracefully.
   - `_build_live_eval_hooks()` executes `_run_live_startup_preflight()` and wires `live_router`, `LiveLLMCodingAgent`, `HeadlessAlloyRunner`, `quota_db_path`, and `use_real_mutator=True`; missing Java 21 or Alloy 6.2 JAR fails closed with `LiveExecutionUnavailableError`.
   - `EvalOrchestrator.__init__()` and `resume()` accept optional `progress_callback: Callable[[str], None] | None = None` emitting `[L1]`, `[L2]`, `[L3 task]`, `[L3]`, and `[L4]` lines; `reqhq eval run` and `reqhq eval resume` wire `_echo_eval_progress` to `stderr` so stdout remains machine-parseable while live progress streams in real time.

## Consequences

- **Positive:** Consolidates ADR-0021 and `DEF-P5-002`/`003`/`004` into a single, conflict-free v2 specification for `VERIFIED_LIVE` evaluation.
- **Positive:** Eliminates all CWD-relative path failures across contract locking and pytest/PBT subprocesses, restoring authentic locked `pbt_pass_rate` and eliminating spurious repair stalls.
- **Positive:** Restores accurate single-source cloud-reduction telemetry across Layers 1–4 without breaking standalone `StageTelemetryCollector` or Phase 3 `ExperimentHarness` unit contracts.
- **Positive:** Streams real-time per-trial progress on `stderr` during long multi-minute live runs.
- **Obligation:** Maintain 100.00% line coverage with CWD-relative `monkeypatch.chdir` regression tests, zero hidden-test leakage PBTs, path-resolution PBTs, and cross-layer `QuotaLedger` isolation tests.
