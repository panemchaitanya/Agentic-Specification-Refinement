# 0020. Live Provider Decoding, Escalation Short-Circuit, Fallbacks and Live Probing

- **Date:** 2026-11-26
- **Status:** Deprecated
- **PR / Branch:** `adr/0020-live-provider-decoding-short-circuit-and-live-probes`
- **Supersedes:** None

## Context

Live end-to-end verification of ReqHQ v2 during Phase 4b against local Ollama (`qwen3:4b` and `phi4-mini` on `TIER_0`), live Google Gemini `v1beta` API (using a Windows DPAPI-encrypted `GEMINI_API_KEY`), and real frozen PURE dev documents (`0000 - cctns.pdf.txt`, `0000 - gamma j.pdf.txt`) plus cross-domain socio-technical cases surfaced five live-runtime gaps invisible to offline `httpx.MockTransport` unit tests:

1. **Ollama Reasoning-Model Token Starvation (`DEF-P4B-003`):** Ollama reasoning models (`qwen3:4b`/`8b`/`14b`/`32b`) default to thinking mode; under constrained JSON/GBNF decoding, internal thinking tokens exhausted `num_predict=512` (`57.61s`, `done_reason="length"`) and emitted empty `response=""`, triggering router retries and timeouts.
2. **Gemini `v1beta` Model Retirement, Schema Rejection & Transient 503s (`DEF-P4B-004`):** Legacy `gemini-2.0-flash` and `gemini-2.5-flash` returned HTTP 404 for new keys; `generationConfig.responseSchema` rejected Pydantic v2 `ConfigDict(extra="forbid")` JSON schemas (`$defs`, `$ref`, `additionalProperties`) with HTTP 400; Gemini 3.x thought parts (`{"thought": true}`) risked corrupting JSON parsing; and free-tier demand spikes produced transient HTTP 503 (`UNAVAILABLE`) responses.
3. **Live CLI Router Wiring & Redundant Local Inference on Explicit Escalations (`DEF-P4B-005`):** `_build_router()` in `src/reqhq/cli.py` previously omitted `cloud_providers`, `QuotaLedger`, and `PromptCache` even after `_run_live_startup_preflight()` decrypted vault keys, and `ProviderRouter.route()` executed local Ollama (~20–36s on `TIER_0`) before discarding the local output on explicit audited escalations (`FINAL_AUDIT`, `CRITIC_DISAGREEMENT`, `ALLOY_REPAIR_EXHAUSTED`).
4. **Stage Resilience & Offline PDF Fallback Over-Fragmentation (`DEF-P4B-002`, `DEF-P4B-006`):** `SocioTechnicalTriager.triage()` was the only elicitation/synthesis stage that did not catch `ProviderError` when falling back to heuristic triage during simultaneous local timeout and cloud 503 spikes, and offline PDF fallback decomposition over-fragmented real PURE SRS documents into 579–806 nodes (>60s PBT blowup).
5. **Live Credential Authentication Verification:** Offline syntax checks alone (ADR-0019) cannot detect revoked or mistyped cloud API keys prior to multi-stage live runs.

## Decision

We extend the local-first LLM routing (ADR-0003), Ollama runtime (ADR-0018), and encrypted credential vault (ADR-0019) architecture with five live-verified invariants:

1. **Ollama Constrained-Decoding Thinking Suppression (`src/reqhq/llm/providers/ollama.py`):**
   - `OllamaLocalProvider.build_payload()` sets `"think": False` by default whenever constrained decoding (`format` or `grammar`) is active, while honoring an explicit boolean `request.metadata["think"]` override. Verified HTTP 200 on both live `qwen3:4b` (`11.82s` valid strict JSON) and `phi4-mini`.
2. **Gemini `v1beta` `responseJsonSchema`, Active Default Model, Thought Filtering & 503 Retry (`src/reqhq/llm/providers/gemini.py`, `src/reqhq/config.py`, `.env.example`):**
   - `GeminiFreeProvider` defaults to `gemini-3.1-flash-lite` (configurable via `ReqHQConfig.gemini_model` / `REQHQ_GEMINI_MODEL`), emits `generationConfig.responseJsonSchema` to accept Pydantic v2 `ConfigDict(extra="forbid")` JSON schemas natively, filters `{"thought": true}` parts before JSON extraction, and performs one bounded automatic retry on transient HTTP 503 (`max_transient_retries=1`, `transient_retry_delay_seconds=1.5`).
3. **Live CLI Router Wiring & Explicit-Escalation Cloud-First Short-Circuit (`src/reqhq/cli.py`, `src/reqhq/llm/router.py`):**
   - `_build_router()` attaches placeholder-validated `GeminiFreeProvider` and `GroqFreeProvider` (strictly omitted under `--strict-local`), persistent `QuotaLedger` (90% ceiling), and `PromptCache` when `mock=False`.
   - When a caller passes an explicit audited `EscalationReason` and cloud routing is permitted (`not strict_local` and `cloud_providers` configured), `ProviderRouter.route()` short-circuits directly to `_escalate_to_cloud()` (`0` redundant local SLM calls, cutting live `FINAL_AUDIT` from `22.42s` to `1.43s`–`1.73s`) and gracefully falls back to local SLM only if all cloud providers fail or rate-limit.
4. **Universal Stage `ProviderError` Fallback & Bounded Offline PDF Decomposition (`src/reqhq/synthesis/triage.py`, `src/reqhq/elicitation/decomposer.py`):**
   - `SocioTechnicalTriager.triage()` catches `ProviderError` and falls back deterministically to `_heuristic_triage_item(..., cloud_verified=False)`, matching `RequirementDecomposer`, `Critics`, `AlloySpecEmitter`, and `PBTSpecEmitter`.
   - Offline PDF fallback decomposition reassembles wrapped lines, filters cover/TOC/footer noise, and caps fallback nodes (`MAX_FALLBACK_NODES_PER_CHUNK=6`, `MAX_FALLBACK_NODES_TOTAL=24`; structured LLM payloads remain uncapped).
5. **Opt-In Live Credential Authentication Probes (`--verify-live`, `src/reqhq/security/credential_vault.py`, `src/reqhq/cli.py`):**
   - `verify_credential_live()` performs lightweight, header-only non-destructive probes (`GET /v1beta/models` for `GEMINI_API_KEY`, `GET /openai/v1/models` for `GROQ_API_KEY`), exposed via opt-in `--verify-live` on `reqhq doctor`, `reqhq auth setup`, and `reqhq auth status`. Default execution, `--strict-local`, and offline CI remain zero-network.

## Consequences

- **Positive:** Eliminates real-world live JSON failures on Ollama `qwen3:*` and Gemini `v1beta` Pydantic schemas, verified end-to-end with zero defects across three live documents (`mini_srs`, `pure_dev_cctns`, `cross_domain_fin-01`) with live Ollama, live Gemini, and live Java 21 / Alloy 6.2.
- **Positive:** Explicit-escalation cloud-first short-circuit saves 20–35 seconds of redundant `TIER_0` local inference per `FINAL_AUDIT` or `CRITIC_DISAGREEMENT` call while preserving graceful local degradation and `--strict-local` zero-egress guarantees.
- **Positive:** Opt-in `--verify-live` catches revoked or mistyped API keys immediately via `reqhq doctor` / `reqhq auth` without slowing down offline CI or leaking secrets.
- **Trade-off:** Disabling thinking by default on constrained Ollama calls trades internal chain-of-thought depth for deterministic JSON compliance within tight `TIER_0` token budgets; callers requiring reasoning on a specific call may opt back in via `metadata={"think": True}`.
- **Obligation:** All five invariants must remain covered at 100% line coverage via offline `httpx.MockTransport` unit tests and re-verified live whenever provider adapters or router logic change.

## Deprecation Note

Consolidated into ADR-0023 (Hardware-Aware Local SLM, Free-Tier Router, Live Decoding & Escalation Short-Circuit (v2)) alongside superseded ADR-0003 to eliminate routing-policy clashes.
