# 0014. Internal PBT and Post-Swarm Verification Gate

- **Date:** 2026-11-25
- **Status:** Accepted
- **PR / Branch:** `adr/0014-internal-pbt-and-post-swarm-verification-gate`
- **Supersedes:** None

## Context

Phase 1 and Phase 2 of ReqHQ v2 surfaced three recurring engineering quality lessons during multi-agent parallel development:

1. **Example-Only Unit Tests Miss Subtle Invariants:** Unit tests alone verified happy paths and individual edge cases, whereas subtle parser/extractor bugs (such as Alloy 6.2 tabular `stderr` output formatting, intervening comment lines breaking `@trace` line mapping, and subtle edge branches across `chunker.py`, `router.py`, and `cli.py`) required dedicated follow-up audits and targeted regression fixes.
2. **Self-Reported Worker Summaries Are Insufficient:** Relying solely on individual agent self-reports risks missing stubbed paths, CRLF/LF drift in `docs/adr/`, stale index parity, or cross-module integration regressions that only surface when the full repository is verified together.
3. **Offline Mocks Alone Do Not Catch Real JVM / Alloy 6 CLI Drift:** While `MockAlloyAnalyzer` keeps default CI fast and offline (ADR-0010), live Java + Alloy 6.2 JAR invocation revealed real CLI stderr/exit-code behavior that mocked unit tests could not predict.

## Decision

We formalize a **Three-Pillar Internal Verification Gate** mandatory for Phase 3 and all subsequent phases of ReqHQ v2:

1. **Mandatory Internal `Hypothesis` Property-Based Testing in `tests/reqhq/`:**
   - Every new Phase 3+ module test suite (`test_adr_phase3.py`, `test_pbt_emitter.py`, `test_guardrail_harness.py`, `test_benchmark_oracle.py`) plus a dedicated cross-phase suite (`tests/reqhq/test_pbt_core_invariants.py`) MUST include deterministic `Hypothesis` property-based tests (`@given`, `@settings(derandomize=True, max_examples=50)` or `RuleBasedStateMachine`) alongside unit tests.
   - Core engine invariants continuously property-tested include: `PIIScrubber` round-trip reversibility, `HierarchicalChunker` zero-truncation (`truncation_ratio == 0.0`), `QuotaLedger` 90%-ceiling monotonicity, `PBT_Spec -> codegen -> ast.parse -> verify_contract_lock`, `GapInjector` ratio bounds and zero verbatim leakage, `cohens_kappa` symmetry/bounds, and `ADRManager` monotonic numbering + `sync_index()` idempotency.
2. **Independent Post-Swarm Deliverable Audit (Zero Trust in Self-Reports):**
   - Before any phase check-in plan is finalized, an independent verifier physically inspects every created and modified file for: existence, non-empty implementation, zero `TODO`/`NotImplementedError` stubs, clean imports, valid AST/YAML/JSON/Markdown, strict LF (`\n`) line endings on `docs/adr/**`, mandatory `Hypothesis` PBT presence in every Phase 3+ test file, and zero edits to immutable legacy paths (`src/specpipe/`, `data/frozen/`, legacy `tests/test_*.py`).
3. **Zero-Failure Full-Suite & Live Alloy 6 Re-Verification Gate:**
   - After the final edit (and after any bounced defect fix), the verifier MUST re-run all four commands cleanly from scratch via `.venv/Scripts/python.exe` and record verbatim outputs in the check-in plan:
     1. Legacy suite: `.venv/Scripts/python.exe -m pytest --ignore=tests/reqhq` (**87 passed**, 0 legacy mutations).
     2. Full `reqhq` unit + Hypothesis PBT suite with coverage: `.venv/Scripts/python.exe -m pytest tests/reqhq --cov=src/reqhq --cov-report=term-missing` (**0 failures**, `>= 90%` required floor, targeting `~98–100%` line coverage).
     3. Live Java + Alloy 6 regression gate: `.venv/Scripts/python.exe -m pytest tests/reqhq -m alloy_live -vv` (**1 passed, 0 skipped, 0 failed** whenever Temurin JRE and `REQHQ_ALLOY_JAR_PATH` are present).
     4. Full combined suite: `.venv/Scripts/python.exe -m pytest` (**0 failures**).

## Consequences

- **Positive:** Catches edge-case regressions, parser mismatches, and cross-module integration drift automatically before PR creation—practicing on `reqhq` itself the same contract-first PBT discipline `reqhq` enforces on target systems (ADR-0011).
- **Positive:** Deterministic `@settings(derandomize=True, max_examples=50)` keeps Hypothesis PBT runs reproducible and fast (< 10 seconds) on Windows 11 `TIER_0` laptops.
- **Trade-off:** Independent post-swarm auditing and full re-verification add a dedicated verification turn at the end of each phase; this cost is small compared to debugging broken merges or regressing legacy baselines.
- **Obligation:** Future ADR tests must assert monotonic growth (`>= N` and prefix/subset membership rather than brittle exact totals) so subsequent phases can append new ADRs cleanly without breaking prior phase tests.
