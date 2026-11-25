# 0012. Antigravity Handoff and Windows Mutation Harness

- **Date:** 2026-11-25
- **Status:** Accepted
- **PR / Branch:** `adr/0012-antigravity-handoff-and-windows-mutation-harness`
- **Supersedes:** None

## Context

Evaluating whether ReqHQ v2's upstream specification refinement genuinely improves downstream agentic code generation requires a reproducible, objective experimental harness. Two major risks threaten validity and portability on our target workstation environment (Windows 11, Python 3.15.0):

1. **Subjective LLM-as-a-Judge Bias:** Scoring generated code with another LLM introduces non-determinism, self-preference bias, and unverifiable verdicts. Objective evaluation requires executable, deterministic software-engineering oracles (`pytest`, `Hypothesis` PBT suites, contract lock checks, and mutation testing).
2. **Windows 11 Mutation Testing Incompatibility:** Standard Python mutation tools such as `mutmut` rely on POSIX `os.fork()`, which does not exist on Windows 11. Depending solely on `mutmut` would make mutation-score measurement unusable on developer laptops and Windows CI runners.

## Decision

We implement a **Reproducible Antigravity Handoff Bundler, Condition A/B/C Experiment Harness, and Pluggable Windows-Native AST Mutation Engine** under `src/reqhq/harness/` (`models.py`, `handoff.py`, `ast_mutator.py`, `mutation.py`, `experiment.py`), exposed via `reqhq handoff <session_id> --out <dir>`:

1. **Self-Contained Antigravity Handoff Bundle (`build_handoff_bundle`):**
   - Assembles a complete, portable handoff directory (`HandoffBundleManifest`) containing `Spec.md`, `<out_dir>/ADRs/` (`ADR-RXXX.md` and `ADR-AXXX.md`, strictly separate from repo `docs/adr/`), `Design.md`, `Process_and_Training.md`, `traceability_matrix.json` (with full `pbt_refs`), `PBT_Spec.yaml`, `guardrails/`, locked `tests/properties/`, `pbt_lock.sha256`, and `HANDOFF_INSTRUCTIONS.md` encoding the **Contract-First Verification Gate**.
2. **Condition A / B / C Controlled Experiment Runner (`ExperimentCondition`):**
   - Runs controlled, seeded trials comparing three input regimes under identical coding-agent interfaces:
     - `COND_A = "raw_prd"`: Raw PRD / prompt only.
     - `COND_B = "single_pass_nl_spec"`: Single-pass natural-language specification.
     - `COND_C = "reqhq_full_bundle"`: Full ReqHQ v2 bundle (`Spec.md` Alloy + `ADRs/` + `Design.md` + locked `PBT_Spec.yaml` & guardrails).
   - Emits strict `TrialResultRow` records capturing exclusively **zero-LLM-judge deterministic metrics**: `hidden_pass_at_1`, `pbt_pass_rate`, `mutation_score`, `lock_verified`, `stall_count`, `wall_clock_seconds`, `quota_tokens_used`, and `quota_requests_used`.
3. **Pluggable `MutationRunner` Protocol with Windows-Native AST Engine (`ast_mutator.py`):**
   - Defines a `MutationRunner` Protocol backed by two concrete runners:
     - **`ASTMutantEngine` (Default on Windows 11 / Python 3.15):** Pure-Python AST mutator applying deterministic single-site mutations across relational operators (`<` <-> `<=`, `>` <-> `>=`, `==` <-> `!=`), boolean operators (`and` <-> `or`, `not` toggling), arithmetic (`+` <-> `-`), return values, and boundary numeric constants (`0 -> 1`, `n -> n + 1`) via `ast.unparse`. Each mutant runs inside an isolated temporary workspace via `subprocess.run([sys.executable, "-m", "pytest", "-q", ...])` with strict timeouts, classifying outcomes into `MutantOutcome` (`KILLED`, `SURVIVED`, `TIMEOUT`, `ERROR`) and computing exact `mutation_score`.
     - **`MutmutRunner` (POSIX-Gated Adapter):** Opt-in wrapper delegating to `mutmut` only on POSIX platforms where `os.fork()` and `mutmut` are available, failing cleanly with an explicit platform guard on Windows.

## Consequences

- **Positive:** Produces reproducible, publication-grade Condition A/B/C evidence grounded 100% in executable deterministic oracles (`pytest`, `Hypothesis`, SHA-256 contract locks, and mutation score) with zero LLM-judge bias.
- **Positive:** Eliminates the Windows 11 `fork()` blocker via a zero-dependency stdlib `ast` mutation engine while preserving a clean adapter seam for `mutmut` on Linux/macOS CI.
- **Trade-off:** Running a separate subprocess per AST mutant in isolated temp directories is slower than copy-on-write `fork()` on large codebases; scoping mutations strictly to the target Functional Core module keeps laptop trial runtimes fast and bounded.
- **Obligation:** Any benchmark report comparing Conditions A, B, and C must record `lock_verified` alongside `pbt_pass_rate` and `mutation_score` so tampered test suites are immediately rejected.
