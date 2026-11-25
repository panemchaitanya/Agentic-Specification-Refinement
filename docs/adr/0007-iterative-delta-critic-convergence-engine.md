# 0007. Iterative Delta-Critic Convergence Engine

- **Date:** 2026-11-24
- **Status:** Accepted
- **PR / Branch:** `adr/0007-iterative-delta-critic-convergence-engine`
- **Supersedes:** None

## Context

Single-pass requirement extraction fails across both ends of the user input spectrum: 1-liner product prompts omit critical failure modes, non-functional budgets, and operational dependencies, while multi-page enterprise PRDs contain hidden contradictions and ambiguities across sections. Conversely, dumping dozens of unranked questions on a user causes rapid fatigue, and naively re-running a full multi-agent critique panel over the entire specification graph after every answer is computationally prohibitive on laptop hardware.

ReqHQ v2 Phase 2 must execute reliably within the baseline **`TIER_0` hardware envelope** verified on developer workstations—**Windows 11, 16 GB system RAM, AMD Radeon 660M (2 GB VRAM)** running quantized local SLMs (`qwen3:4b` `Q4_K_M` with `phi4-mini` fallback, per ADR-0003)—without exhausting RAM/VRAM or burning cloud free-tier quotas. In addition, any auto-generated assumptions recorded when a user finalizes early must remain strictly separated from ReqHQ's repository governance Nygard ADRs (`docs/adr/0001`–`0010`).

## Decision

We implement a **sequential, low-memory Iterative Delta-Critic Convergence Engine** under `src/reqhq/elicitation/` (`models.py`, `decomposer.py`, `critics.py`, `delta.py`, `engine.py`) with strict Pydantic v2 schemas (`ConfigDict(extra="forbid")`, ADR-0005):

1. **Unified Prompt & Chunked-PRD Decomposition (`RequirementDecomposer`):**
   - `decompose_prompt()` ingests 1-liner CLI prompts (`reqhq init --prompt "..."`) with deterministic `sha256(prompt)` digests and verbatim span `TraceLink` provenance.
   - `decompose_chunks()` processes `ChunkingResult` sequentially per chunk, prepending compact `GlobalGlossary.to_prompt_header()` context (ADR-0004) so every extracted `RequirementNode` preserves `(source_sha256, anchor, chunk_id)` traceability without overflowing small local SLM context windows.
2. **Five Sequential Local Specialist Critics (`CriticPanel`):**
   - Five focused critics run **sequentially** against a single loaded local SLM (`TIER_0`-safe, compact structured prompts `< 6,000` chars) covering five `CriticDomain` dimensions:
     - `FunctionalCompletenessCritic`
     - `NonFunctionalQualityCritic`
     - `LogicalConsistencyCritic`
     - `ProcessOperationalCritic`
     - `TrainingAdoptionCritic`
3. **Severity Tiering & Batched High-Information-Gain Questioning:**
   - Every `CriticFinding` is classified by `FindingSeverity` (`BLOCKER`, `MAJOR`, `MINOR`).
   - Findings are deduplicated, prioritized by severity and `info_gain_score`, and grouped into **3–5 batched `ElicitationQuestion` prompts per interactive turn** (`reqhq elicit`), persisting each turn via `SessionEventLog` (`EventKind.TURN_COMPLETED`).
4. **Delta-Only Subgraph Re-Analysis (`DeltaDependencyTracker`):**
   - Tracks dependency edges across `req_id <-> finding_id`, `question_id -> req_ids`, and `turn_id -> req_ids`.
   - When the user submits answers or rewinds history via `reqhq revise <session_id> <turn_id>` (ADR-0005), only the directly modified `REQ-ID`s and their dependent subgraph (`stale_req_ids`) are re-critiqued instead of re-evaluating the entire `RequirementGraph`.
5. **Convergence Gate & `finalize --accept-defaults` Assumption ADRs:**
   - Convergence is achieved once zero unresolved `BLOCKER` or `MAJOR` findings remain (or `--max-turns` completes).
   - Running `reqhq finalize <session_id> --accept-defaults` converts remaining unresolved findings into explicit `AssumptionRecord` objects emitted into the exported target-system bundle as `<export_dir>/ADRs/ADR-AXXX.md` with YAML frontmatter `type: assumption` and `user_confirmed: false`. These target-system Assumption ADRs are never written into `docs/adr/`.
6. **Audited Critic-Disagreement Escalation:**
   - When multiple specialist critics register conflicting findings on the same `REQ-ID`, `CriticPanel` emits a `CriticDisagreement`, escalates through `ProviderRouter.route(..., escalation_reason=EscalationReason.CRITIC_DISAGREEMENT)` (ADR-0003), and records `EventKind.ESCALATION_TRIGGERED` in the session event log.

## Consequences

- **Positive:** Runs comfortably inside `TIER_0` (16 GB RAM / 2 GB VRAM) without parallel model thrashing, while delta-only subgraph re-analysis keeps interactive elicitation turns fast and predictable.
- **Positive:** Severity tiering and 3–5 question batching prevent user fatigue, and `user_confirmed: false` Assumption ADRs make unconfirmed defaults explicit and auditable during downstream verification and review.
- **Positive:** Preserves strict namespace separation between repo Nygard ADRs (`docs/adr/`) and generated target-system Assumption ADRs (`<export_dir>/ADRs/ADR-AXXX.md`).
- **Trade-off:** Sequential execution of five critics increases wall-clock time on the initial full-graph pass compared to parallel cloud calls; this is offset by compact `< 6K`-char prompts, `PromptCache` hits (ADR-0003), and delta-only re-critiquing on subsequent turns.
- **Obligation:** Downstream synthesis and verification stages must surface `user_confirmed: false` assumptions prominently in `traceability_matrix.json` and exported bundles until explicitly confirmed by a human stakeholder.
