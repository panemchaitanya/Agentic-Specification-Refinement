# 0015. Fail-Closed Empirical Runner and Mock Quarantine

- **Date:** 2026-11-25
- **Status:** Accepted
- **PR / Branch:** `adr/0015-fail-closed-empirical-runner-and-mock-quarantine`
- **Supersedes:** None

## Context

Phase 4 evaluates ReqHQ v2's four core empirical hypotheses (`H1`–`H4`) across four experimental layers: Layer 1 (elicitation gap recall & efficiency), Layer 2 (socio-technical routing & Alloy contradiction detection), Layer 3 (Condition A/B/C downstream coding trials with mutation and PBT oracles), and Layer 4 (Windows 11 `TIER_0` hardware & quota telemetry). Running full multi-replicate live trials against local SLMs (`qwen3:4b` / `phi4-mini`) and free-tier cloud escalation (`Gemini`, `Groq`) introduces three critical operational and scientific risks:

1. **Free-Tier RPD Pauses & Long Multi-Replicate Runs:** Multi-replicate live experiments may hit daily free-tier ceilings (ADR-0003) or laptop restarts mid-run. Without durable per-item checkpointing and prompt-hash caching, partial runs would lose completed trials and waste scarce cloud quota.
2. **Windows 11 Python 3.15 Telemetry Fragility:** Installing compiled third-party telemetry libraries (`psutil`) risks Windows wheel build failures on Python 3.15.0 (ADR-0004).
3. **Mock-vs-Live Contamination:** Offline CI pipelines MUST exercise the entire orchestration code path via `run_mode="mock"`, yet mock dry-run numbers must **never** be mistaken for or published as empirical `H1`–`H4` scientific verdicts.

## Decision

We implement a **Resumable, Checkpointed, Fail-Closed Layers 1–4 Evaluation Orchestrator** under `src/reqhq/evaluation/` (`models.py`, `telemetry.py`, `tasks.py`, `runner.py`) with 6 curated greenfield coding tasks under `data/benchmarks/coding_tasks/`, exposed via `reqhq eval run` and `reqhq eval resume` in `src/reqhq/cli.py`:

1. **Resumable Checkpointing & Prompt-Hash Caching (`runner.py`):**
   - Persists append-only JSONL layer logs (`layer1.jsonl`, `layer2.jsonl`, `layer3.jsonl`, `layer4_telemetry.jsonl`) alongside `eval_manifest.json` recording per-file SHA-256 digests, `run_mode` (`RunMode.MOCK` vs. `RunMode.LIVE`), `replicates`, `seed`, and completed record keys.
   - `reqhq eval resume --out <dir>` reloads `eval_manifest.json` and existing JSONL logs, skips already-completed `(layer, record_id)` entries, and reuses Phase 1 `PromptCache` (ADR-0003) so interrupted runs survive free-tier Gemini/Groq RPD cooldowns cleanly without re-burning quota.
2. **Six Curated Greenfield Coding Tasks (`data/benchmarks/coding_tasks/`, `tasks.py`):**
   - Provides 6 self-contained greenfield benchmark tasks across domains (`CodingTaskMeta` with `ConfigDict(extra="forbid")`), each supplying its target module contract, hidden verification tests, and expected socio-technical routes for repeatable Layer 3 Condition A/B/C trials (`ExperimentHarness`, `ASTMutantEngine`, `verify_contract_lock`, ADR-0012).
3. **Zero-Compiled-Dependency Windows 11 Stdlib Telemetry (`telemetry.py`):**
   - Measures process memory and execution latency strictly with Python stdlib (`ctypes` `GetProcessMemoryInfo` on Windows + `tracemalloc`) combined with Phase 1 `HardwareProfiler` (`TIER_0`–`TIER_3`) and `QuotaLedger`, recording `Layer4TelemetryRecord` (`local_only_pct`, `cloud_reduction_pct`, `peak_ram_mb`, `wall_clock_sec`) with zero `psutil` dependency.
4. **Strict Fail-Closed Mock Quarantine (`RunMode.MOCK` vs. `RunMode.LIVE`):**
   - Every record (`Layer1Record`, `Layer2Record`, `Layer3Record`, `Layer4TelemetryRecord`) and `eval_manifest.json` explicitly tags `run_mode` (`"mock"` or `"live"`) and `gold_verified`.
   - Downstream statistical evaluation and report generation (ADRs 0016–0017) inspect `eval_manifest.json` and SHA-256 log hashes before scoring `H1`–`H4`; any manifest with `run_mode != "live"`, `replicates < 2`, `gold_verified == False`, incomplete layers, or SHA-256 mismatch is **quarantined** (`DRY_RUN_UNSCORED` / `QUARANTINED_MOCK_OR_INCOMPLETE`) and fails closed when `--strict-live` is passed.

## Consequences

- **Positive:** Guarantees scientific integrity by construction—offline CI dry-runs test 100% of the evaluation pipeline while making accidental publication of mock data as live hypothesis verdicts structurally impossible.
- **Positive:** Checkpointed JSONL logs plus `PromptCache` allow multi-replicate Windows 11 `TIER_0` runs to pause across free-tier RPD windows and resume deterministically without losing work.
- **Positive:** Pure-stdlib telemetry preserves Python 3.15.0 zero-compilation portability while keeping `data/frozen/` and `src/specpipe/` strictly read-only.
- **Trade-off:** Stdlib `ctypes` / `tracemalloc` telemetry captures process RSS and Python heap peaks rather than per-thread GPU power draw; for `TIER_0` memory-envelope validation (16 GB RAM, 2 GB VRAM), peak RAM and `HardwareProfiler` VRAM metadata provide all required signals.
- **Obligation:** All temporary subprocess directories created during Layer 3 mutation/test trials on Windows MUST pass `ignore_cleanup_errors=True`, and all written `.json`/`.jsonl` logs MUST use strict LF (`\n`) line endings.
