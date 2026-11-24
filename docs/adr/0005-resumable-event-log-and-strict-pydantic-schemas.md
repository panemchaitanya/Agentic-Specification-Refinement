# 0005. Resumable Event Log and Strict Pydantic Schemas

- **Date:** 2026-11-24
- **Status:** Accepted
- **PR / Branch:** `adr/0005-resumable-event-log-and-strict-pydantic-schemas`
- **Supersedes:** None

## Context

In ReqHQ v1, Streamlit sessions lived solely in process memory; container restarts, idle timeouts, or quota interruptions destroyed in-progress analyses (`README.md`). Furthermore, loose dictionary payloads between pipeline stages allow silent field typos, schema drift from LLM outputs, and un-auditable mutations when a user revises an earlier requirement or design answer.

ReqHQ v2 must run reliably on **Python 3.11+** (actively verified on **Python 3.15.0**), support crash-safe CLI resumption (`reqhq resume <session_id>`), and allow users to revise any past turn (`reqhq revise <session_id> <turn_id>`) while accurately invalidating dependent downstream outputs without destroying audit history.

## Decision

We standardize ReqHQ v2 on **strict Pydantic v2 contracts** and a **dual append-only SQLite + JSONL session event log**:

1. **Python 3.11+ & Strict Pydantic v2 (`ConfigDict(extra="forbid")`):**
   - `pyproject.toml` targets `requires-python = ">=3.11"` and is continuously verified on Python 3.15.0.
   - Every core v2 schema—`ReqHQConfig`, `RequirementGraph` / `RequirementNode` / `RequirementEdge` / `TraceLink`, `RoutingADR` / `TraceabilityChain` (`source_sha256 -> req_id -> adr_id -> alloy_model_ref -> pbt_spec_id`), `PBTSpec` / `PropertyInvariant` / `PydanticGuardrail` (targeting Python `Hypothesis` + `Pydantic` guardrails), `ADRRecord`, `AuditEntry`, `SnapshotRecord`, `NormalizedDocument`, `ChunkingResult`, `SessionEvent`, `HardwareProfile`, `ModelSpec`, and `LLMRequest` / `LLMResponse`—declares `model_config = ConfigDict(extra="forbid")` to fail closed on unexpected or misspelled keys.
   - `RequirementGraph` provides deterministic reference-integrity, DAG/cycle, and traceability validation helpers.
2. **Append-Only Dual Storage (`SessionEventLog` in `src/reqhq/store/event_log.py`):**
   - Each session stores events in both `<sessions_dir>/<session_id>/events.db` (SQLite for indexed queries and transactional sequence allocation) and `<sessions_dir>/<session_id>/events.jsonl` (human-inspectable, line-delimited audit artifact).
   - `EventKind` enumerates all session transitions: `SESSION_INIT`, `SOURCE_INGESTED`, `TURN_COMPLETED`, `TURN_REVISED`, `TURN_INVALIDATED`, `ESCALATION_TRIGGERED`, `VERIFICATION_RECORDED`, and `EXPORT_COMPLETED`.
   - `verify_jsonl_parity(session_id)` validates 1-to-1 event parity between SQLite and JSONL during `reqhq verify`.
3. **Deterministic Resume & Non-Destructive Turn Revision:**
   - `resume(session_id)` replays the append-only log to reconstruct active state (`active_events`, `active_turns`, `invalidated_turns`, `sources`, `metadata`) cleanly after any interruption.
   - `revise(session_id, turn_id, revised_payload)` never updates or deletes prior SQLite rows or JSONL lines. Instead, it appends a `TURN_REVISED` event for `turn_id` followed by `TURN_INVALIDATED` events for all active downstream turns created after `turn_id`, preserving complete before/after auditability while marking exactly which downstream artifacts must be regenerated.

## Consequences

- **Positive:** Zero silent schema drift—LLM outputs and cross-module boundaries fail fast on unknown fields or malformed types, and Hypothesis property tests can verify schema round-trip invariants deterministically.
- **Positive:** Sessions survive crashes, reboots, and cloud cooldown pauses seamlessly via `reqhq resume`, while `reqhq revise` gives users safe time-travel editing without corrupting historical provenance.
- **Trade-off:** Maintaining both `events.db` and `events.jsonl` doubles event-write I/O per turn; because CLI sessions emit tens to hundreds of events (not high-frequency telemetry), latency overhead is negligible (< 2 ms per append) while auditability gains are substantial.
- **Obligation:** Any future schema evolution (e.g., adding optional fields in Phase 2) must preserve backward-compatible defaults or explicit event migration logic when loading older `events.jsonl` files.
