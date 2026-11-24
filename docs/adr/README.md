# ReqHQ Architecture Decision Records (ADRs)

This directory contains the authoritative **Michael Nygard-style Architecture Decision Records (ADRs)** for ReqHQ v2 (`src/reqhq/`).

## Governance Rules (Per [ADR-0001](./0001-record-architecture-decisions.md))

1. **Format:** Every ADR follows Michael Nygard's concise structure:
   - Title heading: `# NNNN. <Short Decision Title>`
   - Metadata block: `Date`, `Status`, `PR / Branch`, `Supersedes` (and `Superseded By` when superseded)
   - Required sections: `## Context`, `## Decision`, `## Consequences`
2. **Monotonic 4-Digit Numbering:** ADR filenames follow `NNNN-kebab-case-slug.md` (e.g., `0001-record-architecture-decisions.md`). Numbers are strictly monotonic across both active ADRs (`docs/adr/`) and archived ADRs (`docs/adr/archive/`) and are **never reused**.
3. **One Separate PR per ADR:** Each new ADR or superseding ADR is proposed and reviewed in its own dedicated branch (`adr/NNNN-slug`) and pull request before implementation PRs reference it.
4. **Status Lifecycle & Archival:**
   - `Proposed` -> `Accepted` (remains in `docs/adr/`)
   - `Proposed` / `Accepted` -> `Deprecated` (moved into `docs/adr/archive/` with a Deprecation Note)
   - `Proposed` / `Accepted` -> `Superseded` (cross-linked via `Superseded By: ADR-MMMM` / `Supersedes: ADR-NNNN` and moved into `docs/adr/archive/`)
   - Historical ADRs are **never deleted**; archival preserves auditability and serves as future brownfield context (see [ADR-0006](./0006-abstract-system-context-for-brownfield-readiness.md)).
5. **Append-Only Audit Trail:** Every lifecycle transition appends a validated JSON event to [`audit_log.jsonl`](./audit_log.jsonl) and regenerates [`AUDIT_HISTORY.md`](./AUDIT_HISTORY.md) and [`INDEX.md`](./INDEX.md).

## CLI Workflow (`reqhq adr`)

```bash
# Create a new Proposed ADR on its dedicated branch
reqhq adr new "Use Alloy 6 for Formal State Verification" --pr-branch adr/0007-use-alloy-6

# Accept a Proposed ADR after PR review
reqhq adr accept 0007 --author reviewer --note "Approved in PR #42"

# Deprecate an ADR (automatically archives to docs/adr/archive/)
reqhq adr deprecate 0007 --reason "Replaced by lightweight SMT check"

# Supersede an existing ADR with a new ADR (archives old ADR and cross-links both)
reqhq adr supersede 0003 "Adopt Local vLLM Runtime"

# Synchronize INDEX.md and AUDIT_HISTORY.md deterministically from disk
reqhq adr sync-index
```

## Key Files

- [`INDEX.md`](./INDEX.md): Auto-generated catalog of active and archived ADRs.
- [`audit_log.jsonl`](./audit_log.jsonl): Append-only machine-readable lifecycle ledger.
- [`AUDIT_HISTORY.md`](./AUDIT_HISTORY.md): Auto-generated Markdown table of all ADR lifecycle events.
- [`archive/`](./archive/): Immutable home for `Deprecated` and `Superseded` ADRs.
