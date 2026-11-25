# 0009. Two-Layer Literate Alloy 6 and Legacy Compatibility

- **Date:** 2026-11-24
- **Status:** Accepted
- **PR / Branch:** `adr/0009-two-layer-literate-alloy6-and-legacy-compat`
- **Supersedes:** None

## Context

Natural-language software specifications routinely conceal subtle state-machine violations, race conditions, and contradictory invariants that are expensive to discover during coding or production incidents. Formal relational and temporal modeling in **Alloy 6**—with native mutable relations (`var`), post-state priming (`'`), and Linear Temporal Logic (LTL) operators (`always`, `eventually`, `after`, `until`, `once`)—provides lightweight bounded verification whose state transitions and invariants map directly onto Phase 3 stateful property-based testing (`Hypothesis` `RuleBasedStateMachine`).

However, two practical architectural tensions arise:

1. **Toolchain Compatibility:** Some enterprise and academic environments run legacy **Alloy 4/5** analyzers that do not support Alloy 6 temporal keywords (`var`, `'`, LTL).
2. **Socio-Technical & Structural Coupling:** Mixing timeless domain data invariants with temporal transition encoding in a single monolithic `.als` file makes models harder for small `TIER_0` SLMs to generate and repair, and fails to represent operational boundaries where software preconditions depend on human roles or trained operators (ADR-0008).

## Decision

We adopt a **Two-Layer Literate Alloy Specification Architecture** under `src/reqhq/alloy/` (`models.py`, `emitter.py`, `extractor.py`) targeting Alloy 6 by default with deterministic legacy lowering:

1. **Literate `Spec.md` Container (`AlloySpecEmitter` & `extractor.py`):**
   - `Spec.md` combines human-readable engineering sections (System Boundary, Quantified NFR Budgets, Traceability IDs, API Contracts, and Out-of-Scope / Non-Goals linking to `Process` and `Training` Routing ADRs in `<export_dir>/ADRs/ADR-RXXX.md`) with fenced, named Alloy modules extracted cleanly by `extract_alloy_blocks()` and `write_alloy_files()`.
   - Every formal `fact`, `pred`, and `assert` carries explicit `// @trace REQ-XXX` inline comments so extracted `.als` line numbers map deterministically back to `RequirementGraph` IDs.
2. **Layer 1 — Version-Agnostic Core Relational Layer (`core_domain`, fenced as `alloy module=core_domain`):**
   - Encodes only timeless domain entities, relations, multiplicities, and structural `fact`s compatible across **Alloy 4, Alloy 5, and Alloy 6**.
   - Models socio-technical boundaries explicitly via `sig HumanActor` and `sig TrainedRole` so software invariants can depend on required human approval or certified operator roles without encoding organizational SOPs as software state machines.
   - Enforced by a mandatory static linter (`lint_core_domain_version_agnostic`) that rejects any Alloy 6 temporal keyword (`var`, `'`, `always`, `eventually`, `after`, `until`, `once`, `historically`, `since`, `triggered`, `releases`, `before`) outside comments and string literals before solver invocation.
3. **Layer 2 — Behavioral Adapter Layer (`behavior`, fenced as `alloy module=behavior`):**
   - Imports Layer 1 strictly via `open core_domain`, isolating behavioral transition semantics from core domain structure.
   - **Default (`--alloy-compat=alloy6`, `AlloyCompatMode.ALLOY6`):** Uses native Alloy 6 `var` fields, primed post-states (`'`), and LTL safety/liveness assertions (`always`, `eventually`, `after`, `until`, `once`) with bounded `check` commands, providing a direct semantic mapping to Phase 3 stateful PBT invariants.
   - **Legacy Mode (`--alloy-compat=legacy`, `AlloyCompatMode.LEGACY`):** Lowers state transitions onto `open util/ordering[Time]` with explicit time-indexed state predicates and **zero** Alloy 6 temporal keywords, enabling verification on legacy Alloy 4/5 analyzers while reusing `core_domain.als` unmodified.

## Consequences

- **Positive:** Defaulting to Alloy 6 yields concise, idiomatic temporal specifications that align naturally with Phase 3 stateful `Hypothesis` property-based testing.
- **Positive:** Enforcing a version-agnostic `core_domain` via static linting guarantees clean separation between structural domain invariants and temporal encoding, simplifying local SLM generation/repair and making `--alloy-compat=legacy` deterministic and low-risk.
- **Positive:** Explicit `sig HumanActor` and `sig TrainedRole` signatures anchor socio-technical dependencies directly inside the formal boundary without conflating software and organizational tracks.
- **Trade-off:** Expressing temporal liveness properties in `--alloy-compat=legacy` (`util/ordering[Time]`) is bounded to finite traces rather than infinite lasso traces supported by Alloy 6's temporal solver; users requiring full LTL liveness semantics should use the default `alloy6` mode.
- **Obligation:** All emitted `Spec.md` files must pass `lint_core_domain_version_agnostic` and retain `// @trace REQ-XXX` annotations on every formal constraint so headless verification (ADR-0010) can map solver diagnostics back to requirements.
