# 0027. End-to-End Smart Ingestion Wiring, Live LLM Runtime Hardening, and Per-Document Ablation Memoization

- **Date:** 2026-11-28
- **Status:** Accepted
- **PR / Branch:** `feat/v2-phase5-live-evaluation`
- **Supersedes:** None

## Context

Following acceptance of ADR-0022 through ADR-0026, end-to-end live evaluation runs (`eval_live_v3_full`, `eval_live_v4_full`, `eval_live_v4_ablate`) on `TIER_0` hardware exposed four architectural gaps between the standalone smart-ingestion modules, live LLM provider defaults, and the Proposal B3 / leave-one-critic-out ablation runner:

1. **Smart Ingestion Disconnect:** ADR-0026 delivered `PDFTextCleaner` and `SemanticSRSChunker` as standalone modules, but `BaseCritic.evaluate()`, `RequirementDecomposer.decompose_chunks()`, and `CorpusBenchmarkRunner` still operated on raw sequential chunk slicing.
2. **Layer 1 Reasoning-Token Exhaustion & Single-Line Codegen Collapse (`DEF-P5-005`):** Plain-text Layer 1 elicitation requests (`response_schema=None`) did not set `metadata['think'] = False`, causing Ollama `qwen3:4b` to exhaust its 512-token budget on internal thinking tokens and emit blank responses. In Layer 3, complex state-machine tasks occasionally collapsed generated Python onto a single line.
3. **Live Cloud Failover & `TIER_0` Timeout Failures:** Groq retired `llama-3.3-70b-versatile` (`HTTP 404 model_not_found`), breaking Gemini-429-to-Groq failover, while `OllamaLocalProvider`'s `60.0s` default HTTP timeout aborted valid `TIER_0` local generations.
4. **Redundant 7x Critic Re-Execution & Discarded Arbitration in `AblationRunner`:** Iterating arm-first and calling `CriticPanel.run_on_nodes()` seven times per document executed 30 critic passes instead of 5 and triggered dozens of `_arbitrate_disagreement()` LLM escalations whose disagreement output was discarded, pushing single-document wall clock to ~19 minutes.

## Decision

We adopt four targeted architectural upgrades extending ADR-0022, ADR-0023, ADR-0024, and ADR-0026:

1. **End-to-End Smart Ingestion Pipeline Wiring (Extending ADR-0026):**
   - `BaseCritic.evaluate()` (`src/reqhq/elicitation/critics.py`) groups nodes via `cluster_requirements_into_batches(nodes, batch_size=_BATCH_SIZE)` for semantic co-location.
   - `RequirementDecomposer.decompose_chunks()` (`src/reqhq/elicitation/decomposer.py`) applies trace-preserving `deduplicate_requirements()` after chunk extraction.
   - `CorpusBenchmarkRunner` (`src/reqhq/evaluation/corpus_bench.py`) and `reqhq eval corpus-bench` (`src/reqhq/cli.py`) enable `PDFTextCleaner` + `SemanticSRSChunker` by default (`use_smart_frontend=True`, toggleable via `--smart-frontend` / `--raw-frontend`) and persist `smart_frontend_enabled`, `cleaned_char_count`, `skipped_sections_count`, and `llm_routed_chars` in `CorpusDocRecord`.

2. **Layer 1 Thinking Suppression & Multi-Line Codegen Hardening (`DEF-P5-005`, Extending ADR-0022/0023):**
   - `EvalOrchestrator.run_layer1()` (`src/reqhq/evaluation/runner.py`) passes `metadata={"stage": "layer1_elicitation", "think": False}` on plain-text requests and escalates empty responses via `EscalationReason.SCHEMA_RETRY_EXHAUSTED` before deterministic fallback.
   - `LiveLLMCodingAgent` (`src/reqhq/harness/live_agent.py`) enforces multi-line newline-separated Python in `_CODEGEN_SYSTEM_PROMPT`.

3. **Active Groq Model Default & Configurable `TIER_0` Ollama Timeout (Extending ADR-0023):**
   - `GroqFreeProvider` (`src/reqhq/llm/providers/groq.py`) defaults to `openai/gpt-oss-20b`, configurable via `ReqHQConfig.groq_model` / `REQHQ_GROQ_MODEL` (`src/reqhq/config.py`) and wired in `cli._build_router()`.
   - `OllamaLocalProvider` (`src/reqhq/llm/providers/ollama.py`) defaults to `timeout=120.0`, configurable via float-validated `ReqHQConfig.ollama_timeout_seconds` / `REQHQ_OLLAMA_TIMEOUT` and wired in `cli._build_router()`.

4. **Per-Document 5-Critic Memoization in `AblationRunner` (Extending ADR-0024):**
   - `AblationRunner.run()` (`src/reqhq/evaluation/ablations.py`) iterates per document, evaluates only the unique specialist critics required by pending arms once via `BaseCritic.evaluate()` and `_critic_keys_for_arm()` (skipping unused `_arbitrate_disagreement()` escalations), attributes per-arm `wall_clock_sec` from the active critics' latencies plus oracle scoring, and atomically checkpoints all 7 arms per document.

## Consequences

- **Positive:** Live evaluation `eval_live_v4_full` flipped H1 from `INCONCLUSIVE` to `SUPPORTED` (`44.1%` vs `12.7%` recall, `p = 0.0040`) with 100% local Ollama inference (`0` cloud calls), while H2 (`99.12%` cloud request reduction) and H3 (`48/48` Alloy contradictions, `p = 1.19e-7`) remained `SUPPORTED`.
- **Positive:** Live Groq failover with `openai/gpt-oss-20b` succeeds in ~`1.2s` with strict JSON even during Gemini 500-RPD cooldowns, and the `120.0s` Ollama timeout prevents premature `TIER_0` aborts.
- **Positive:** Per-document critic memoization cuts `AblationRunner` LLM volume from 30 critic passes + ~105 discarded arbitration calls per document down to at most 5 critic passes (~7-10x speedup) with mathematically identical findings, recall, precision, and F1, and supports partial per-arm resume.
- **Obligation:** Maintain `100.00%` line coverage across all `src/reqhq/` modules, keep NTFS/SQLite Hypothesis PBTs configured with `deadline=None` (`AGENTS.md` Section 3), and re-verify live Layer 3 and B3 ablations after deployment.
