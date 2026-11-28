# 0006. Abstract System Context for Brownfield Readiness

- **Date:** 2026-11-24
- **Status:** Accepted
- **PR / Branch:** `adr/0006-abstract-system-context-for-brownfield-readiness`
- **Supersedes:** None

## Context

ReqHQ v2 Phase 1 focuses on **greenfield specification refinement**—turning newly authored PRDs, notes, and product briefs into trace-linked requirement graphs, routing ADRs, and property-based test specifications. However, Phase 2 of ReqHQ v2 explicitly targets **brownfield engineering**, where new requirements must be reconciled against an existing software system's architecture, codebase structure, formal invariants, and historical architectural decisions.

If Phase 1 hard-codes greenfield assumptions (e.g., assuming no prior modules, no existing Alloy invariants, and no prior ADRs) directly into prompts, schemas, and pipeline orchestration, Phase 2 brownfield support will require disruptive breaking changes across schemas, event logs, and CLI interfaces.

## Decision

We introduce an explicit **SystemContext abstraction seam** in `src/reqhq/schemas/system_context.py` from Day 1:

1. **Abstract Interface (`SystemContext` Protocol, `SystemContextBase` ABC & `BrownfieldArtifactRef`):**
   - Defines a minimal, strict contract (`mode`, `is_empty() -> bool`, `to_prompt_context() -> str`, `summary_metadata() -> dict[str, Any]`, plus `BrownfieldArtifactRef` and `SystemContextBase.brownfield_extension_points()` declaring structured extension slots for architectural summaries, AST symbols, existing formal invariants, and superseded ADRs).
2. **Phase 1 Concrete Implementation (`GreenfieldEmptyContext`):**
   - Implements `SystemContext` with `mode: Literal["greenfield"] = "greenfield"`, `is_empty() == True`, and a deterministic empty/greenfield `to_prompt_context()` representation, validated with Pydantic `ConfigDict(extra="forbid")`.
   - Serves as the default injected context across all Phase 1 greenfield sessions.
3. **Reserved Phase 2 Brownfield Extension Seam:**
   - Phase 2 will introduce a concrete `BrownfieldRepoContext` (implementing the same `SystemContext` contract) populated by repository analyzers reading:
     - `ARCHITECTURE.md` and service/module boundary maps,
     - Static code AST symbol graphs and public API surfaces,
     - Legacy Alloy (`.als`) models and existing PBT invariants,
     - Active ADRs (`docs/adr/`) and archived/superseded ADRs (`docs/adr/archive/`, per ADR-0001).

## Consequences

- **Positive:** Phase 1 stays lean, fast, and 100% focused on greenfield requirements without premature AST parsers or repository indexers.
- **Positive:** Phase 2 brownfield capabilities plug cleanly into the existing `SystemContext` seam without breaking Phase 1 schemas, `SessionEventLog` records, or CLI workflows.
- **Positive:** Archiving deprecated/superseded ADRs in `docs/adr/archive/` (ADR-0001) immediately produces structured historical records ready for Phase 2 brownfield conflict detection.
- **Trade-off:** Maintaining an empty context wrapper adds a small abstraction layer in Phase 1; this cost is minimal (one lightweight Pydantic model) compared to retrofitting brownfield context across all agents later.
