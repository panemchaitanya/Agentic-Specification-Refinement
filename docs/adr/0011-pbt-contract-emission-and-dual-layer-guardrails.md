# 0011. PBT Contract Emission and Dual-Layer Guardrails

- **Date:** 2026-11-25
- **Status:** Accepted
- **PR / Branch:** `adr/0011-pbt-contract-emission-and-dual-layer-guardrails`
- **Supersedes:** None

## Context

Phase 2 produces formally verified, non-vacuous Alloy 6 specifications (`Spec.md`), socio-technical Routing and Assumption ADRs (`<export_dir>/ADRs/ADR-RXXX.md`, `ADR-AXXX.md`), and Functional Core / Imperative Shell `Design.md` documents (ADRs 0007–0010). However, when downstream coding agents implement software from natural-language and formal specs alone, two recurring failure modes remain:

1. **Specification-to-Code Drift:** Pure documentation does not execute against Python implementations; subtle boundary bugs, missing preconditions, and state-transition violations slip past example-based unit tests.
2. **Assertion Weakening by Coding Agents:** When coding agents are allowed to edit generated test suites freely, they frequently weaken failing assertions or relax input strategies to make tests pass rather than fixing the implementation.

Furthermore, ReqHQ v2 runs on Python 3.15.0 Windows without `PyYAML` installed, and must preserve strict namespace separation between repository governance Nygard ADRs (`docs/adr/0001`–`0014`) and generated target-system artifacts inside each session export directory.

## Decision

We implement **Phase 3 PBT Contract Emission, Dual-Layer Python Guardrail Codegen, and SHA-256 Contract Locking** under `src/reqhq/pbt/` (`models.py`, `emitter.py`, `codegen.py`, `contract_lock.py`, `counterexample_feedback.py`, with `export_pbt_bundle` in `codegen.py`), exposed via `reqhq pbt-export <session_id> --out <dir>`:

1. **Strict `PBT_Spec.yaml` Contract Emission (`PBTSpecEmitter`):**
   - Translates verified literate Alloy 6 blocks (`assert`/`pred`/`sig` + `// @trace REQ-XXX` via `extract_alloy_blocks` and `build_req_to_alloy_refs`), Routing ADRs (`ADRs/ADR-RXXX.md`), and `Design.md` symbols into a strict Pydantic v2 `PBTContractDocument` (`ConfigDict(extra="forbid")`, wrapping Phase 1 `PBTSpec`, `PropertyInvariant`, `HypothesisStrategySpec`, and `PydanticGuardrail`).
   - Preserves end-to-end provenance (`source_sha256 -> REQ-ID -> ADR-ID -> alloy_ref -> property_id -> target_symbol`) and serializes/parses `PBT_Spec.yaml` via a deterministic pure-Python YAML serializer/parser (`dump_pbt_spec_yaml`, `load_pbt_spec_yaml`) with zero `PyYAML` dependency.
   - Supports optional `TIER_0`-safe `ProviderRouter` enrichment alongside deterministic rule-based extraction.
2. **Dual-Layer Python Guardrail Codegen (`compile_pbt_guardrails`):**
   - **Runtime Guardrails (`guardrails/<module>_guards.py`):** Emits strict `Pydantic v2` (`ConfigDict(extra="forbid")`) domain validator models and `@enforce_contract` precondition/postcondition runtime decorators for properties configured with `GuardrailEnforcement.RUNTIME_ASSERT` or `GuardrailEnforcement.BOTH`.
   - **Test-Time Property Suites (`tests/properties/test_<module>_pbt.py`):** Emits syntactically valid, runnable `Hypothesis` stateless (`@given` + `@settings(derandomize=True, max_examples>=200)`) and stateful (`RuleBasedStateMachine`) property tests covering `PropertyKind` (`INVARIANT`, `POSTCONDITION`, `IDEMPOTENCY`, `ROUNDTRIP`, `STATEFUL_TRANSITION`).
3. **SHA-256 Contract Locking (`pbt_lock.sha256`):**
   - Hashes `PBT_Spec.yaml`, emitted `guardrails/*.py`, and emitted `tests/properties/*.py` into a deterministic `pbt_lock.sha256` manifest.
   - `verify_contract_lock()` recomputes SHA-256 digests before evaluation and raises `ContractTamperError` if a coding agent alters, deletes, or weakens any locked contract or assertion file.
4. **Shrunk Counterexample Feedback (`CounterexampleRepairReport`):**
   - Parses failing `Hypothesis` shrunk counterexamples and assertion errors into structured `CounterexampleRepairReport` records classified under `CounterexampleClassification` (`CODE_DEFECT`, `SPEC_AMBIGUITY`, `STRATEGY_MISMATCH`) with actionable repair guidance linked back to `property_id` and `req_id`.

## Consequences

- **Positive:** Closes the formal-spec-to-code gap by turning verified Alloy invariants and Routing ADRs directly into executable runtime validators and 200+-example deterministic `Hypothesis` tests.
- **Positive:** `pbt_lock.sha256` enforces an immutable contract-first boundary that prevents downstream coding agents from gaming test suites by weakening assertions.
- **Positive:** Pure-Python `PBT_Spec.yaml` round-tripping maintains Python 3.15.0 Windows zero-compilation portability while strictly separating exported target-system artifacts from `docs/adr/`.
- **Trade-off:** Automated codegen targets Python Functional Core modules (`Pydantic v2` + `Hypothesis`); non-Python target stacks will require language-specific codegen adapters in a future phase.
- **Obligation:** Downstream handoff bundles (ADR-0012) must populate `pbt_refs` in `traceability_matrix.json` and run `verify_contract_lock()` before accepting any coding-agent submission.
