# 0003. Hardware-Aware Local SLM and Free-Tier Router

- **Date:** 2026-11-24
- **Status:** Accepted
- **PR / Branch:** `adr/0003-hardware-aware-local-slm-and-free-tier-router`
- **Supersedes:** None

## Context

In ReqHQ v1 (`src/specpipe/`), calling a single cloud free-tier LLM for every pipeline stage hit the 500 RPD Gemini quota after 43 of 61 dev documents, halting runs for ~15 hours (`README.md`). At the same time, ReqHQ v2 must run reliably on developer laptops typical of student and individual engineering environments—specifically **Windows 11, 16 GB system RAM, Intel Core i5 CPU, and 4–8 GB VRAM** (as well as CPU-only or higher-VRAM workstations)—while preserving strict privacy for proprietary PRDs and preventing silent quota exhaustion or unstructured JSON failures.

## Decision

We implement a **local-first, hardware-aware LLM execution and gated free-tier routing architecture** under `src/reqhq/llm/` and `src/reqhq/store/prompt_cache.py`:

1. **Cross-Platform Hardware Profiling & Model Tiers (`T0`–`T3`):**
   - `HardwareProfiler.detect(tier_override=...)` probes GPU VRAM, RAM, and CPU cores on Windows 11 via a deterministic fallback chain (`nvidia-smi` -> DirectML/DXGI probe -> PowerShell CIM `Win32_VideoController` -> CPU fallback, plus stdlib `GlobalMemoryStatusEx` / Linux `/proc/meminfo` / macOS `sysctl`) and supports deterministic `--hardware-tier` override.
   - `MODEL_TIERS` maps detected VRAM to quantized local SLMs served via Ollama / `llama.cpp`:
     - **Tier 0 (`<= 4 GB` VRAM / CPU):** `qwen3:4b` (`Q4_K_M`, fallback `phi4-mini`)
     - **Tier 1 (`5–8 GB` VRAM):** `qwen3:8b` (`Q4_K_M`)
     - **Tier 2 (`12–16 GB` VRAM):** `qwen3:14b` (`Q4_K_M`)
     - **Tier 3 (`24+ GB` VRAM):** `gpt-oss:20b` (`Q4_K_M`)
2. **Constrained Decoding & Local-First Retries:**
   - `OllamaLocalProvider` sends JSON Schema / GBNF grammar constraints (`format` / `grammar` payload fields) to Ollama `/api/generate` so local SLMs produce structurally valid JSON.
   - `ProviderRouter` always executes locally first with up to `max_local_retries=2` schema validation retries before considering escalation.
3. **Explicitly Gated Cloud Free-Tier Escalation:**
   - Outbound calls to `GeminiFreeProvider` or `GroqFreeProvider` are permitted **only** when triggered by an explicit `EscalationReason` enum value: `CRITIC_DISAGREEMENT`, `SCHEMA_RETRY_EXHAUSTED`, `ALLOY_REPAIR_EXHAUSTED`, or `FINAL_AUDIT`.
4. **SQLite Quota Ledger (90% Ceiling) & HTTP 429 Failover:**
   - `QuotaLedger` tracks rolling RPM, RPD, TPM, and TPD usage per cloud provider in SQLite and blocks requests once usage would exceed **90% (`ceiling_ratio=0.90`)** of any limit, leaving a 10% safety margin.
   - On HTTP 429 (`RateLimitError`), `QuotaLedger` records a provider cooldown and `ProviderRouter` fails over between `gemini` and `groq`. If both free tiers are blocked, exhausted, or unconfigured, the router degrades gracefully to the local SLM output tagged `cloud_verified=False`.
5. **Reversible PII Scrubber & `--strict-local` Mode:**
   - Before any prompt leaves the machine for a cloud provider, `PIIScrubber` replaces emails, phone numbers, SSNs, API keys/Bearer tokens, IPv4 addresses, credit-card numbers, and credentialed URLs with deterministic placeholders (e.g., `<EMAIL_1>`, `<API_KEY_1>`) and recursively restores them in the returned response.
   - When `--strict-local` is enabled, `ProviderRouter` strictly forbids all outbound cloud calls and returns local results (`cloud_verified=False`).
6. **SHA-256 Prompt Cache:**
   - `PromptCache` (SQLite) keys responses by SHA-256 of `(prompt, model, schema_name, system_prompt)` to avoid redundant local compute and preserve cloud free-tier quota across repeated runs and session resumes.

## Consequences

- **Positive:** Eliminates routine cloud quota burn on Windows 11 / 16 GB RAM / 4–8 GB VRAM laptops while retaining stronger cloud reasoning for genuinely hard disagreements, schema repair failures, and final audits.
- **Positive:** Sensitive PRD identifiers never reach third-party free-tier APIs in raw form, and `--strict-local` provides an enforceable zero-egress guarantee for regulated or air-gapped environments.
- **Trade-off:** Local 4B–8B quantized SLMs have lower reasoning depth than frontier models on complex multi-constraint synthesis, which is explicitly surfaced via `cloud_verified=False` whenever cloud verification is skipped or unavailable.
- **Trade-off:** Regex-based PII scrubbing cannot catch arbitrary contextual secrets (e.g., unnamed proprietary codenames); users with strict confidentiality requirements should run with `--strict-local`.
