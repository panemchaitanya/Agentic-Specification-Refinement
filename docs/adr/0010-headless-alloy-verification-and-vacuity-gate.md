# 0010. Headless Alloy Verification and Vacuity Gate

- **Date:** 2026-11-24
- **Status:** Accepted
- **PR / Branch:** `adr/0010-headless-alloy-verification-and-vacuity-gate`
- **Supersedes:** None

## Context

Synthesizing Alloy specifications inside `Spec.md` (ADR-0009) without running an automated verification gate creates a severe reliability gap: LLM-generated models often contain syntax/type errors or mutually contradictory `fact`s. Worse, in formal verification, an inconsistent set of axioms makes every `check` assertion pass **vacuously** (**vacuous truth**—there are zero valid model instances, so no counterexample can exist), giving stakeholders false assurance that a broken specification is verified.

At the same time, ReqHQ v2 must run on `TIER_0` Windows 11 laptops (`16 GB` RAM) without hanging on unbounded SAT search, must translate low-level solver diagnostics into plain-language stakeholder questions, and must keep the default CI test suite 100% offline even when Java or the Alloy Analyzer JAR is not installed.

## Decision

We implement a closed-loop **Headless Alloy Verification Runner, Vacuity Gate, and Repair Controller** under `src/reqhq/alloy/` (`vacuity.py`, `runner.py`, `mock_analyzer.py`, `unsat_mapper.py`, `counterexample.py`, `repair_loop.py`), wired into `reqhq verify` and `reqhq export --out` and configured via `ReqHQConfig` (`REQHQ_ALLOY_JAR_PATH`, `REQHQ_ALLOY_COMPAT`):

1. **Bounded Headless Alloy JAR Execution (`HeadlessAlloyRunner`):**
   - Implements the `AlloyAnalyzer` Protocol by invoking `java -jar <REQHQ_ALLOY_JAR_PATH>` via `subprocess.run` with strict timeouts and laptop-safe bounded scopes and bitwidths (default `max_scope=4`, configurable via `--max-scope`).
   - Parses solver stdout/stderr into strict Pydantic `AlloyRunResult` models covering four canonical outcomes: `COMPILE_ERROR`, `VACUOUS_UNSAT`, `COUNTEREXAMPLE_FOUND`, and `VERIFIED`.
2. **Mandatory Paired Vacuity Gate (`inject_vacuity_witnesses`):**
   - Before checking any `assert`, automatically injects a paired non-trivial witness predicate (`pred __vacuity_witness_<name>`) and `run __vacuity_witness_<name> for <max_scope>` command for every assertion in `behavior.als`.
   - If any witness returns `UNSAT`, the gate fails immediately with `VACUOUS_UNSAT`, ensuring contradictory `fact`s can never masquerade as passing assertions.
3. **Minimal `UNSAT` Core Extraction & `REQ-ID` Conflict Mapping (`map_unsat_to_conflicts`):**
   - Extracts minimal `UNSAT` core line/fact references from the analyzer result and joins them against the `// @trace REQ-XXX` line maps extracted from `Spec.md` (ADR-0009).
   - Emits structured, plain-language `ConflictQuestion` objects identifying the exact conflicting `REQ-ID`s so stakeholders can resolve requirement contradictions directly.
4. **Counterexample-to-Scenario Translation (`translate_counterexample`):**
   - Translates Alloy counterexample traces into concrete step-by-step `CounterexamplePrompt` user scenarios asking the stakeholder whether the trace demonstrates a missing guardrail or an intended business exception.
5. **Bounded 2-Attempt Local Repair Loop & Audited Escalation (`AlloyVerificationController`):**
   - Executes Layer 1 static lint -> block extraction -> vacuity witness injection -> compile/verify.
   - On `COMPILE_ERROR`, performs up to `max_local_repairs=2` local SLM repair attempts; if local repair exhausts its budget, escalates through `ProviderRouter` with `EscalationReason.ALLOY_REPAIR_EXHAUSTED` (ADR-0003) and logs outcomes and escalations to `SessionEventLog` (`EventKind.VERIFICATION_RECORDED`, `EventKind.ESCALATION_TRIGGERED`).
6. **Offline CI Determinism & Opt-In Live Verification:**
   - Ships `MockAlloyAnalyzer` (accessible via `--mock-alloy`) supporting deterministic single-shot and scripted multi-step verification sequences across all four outcomes for 100% offline unit and end-to-end testing.
   - Registers `@pytest.mark.alloy_live` in `pyproject.toml` for opt-in live Alloy JAR tests that skip cleanly when `java` or `REQHQ_ALLOY_JAR_PATH` is absent.

## Consequences

- **Positive:** Eliminates vacuous truth by construction via mandatory paired `run` witness checks while keeping verification latency bounded on 16 GB Windows laptops.
- **Positive:** Bridges formal verification and stakeholder elicitation by mapping `UNSAT` cores and counterexamples back to traced `REQ-ID`s and readable scenarios instead of raw SAT traces.
- **Positive:** Preserves 100% offline CI reliability via `MockAlloyAnalyzer` while enabling real headless Alloy 6 / legacy verification whenever a local JVM and Alloy JAR are configured.
- **Trade-off:** Bounded scopes (`max_scope=4` by default) provide small-scope refutation rather than unbounded mathematical proof; users analyzing larger cardinality interactions can raise `--max-scope` at higher solver latency cost.
- **Obligation:** Any exported bundle claiming formal verification (`Spec.md`) must pass both the vacuity witness gate and bounded assertion checks (with non-vacuous status recorded in the session event log) before proceeding to Phase 3 PBT generation.
