# 0018. Local Ollama Auto-Bootstrap and Readiness Gate

- **Date:** 2026-11-25
- **Status:** Accepted
- **PR / Branch:** `adr/0018-local-ollama-auto-bootstrap-and-readiness-gate`
- **Supersedes:** None

## Context

ReqHQ v2 adopts a local-first, hardware-aware SLM routing architecture (ADR-0003) targeting Windows 11 developer laptops (16 GB system RAM, 2–8 GB VRAM, `TIER_0`–`TIER_3`) backed by a local Ollama runtime (`http://127.0.0.1:11434`). In live interactive sessions and multi-stage evaluations, relying on users to manually start the Ollama daemon and verify model availability before invoking ReqHQ causes three recurring operational failures:

1. **Cold-Daemon Failure:** Ollama may be installed on `PATH` or at the default Windows per-user installation path (`%LOCALAPPDATA%\Programs\Ollama\ollama.exe`), yet `ollama serve` is not running when a user invokes `reqhq elicit`, `reqhq finalize`, `reqhq export`, `reqhq handoff`, or `reqhq eval run --mode live`, producing opaque HTTP connection errors mid-pipeline.
2. **Missing Tier-Aligned Model Weights:** Even when the daemon is reachable, the tier-assigned primary and fallback models defined in `src/reqhq/llm/model_registry.py` (e.g., `TIER_0`: `qwen3:4b` primary and `phi4-mini` fallback) may not yet be pulled on disk. Unassisted runtime calls fail late with HTTP 404 model-not-found errors rather than failing fast with exact setup instructions.
3. **VRAM Thrashing & Offline CI Contamination:** Eagerly warming up both primary and fallback models simultaneously on a 16 GB RAM / 2 GB VRAM laptop triggers severe memory swapping and VRAM thrashing. Conversely, running live daemon probes or auto-starting background processes during `--mock`, `reqhq adr`, `reqhq status`, or offline CI runs violates deterministic, zero-network test isolation.

## Decision

We implement a **Local Ollama Auto-Bootstrap and Readiness Gate** under `src/reqhq/llm/ollama_runtime.py` (re-exported via `src/reqhq/llm/__init__.py`), configured by `ReqHQConfig.auto_start_ollama` (`REQHQ_AUTO_START_OLLAMA`, default `True`) in `src/reqhq/config.py`, and exposed via `reqhq doctor` and live-command startup preflight in `src/reqhq/cli.py`:

1. **Daemon Detection & Detached Auto-Start (`OllamaRuntimeManager`):**
   - `OllamaRuntimeManager.is_daemon_running()` and `list_installed_models()` probe `GET /api/tags` against `ollama_base_url` with bounded timeouts and never raise unhandled connection errors.
   - `find_ollama_binary()` resolves the local executable via `shutil.which("ollama")` and falls back on Windows to `%LOCALAPPDATA%\Programs\Ollama\ollama.exe`.
   - When the daemon is down, `auto_start=True`, and the binary is present, `start_daemon()` spawns `ollama serve` as a detached background process using `CREATE_NO_WINDOW | DETACHED_PROCESS` on Windows (`start_new_session=True` on POSIX) and polls `GET /api/tags` until the daemon responds or `startup_timeout_seconds` expires.
2. **Tier-Aligned Model Inventory Check (`model_registry.py`, `normalize_model_tag`):**
   - `check_and_bootstrap()` resolves the active `HardwareTier` via `HardwareProfiler.detect(tier_override=...)` and `get_model_for_tier()` from `src/reqhq/llm/model_registry.py`, computing `required_models` as the tier's primary model plus its configured fallback model when `require_fallback_model=True` (e.g., `TIER_0`: `qwen3:4b` + `phi4-mini`).
   - `normalize_model_tag()` canonicalizes Ollama tag names case-insensitively so tagless identifiers such as `phi4-mini` match installed `phi4-mini:latest` entries while explicit quantized/sized tags such as `qwen3:4b` match exactly.
3. **Actionable Install/Pull Guidance & Opt-In `--auto-pull`:**
   - Default startup never silently downloads multi-gigabyte model weights. Whenever the binary, daemon, or required tier models are unavailable, `OllamaReadinessReport.guidance` (and `OllamaRuntimeError.guidance` when `raise_on_unready=True` in `ensure_ollama_ready()`) emits exact, copy-paste recovery steps (`winget install Ollama.Ollama`, `ollama serve`, and `ollama pull <model>` for every missing model).
   - Users may explicitly opt in to automated pulling via `reqhq doctor --auto-pull` (`auto_pull=True`), which invokes `ollama pull <model>` for each missing required model and re-verifies inventory via `GET /api/tags`.
4. **Single-Primary-Model Warm-Up (`keep_alive="10m"`):**
   - When `ready` is `True` and `warmup=True`, `warmup_model(spec.model_name, keep_alive="10m")` sends a single empty-prompt `POST /api/generate` (`{"model": model_name, "prompt": "", "stream": false, "keep_alive": "10m"}`) strictly for the active primary model only—never pre-loading the fallback model concurrently—protecting 16 GB RAM / 2 GB VRAM laptops from multi-model VRAM thrashing while eliminating first-token cold-start latency.
5. **Diagnostic CLI (`reqhq doctor`) & Live Preflight Bypass Rules (`src/reqhq/cli.py`):**
   - `reqhq doctor [--auto-start/--no-auto-start] [--auto-pull] [--warmup] [--strict-local]` reports `HardwareTier`, Ollama daemon and tier-model readiness (`OllamaReadinessReport`), masked credential vault status (ADR-0019), Java 21, and Alloy 6.2 JAR availability, exiting non-zero with actionable guidance when critical local components are unready.
   - Live LLM CLI commands (`elicit`, `finalize`, `export`, `handoff`, `eval run --mode live`) automatically invoke `ensure_ollama_ready(auto_start=cfg.auto_start_ollama, warmup=True, raise_on_unready=True)` at startup before executing pipeline stages.
   - Non-LLM and offline commands (`--mock`, `reqhq adr`, `reqhq status`, and offline CI test suites) strictly bypass Ollama probing, auto-start, and warm-up so zero subprocesses or network requests occur.

## Consequences

- **Positive:** Eliminates cold-daemon connection failures and late mid-run HTTP 404 model errors on Windows 11 developer machines, giving users immediate, copy-paste remediation or transparent detached startup.
- **Positive:** Single-primary-model warm-up (`keep_alive="10m"`) respects the `TIER_0` 16 GB RAM / 2 GB VRAM envelope without thrashing GPU memory, while reusing zero new dependencies (stdlib `subprocess`/`shutil`/`os` + installed `httpx` and `pydantic`).
- **Positive:** Strict bypass under `--mock`, `adr`, `status`, and offline CI preserves 100% deterministic, zero-network unit and system test execution.
- **Trade-off:** A detached `ollama serve` process started by ReqHQ continues running after the CLI command exits so subsequent commands avoid cold restarts; users who prefer manual daemon lifecycle control can set `REQHQ_AUTO_START_OLLAMA=false` or pass `--no-auto-start`.
- **Obligation:** All unit and deterministic Hypothesis property-based tests for `src/reqhq/llm/ollama_runtime.py` must run 100% offline via `httpx.MockTransport` and mocked `subprocess`, achieving 100% line coverage with zero real network calls and verifying `report.ready == (report.daemon_running and not report.missing_models)`.
