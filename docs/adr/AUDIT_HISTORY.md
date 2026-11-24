# ADR Audit History

> Auto-generated from the append-only [`audit_log.jsonl`](./audit_log.jsonl) ledger. Do not delete historical events; append new lifecycle transitions via `ADRManager` or `reqhq adr`.

- **Total Audit Events:** 12
- **Created:** 6 | **Accepted:** 6 | **Deprecated:** 0 | **Superseded:** 0 | **Archived:** 0

| Timestamp (UTC) | Action | ADR | Transition | Author | Related | Path | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-11-24T08:42:00Z | `created` | ADR-0001 | Proposed | adr-architect | — | `0001-record-architecture-decisions.md` | Drafted foundational ADR-0001 on branch adr/0001-record-architecture-decisions |
| 2026-11-24T08:42:01Z | `accepted` | ADR-0001 | Proposed -> Accepted | adr-architect | — | `0001-record-architecture-decisions.md` | Accepted foundational governance ADR-0001 for ReqHQ v2 Phase 1 |
| 2026-11-24T08:42:02Z | `created` | ADR-0002 | Proposed | adr-architect | — | `0002-isolate-v2-engine-under-src-reqhq.md` | Drafted foundational ADR-0002 on branch adr/0002-isolate-v2-engine-under-src-reqhq |
| 2026-11-24T08:42:03Z | `accepted` | ADR-0002 | Proposed -> Accepted | adr-architect | — | `0002-isolate-v2-engine-under-src-reqhq.md` | Accepted v2 isolation ADR-0002 preserving src/specpipe and 87 legacy tests |
| 2026-11-24T08:42:04Z | `created` | ADR-0003 | Proposed | adr-architect | — | `0003-hardware-aware-local-slm-and-free-tier-router.md` | Drafted foundational ADR-0003 on branch adr/0003-hardware-aware-local-slm-and-free-tier-router |
| 2026-11-24T08:42:05Z | `accepted` | ADR-0003 | Proposed -> Accepted | adr-architect | — | `0003-hardware-aware-local-slm-and-free-tier-router.md` | Accepted local-first router, hardware tiers T0-T3, quota ledger, and PII gate ADR-0003 |
| 2026-11-24T08:42:06Z | `created` | ADR-0004 | Proposed | adr-architect | — | `0004-day1-cloud-connectors-and-hierarchical-chunking.md` | Drafted foundational ADR-0004 on branch adr/0004-day1-cloud-connectors-and-hierarchical-chunking |
| 2026-11-24T08:42:07Z | `accepted` | ADR-0004 | Proposed -> Accepted | adr-architect | — | `0004-day1-cloud-connectors-and-hierarchical-chunking.md` | Accepted Day 1 read-only connectors, Python 3.15 wheel-safe parsers, and 0%-truncation chunker ADR-0004 |
| 2026-11-24T08:42:08Z | `created` | ADR-0005 | Proposed | adr-architect | — | `0005-resumable-event-log-and-strict-pydantic-schemas.md` | Drafted foundational ADR-0005 on branch adr/0005-resumable-event-log-and-strict-pydantic-schemas |
| 2026-11-24T08:42:09Z | `accepted` | ADR-0005 | Proposed -> Accepted | adr-architect | — | `0005-resumable-event-log-and-strict-pydantic-schemas.md` | Accepted Pydantic v2 extra=forbid schemas and append-only SQLite+JSONL event log ADR-0005 |
| 2026-11-24T08:42:10Z | `created` | ADR-0006 | Proposed | adr-architect | — | `0006-abstract-system-context-for-brownfield-readiness.md` | Drafted foundational ADR-0006 on branch adr/0006-abstract-system-context-for-brownfield-readiness |
| 2026-11-24T08:42:11Z | `accepted` | ADR-0006 | Proposed -> Accepted | adr-architect | — | `0006-abstract-system-context-for-brownfield-readiness.md` | Accepted GreenfieldEmptyContext / SystemContext seam ADR-0006 for Phase 2 brownfield readiness |
