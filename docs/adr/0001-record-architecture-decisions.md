# 0001. Record Architecture Decisions

- **Date:** 2026-11-24
- **Status:** Accepted
- **PR / Branch:** `adr/0001-record-architecture-decisions`
- **Supersedes:** None

## Context

ReqHQ v2 introduces a greenfield local-first agentic requirements-engineering engine (`src/reqhq/`) alongside the frozen mid-submission research baseline (`src/specpipe/`). Multiple architectural trade-offs—covering hardware-tiered local SLM execution, gated free-tier cloud escalation, Python 3.15 Windows wheel compatibility, immutable ingestion snapshots, append-only event sourcing, and brownfield extensibility—must remain auditable, reviewable, and traceable over time.

Without a disciplined decision log, architectural rationale erodes into commit messages or chat threads, making future revisions risky and depriving Phase 2 brownfield analysis of historical context.

## Decision

We adopt **Michael Nygard-style Architecture Decision Records (ADRs)** stored in `docs/adr/` and governed by the following non-negotiable conventions:

1. **Concise Nygard Format:** Each ADR lives in `docs/adr/NNNN-kebab-case-slug.md` with a metadata block (`Date`, `Status`, `PR / Branch`, `Supersedes`, and `Superseded By` when superseded) followed by three mandatory sections: `## Context`, `## Decision`, and `## Consequences`.
2. **Strictly Monotonic 4-Digit Numbering:** ADR identifiers (`0001`, `0002`, ...) increment monotonically across both active (`docs/adr/`) and archived (`docs/adr/archive/`) records. Numbers are never reused.
3. **One Separate PR per ADR:** Each new or superseding ADR is proposed and reviewed on its own dedicated branch (`adr/NNNN-slug`) and pull request so architectural review stays decoupled from implementation diffs. Implementation PRs explicitly cite the accepted ADR IDs they realize.
4. **Status Lifecycle & Archival (Never Delete):**
   - Valid statuses are `Proposed`, `Accepted`, `Deprecated`, and `Superseded`.
   - When an ADR is `Deprecated` or `Superseded`, its metadata is updated (including `Superseded By: ADR-MMMM` when superseded), a rationale note is appended, and the file is moved into `docs/adr/archive/`. Historical ADRs are never deleted.
5. **Append-Only Audit Trail & Automated Sync:**
   - Every lifecycle event (`created`, `accepted`, `deprecated`, `superseded`, `archived`) is appended as a strict Pydantic-validated JSON object to `docs/adr/audit_log.jsonl`.
   - `docs/adr/INDEX.md` and `docs/adr/AUDIT_HISTORY.md` are deterministically regenerated from disk and `audit_log.jsonl` via `ADRManager` (`src/reqhq/adr/manager.py`) and the `reqhq adr` CLI (`new`, `accept`, `deprecate`, `supersede`, `sync-index`).

## Consequences

- **Positive:** Every major architectural choice has an immutable, human- and machine-readable record reviewed in isolation before code merges. Archived ADRs preserve institutional memory and become direct inputs to Phase 2 brownfield context loaders (ADR-0006).
- **Positive:** Automated lifecycle tooling (`ADRManager` + `reqhq adr`) eliminates manual numbering collisions, stale indices, and undocumented status edits.
- **Trade-off:** Opening a separate PR per ADR adds minor Git workflow overhead compared to bundling docs inside feature PRs; this is mitigated by `reqhq adr` automation and explicit PR checklists.
- **Obligation:** Contributors must run `reqhq adr sync-index` (or use `reqhq adr` commands) whenever adding or transitioning ADRs so `INDEX.md`, `AUDIT_HISTORY.md`, and `audit_log.jsonl` remain synchronized.
