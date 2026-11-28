# 0008. Socio-Technical Triage and Routing ADR Synthesis

- **Date:** 2026-11-24
- **Status:** Accepted
- **PR / Branch:** `adr/0008-socio-technical-triage-and-routing-adr-synthesis`
- **Supersedes:** None

## Context

Requirements engineering failures rarely stem from code alone: real systems rely on software mechanisms, human operational processes (SOPs, approvals, escalation paths), and role-based user training. Treating every requirement as a software feature inflates application complexity, produces brittle workarounds for organizational policies, and hides non-software go-live prerequisites until deployment fails.

At the same time, ReqHQ v2 already maintains **repository governance Nygard ADRs** under `docs/adr/0001`–`0010` (ADR-0001). Mixing generated target-system Routing ADRs and Assumption ADRs for a user's product into `docs/adr/` would corrupt ReqHQ's own architectural audit ledger and create numbering collisions. Furthermore, the Python 3.15.0 Windows environment (ADR-0004) intentionally avoids adding unbundled third-party YAML parsers (`PyYAML` is not installed in `.venv`).

## Decision

We implement **Stage 4a & 5a Socio-Technical Triage and Multi-Artifact Synthesis** under `src/reqhq/synthesis/` (`models.py`, `triage.py`, `routing_adr_emitter.py`, `design_emitter.py`, `process_training_emitter.py`, `bundle.py`):

1. **Socio-Technical Triad Partitioning (`SocioTechnicalTriager`):**
   - Every active `REQ-ID` in the converged `RequirementGraph` is triaged sequentially in compact `TIER_0`-safe batches via `ProviderRouter` into one of four `TriageTrack` buckets (mapping cleanly to `RoutingDecisionKind`):
     - **`Software`:** Implemented in application code and verified formally in `Spec.md` / `Design.md`.
     - **`Process`:** Fulfilled via Standard Operating Procedures (SOPs), governance gates, or human workflows.
     - **`Training`:** Fulfilled via role-based onboarding, job aids, or operator certification.
     - **`Hybrid`:** Explicitly decomposed into coordinated software, process, and training sub-responsibilities (`component_breakdown`), recording human roles, rejected alternatives, and non-software go-live prerequisites.
2. **Strict Namespace Separation (`docs/adr/` vs. `<export_dir>/ADRs/`):**
   - `docs/adr/NNNN-slug.md` remains **strictly reserved** for ReqHQ repository architecture Nygard ADRs managed by `ADRManager` (`reqhq adr`).
   - Generated target-system ADRs are written exclusively into the session's export directory (`reqhq export --out <dir>`):
     - `<export_dir>/ADRs/ADR-RXXX.md`: Target-system **Routing ADRs** documenting why each requirement (or requirement group) was routed to `Software`, `Process`, `Training`, or `Hybrid`, validated strictly against `RoutingADR` and `TraceabilityChain` (`source_sha256 -> req_id -> adr_id -> alloy_model_ref -> pbt_spec_id`).
     - `<export_dir>/ADRs/ADR-AXXX.md`: Target-system **Assumption ADRs** emitted from `finalize --accept-defaults` with `type: assumption` and `user_confirmed: false`.
   - YAML frontmatter serialization and round-trip parsing (`parse_emitted_adr`, `validate_routing_adr_markdown`) use a strict pure-Python implementation with zero external `PyYAML` dependency.
3. **Functional Core / Imperative Shell `Design.md` (`design_emitter.py`):**
   - Emits `<export_dir>/Design.md` organized around the **Functional Core, Imperative Shell** architecture—separating pure, deterministic domain state machines from I/O adapters so Phase 3 can generate Python `Hypothesis` stateful property-based tests directly against pure domain logic.
   - Includes deterministic Mermaid architecture/component diagrams and explicit `REQ-ID` and `ADR-RXXX` traceability cross-references.
4. **Operational Enablement & Zero-Orphan Traceability (`process_training_emitter.py`, `bundle.py`):**
   - Emits `<export_dir>/Process_and_Training.md` detailing SOPs, role ownership, operational checklists, go-live prerequisites, and role-based training modules for all `Process`, `Training`, and `Hybrid` requirements.
   - Emits `<export_dir>/traceability_matrix.json` (`TraceabilityMatrix`) linking every active `REQ-ID` from `source_sha256` and verbatim span through its track, `ADR-RXXX`/`ADR-AXXX` files, Alloy refs, and Phase 3 PBT hooks, enforcing **zero orphaned `REQ-ID`s**.

## Consequences

- **Positive:** Prevents over-engineering organizational workflows in software while making operational SOPs, role responsibilities, and training prerequisites first-class, traceable deliverables.
- **Positive:** Strict namespace isolation keeps `docs/adr/` pristine for repository governance while giving every exported target system its own self-contained `ADRs/` directory.
- **Positive:** Functional Core / Imperative Shell boundaries in `Design.md` create a direct architectural bridge to Phase 3 `Hypothesis` + `Pydantic` property-based testing.
- **Trade-off:** Maintaining a pure-Python YAML frontmatter subset restricts emitted frontmatter to structured scalars, lists, and flat/nested mappings; this is well suited to strict Pydantic `RoutingADR` schemas and avoids adding a native dependency on Python 3.15.
- **Obligation:** Any future emitter or CLI export option must preserve the `docs/adr/` vs. `<export_dir>/ADRs/` boundary and fail or flag immediately if any active `REQ-ID` becomes orphaned in `traceability_matrix.json`.
