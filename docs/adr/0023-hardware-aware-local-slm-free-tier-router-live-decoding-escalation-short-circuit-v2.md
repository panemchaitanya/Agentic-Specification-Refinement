# 0023. Hardware-Aware Local SLM, Free-Tier Router, Live Decoding & Escalation Short-Circuit (v2)

- **Date:** 2026-11-27
- **Status:** Accepted
- **PR / Branch:** `fix/p5-live-eval-paths-telemetry-progress`
- **Supersedes:** ADR-0003

## Context

ADR-0003 (v1) established ReqHQ v2's hardware-tiered local SLM routing (`TIER_0`–`TIER_3`), constrained decoding, gated free-tier cloud escalation, SQLite `QuotaLedger` (90% ceiling), reversible `PIIScrubber`, `--strict-local`, and SHA-256 `PromptCache`, stating that `ProviderRouter` always executes locally first before considering escalation.

During Phase 4b live end-to-end verification against local Ollama (`qwen3:4b`, `phi4-mini`) and live Google Gemini `v1beta`, ADR-0020 introduced five live-runtime fixes (`DEF-P4B-002`–`DEF-P4B-006`)—including short-circuiting explicit audited `EscalationReason` calls directly to cloud without burning 20–36s on redundant local SLM inference—which directly amended ADR-0003 v1's local-first-always rule.

This v2 ADR supersedes ADR-0003 and consolidates ADR-0020 (deprecated and archived in `docs/adr/archive/`) into one single, conflict-free routing and live-provider architecture.

## Decision

We consolidate all hardware-aware local SLM routing, free-tier governance, live provider constrained decoding, universal stage fallbacks, and opt-in live credential probing across `src/reqhq/llm/`, `src/reqhq/store/prompt_cache.py`, `src/reqhq/synthesis/triage.py`, `src/reqhq/elicitation/decomposer.py`, `src/reqhq/security/credential_vault.py`, `src/reqhq/config.py`, and `src/reqhq/cli.py`:

1. **Cross-Platform Hardware Profiling & Model Tiers (`T0`–`T3`, `hardware_profiler.py`, `model_registry.py`):**
   - `HardwareProfiler.detect(tier_override=...)` probes VRAM, RAM, and CPU cores (`nvidia-smi` -> DirectML/DXGI -> `Win32_VideoController` -> CPU fallback), mapping to `TIER_0` (`<= 4 GB`: `qwen3:4b` `Q4_K_M`, fallback `phi4-mini`), `TIER_1` (`5–8 GB`: `qwen3:8b`), `TIER_2` (`12–16 GB`: `qwen3:14b`), and `TIER_3` (`24+ GB`: `gpt-oss:20b`).
2. **Local Constrained Decoding & Thinking Suppression (`DEF-P4B-003`, `src/reqhq/llm/providers/ollama.py`):**
   - `OllamaLocalProvider.build_payload()` sends JSON Schema / GBNF constraints (`format` / `grammar`) to `/api/generate` and sets `"think": False` by default whenever `format` or `grammar` is active (honoring explicit boolean `request.metadata["think"]` overrides), preventing reasoning models (`qwen3:*`) from exhausting `num_predict` on thinking tokens.
3. **Dual-Path Audited Routing: Local-First Default + Explicit-Escalation Cloud Short-Circuit (`DEF-P4B-005`, `router.py`):**
   - **Routine calls (`escalation_reason is None`):** `ProviderRouter` executes locally first with up to `max_local_retries=2` schema validation retries, escalating only on `SCHEMA_RETRY_EXHAUSTED` when cloud routing is permitted.
   - **Explicit audited escalations (`CRITIC_DISAGREEMENT`, `ALLOY_REPAIR_EXHAUSTED`, `FINAL_AUDIT`):** When `not strict_local` and cloud providers are configured, `ProviderRouter.route()` short-circuits directly to `_escalate_to_cloud()` (`0` redundant local SLM calls, cutting `TIER_0` `FINAL_AUDIT` latency from `22.42s` to ~`1.5s`) and falls back to local SLM only if all cloud providers fail or rate-limit.
4. **Live Gemini `v1beta` & Groq Free-Tier Governance (`DEF-P4B-004`, `gemini.py`, `groq.py`, `quota_ledger.py`, `pii_scrubber.py`, `prompt_cache.py`, `cli.py`):**
   - `GeminiFreeProvider` defaults to `gemini-3.1-flash-lite` (overridable via `ReqHQConfig.gemini_model` / `REQHQ_GEMINI_MODEL`), emits `generationConfig.responseJsonSchema` (supporting Pydantic v2 `ConfigDict(extra="forbid")` `$defs`/`$ref`/`additionalProperties`), filters `{"thought": true}` parts before JSON extraction, and retries transient HTTP 503 once (`max_transient_retries=1`, `transient_retry_delay_seconds=1.5`).
   - `QuotaLedger` enforces a 90% ceiling (`ceiling_ratio=0.90`) on RPM/RPD/TPM/TPD with HTTP 429 cooldown failover between `gemini` and `groq`; `PIIScrubber` reversibly redacts sensitive entities before egress; `--strict-local` forbids all outbound calls; `PromptCache` deduplicates by SHA-256; and `_build_router()` wires decrypted cloud providers, `QuotaLedger`, and `PromptCache` when `mock=False`.
5. **Universal Stage `ProviderError` Fallback, Bounded PDF Fallback & Opt-In Live Auth Probes (`DEF-P4B-002`, `DEF-P4B-006`):**
   - Every elicitation/synthesis stage—including `SocioTechnicalTriager.triage()`—catches `ProviderError` and degrades deterministically with `cloud_verified=False`; offline PDF fallback decomposition caps nodes at `MAX_FALLBACK_NODES_PER_CHUNK=6` and `MAX_FALLBACK_NODES_TOTAL=24`.
   - `verify_credential_live()` provides opt-in header-only auth probes (`--verify-live` on `reqhq doctor`, `reqhq auth setup`, `reqhq auth status`) while default commands, `--strict-local`, and offline CI remain strictly zero-network.

## Consequences

- **Positive:** Eliminates the routing-order contradiction between ADR-0003 v1 (always local first) and ADR-0020 (explicit-escalation cloud short-circuit), establishing one authoritative v2 routing & live-provider specification.
- **Positive:** Preserves `TIER_0` local-first privacy, 90% quota protection, and zero-network offline CI while delivering reliable live JSON decoding on Ollama `qwen3:*` and Gemini 3.x.
- **Trade-off:** Disabling thinking by default on constrained Ollama calls trades internal chain-of-thought depth for deterministic JSON compliance within tight `TIER_0` token budgets (opt-in via `metadata={"think": True}`).
- **Obligation:** Maintain 100.00% line coverage via offline `httpx.MockTransport` unit tests and re-verify live against Ollama + Gemini/Groq whenever provider adapters or router logic change.
