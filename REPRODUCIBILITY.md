# ReqHQ v2 Reproducibility Guide

**Project:** Agentic Specification Refinement (`reqhq` v2)  
**Team:** `req-hq` — IIIT Hyderabad  
**Authors:** Panem Chaitanya Pavan Kumar (`2025204023`), Anjali Yadav (`2025204015`), Anshul Agrawal (`2025901015`)

This document provides exact, copy-paste commands to reproduce:
1. Offline CI dry-run verification (plumbing + Mock Quarantine),
2. Live Windows 11 `TIER_0` Ollama + Gemini/Groq free-tier + Alloy 6.2 empirical evaluation (including crash/cooldown resumption), and
3. Cryptographic SHA-256 release bundle & frozen dataset verification.

---

## 1. Environment & Toolchain Prerequisites

- **Operating System:** Windows 11 (x64)
- **Python Runtime:** Python `3.15.0` inside `.venv/Scripts/python.exe` (zero compiled third-party wheel dependencies for statistics, SVG rendering, or memory telemetry — pure Python stdlib `math`, `random`, `statistics`, `xml.etree.ElementTree`, `ctypes`, `tracemalloc`).
- **Java & Alloy 6.2:** Eclipse Temurin JRE 21 on `PATH` and Alloy 6.2 distribution JAR:
  - `REQHQ_ALLOY_JAR_PATH="C:/tools/alloy/org.alloytools.alloy.dist.jar"`
- **Local SLM Runtime (Live Runs):** Ollama running at `http://127.0.0.1:11434` with pinned `TIER_0` models:
  - Primary `TIER_0`: `qwen3:4b` (`Q4_K_M`)
  - Fallback `TIER_0`: `phi4-mini`
  - Higher hardware tiers (`src/reqhq/llm/model_registry.py`): `TIER_1` (`qwen3:8b`), `TIER_2` (`qwen3:14b`), `TIER_3` (`gpt-oss:20b`).
- **Optional Free-Tier Cloud Keys (Live Escalation):**
  - `GEMINI_API_KEY` and/or `GROQ_API_KEY` (subject to SQLite `QuotaLedger` 90% ceiling and deterministic PII scrubbing).

### Verify Local Environment (PowerShell)

```powershell
.venv/Scripts/python.exe --version
java -version
$env:REQHQ_ALLOY_JAR_PATH = "C:/tools/alloy/org.alloytools.alloy.dist.jar"
Test-Path $env:REQHQ_ALLOY_JAR_PATH
.venv/Scripts/python.exe -m reqhq.cli status
```

---

## 2. Offline CI Dry-Run Pipeline (No Network / No API Keys)

Offline dry-runs execute the full Layers 1–4 orchestrator, pure-Python statistical pipeline, SVG chart renderer, Markdown/LaTeX report generator, and SHA-256 release packager deterministically using `--mode mock`.

> [!WARNING]
> Offline `--mode mock` runs are strictly **quarantined fail-closed** as `DRY_RUN_UNSCORED`. Every SVG carries a `DRY-RUN MOCK DATA` watermark, `FINAL_REPORT.md` and `tables_latex.tex` carry prominent warning banners, and all H1–H4 verdicts are forced to `QUARANTINED_MOCK_OR_INCOMPLETE`. Never cite dry-run outputs as empirical evidence.

```powershell
$env:REQHQ_ALLOY_JAR_PATH = "C:/tools/alloy/org.alloytools.alloy.dist.jar"

# 1. Run Layers 1-4 in deterministic mock mode
.venv/Scripts/python.exe -m reqhq.cli eval run `
  --layers 1,2,3,4 `
  --out artifacts/ci_dry_run `
  --mode mock `
  --replicates 2 `
  --seed 42

# 2. Verify idempotent checkpoint resume (zero duplicate JSONL records)
.venv/Scripts/python.exe -m reqhq.cli eval resume --out artifacts/ci_dry_run

# 3. Analyze metrics, paired bootstrap CIs, exact tests, Pareto SVGs & 7-category taxonomy
.venv/Scripts/python.exe -m reqhq.cli eval analyze `
  --in artifacts/ci_dry_run `
  --out artifacts/ci_dry_run/analysis

# 4. Generate quarantined FINAL_REPORT.md + Overleaf booktabs tables_latex.tex
.venv/Scripts/python.exe -m reqhq.cli eval report `
  --in artifacts/ci_dry_run/analysis `
  --out artifacts/ci_dry_run/report `
  --format both

# 5. Package tamper-evident release bundle and verify SHA-256 integrity
.venv/Scripts/python.exe -m reqhq.cli eval package `
  --in artifacts/ci_dry_run `
  --out artifacts/ci_dry_run_bundle

.venv/Scripts/python.exe -m reqhq.cli eval verify-bundle `
  artifacts/ci_dry_run_bundle/artifacts_manifest.json
```

Passing `--strict-live` to `reqhq eval analyze` on a mock run is guaranteed to exit non-zero via `MockRunQuarantineError`.

---

## 3. Live Windows 11 Empirical Run (`TIER_0` Ollama + Gemini/Groq + Alloy 6.2)

Live empirical runs enforce `run_mode="live"`, `replicates >= 2`, human-verified frozen gold (`gold_verified=True`), and live Alloy 6.2 verification without silent fallback to mock executors.

### 3.1 Start Ollama & Pull Pinned `TIER_0` Models

```powershell
ollama pull qwen3:4b
ollama pull phi4-mini
```

### 3.2 Configure Environment & Launch Live Evaluation

```powershell
$env:REQHQ_ALLOY_JAR_PATH = "C:/tools/alloy/org.alloytools.alloy.dist.jar"
$env:OLLAMA_BASE_URL = "http://127.0.0.1:11434"
# Optional free-tier cloud escalation keys (omit or pass --strict-local for 100% local runs):
# $env:GEMINI_API_KEY = "<your-gemini-key>"
# $env:GROQ_API_KEY = "<your-groq-key>"

.venv/Scripts/python.exe -m reqhq.cli eval run `
  --layers 1,2,3,4 `
  --out artifacts/live_run_v2 `
  --mode live `
  --replicates 3 `
  --seed 42
```

### 3.3 Resuming Across Free-Tier RPD Cooldowns or Reboots

Every completed `(layer, case_id, condition, replicate)` unit is committed atomically to `artifacts/live_run_v2/checkpoint.json` and append-only `layer1.jsonl`–`layer4_telemetry.jsonl` logs. To resume after a free-tier quota cooldown or interruption without duplicating records:

```powershell
$env:REQHQ_ALLOY_JAR_PATH = "C:/tools/alloy/org.alloytools.alloy.dist.jar"
.venv/Scripts/python.exe -m reqhq.cli eval resume --out artifacts/live_run_v2
```

### 3.4 Strict-Live Analysis, Report Generation & Release Packaging

```powershell
# Fail-closed live verification (exits non-zero if any layer, gold check, or log hash fails)
.venv/Scripts/python.exe -m reqhq.cli eval analyze `
  --in artifacts/live_run_v2 `
  --out artifacts/live_run_v2/analysis `
  --strict-live

# Emit VERIFIED_LIVE FINAL_REPORT.md + Overleaf tables_latex.tex + clean SVG figures
.venv/Scripts/python.exe -m reqhq.cli eval report `
  --in artifacts/live_run_v2/analysis `
  --out artifacts/live_run_v2/report `
  --format both

# Build tamper-evident release bundle with pinned models & frozen dataset hashes
.venv/Scripts/python.exe -m reqhq.cli eval package `
  --in artifacts/live_run_v2 `
  --out artifacts/live_release_bundle

# Cryptographically verify every file in the release bundle
.venv/Scripts/python.exe -m reqhq.cli eval verify-bundle `
  artifacts/live_release_bundle/artifacts_manifest.json
```

---

## 4. SHA-256 Bundle & Frozen Dataset Integrity Verification

`reqhq eval verify-bundle <manifest_path>` verifies:
1. Strict Pydantic v2 schema validity of `artifacts_manifest.json` (`ConfigDict(extra="forbid")`),
2. Path safety (rejects `..`, absolute paths, Windows drive prefixes, duplicate entries, and symlink escapes outside the bundle root),
3. Presence of every tracked file (`eval_manifest.json`, `layer*.jsonl`, `analysis_summary.json`, `*.svg`, `FINAL_REPORT.md`, `tables_latex.tex`), and
4. Exact byte length and 64-character SHA-256 digest match for every tracked file.

Every packaged `artifacts_manifest.json` also records read-only SHA-256 digests of the immutable frozen benchmark manifests:
- `data/frozen/pure/FREEZE.json`
- `data/frozen/promise_nfr/FREEZE.json`

To inspect frozen dataset hashes directly in PowerShell:

```powershell
Get-FileHash -Algorithm SHA256 data/frozen/pure/FREEZE.json, data/frozen/promise_nfr/FREEZE.json
```

---

## 5. Automated Test & Verification Suite (4-Gate Protocol)

Run all verification gates strictly with `.venv/Scripts/python.exe`:

```powershell
$env:REQHQ_ALLOY_JAR_PATH = "C:/tools/alloy/org.alloytools.alloy.dist.jar"

# Gate 1: Legacy specpipe regression suite (must remain 87 passed, zero legacy edits)
.venv/Scripts/python.exe -m pytest --ignore=tests/reqhq

# Gate 2: Full ReqHQ v2 unit + deterministic Hypothesis PBT suite with coverage & strict warnings
.venv/Scripts/python.exe -m pytest tests/reqhq `
  --cov=src/reqhq `
  --cov-report=term-missing `
  -W error::hypothesis.errors.HypothesisWarning

# Gate 3: Live headless Alloy 6.2 verification gate
.venv/Scripts/python.exe -m pytest tests/reqhq -m alloy_live -vv

# Gate 4: Combined full repository test suite
.venv/Scripts/python.exe -m pytest
```
