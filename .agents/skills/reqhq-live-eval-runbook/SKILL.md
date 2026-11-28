---
name: reqhq-live-eval-runbook
description: >-
  Verified copy-paste CLI runbooks for ReqHQ v2 live evaluations (Layers 1-4,
  Proposal B3 critic ablations, PURE corpus benchmark, report packaging),
  credential rotation, and full pytest + live Alloy 6.2 + 100% coverage gates.
  Use whenever preparing, running, resuming, or diagnosing live ReqHQ v2 evals
  or regression suites.
---

# ReqHQ v2 Live Evaluation & Verification Runbook

## Shared Environment Exports (Git Bash)

Always export both variables before any live evaluation, ablation, or test run so fresh `--sessions-dir` runs share one encrypted DPAPI vault without re-prompting for API keys and Alloy 6.2 runs live:

```bash
export REQHQ_VAULT_PATH=".reqhq_sessions/credentials.vault.enc"
export REQHQ_ALLOY_JAR_PATH="C:/tools/alloy/org.alloytools.alloy.dist.jar"
```

## 1. Full Live Evaluation (Layers 1–4, Extended Dev)

Run standalone (never overlap with `eval ablate` or full `pytest` on the same laptop):

```bash
.venv/Scripts/python.exe -m reqhq.cli eval run \
  --mode live \
  --layers 1,2,3,4 \
  --split dev \
  --include-promise \
  --cross-domain-suite extended \
  --coding-suite extended \
  --replicates 1 \
  --seed 42 \
  --sessions-dir .reqhq_sessions_v5 \
  --out eval_live_v5_full
```

## 2. Analyze, Report & Package Release Bundle

```bash
.venv/Scripts/python.exe -m reqhq.cli eval analyze --in eval_live_v5_full --out eval_live_v5_full/analysis --strict-live
.venv/Scripts/python.exe -m reqhq.cli eval report --in eval_live_v5_full/analysis --out eval_live_v5_full/report --format both
.venv/Scripts/python.exe -m reqhq.cli eval package --eval-dir eval_live_v5_full --analysis-dir eval_live_v5_full/analysis --report-dir eval_live_v5_full/report --out release_bundle_live_v5_full
```

## 3. Proposal B3 & Leave-One-Critic-Out Ablations (Run After `eval run`)

```bash
.venv/Scripts/python.exe -m reqhq.cli eval ablate \
  --out eval_live_v5_ablate \
  --mode live \
  --split dev \
  --include-promise \
  --sessions-dir .reqhq_sessions_v5
```

## 4. PURE 61-Doc Corpus Scalability Benchmark

```bash
.venv/Scripts/python.exe -m reqhq.cli eval corpus-bench \
  --mode live \
  --split dev \
  --smart-frontend \
  --sessions-dir .reqhq_sessions_v5 \
  --out eval_live_corpus_pure61
```

## 5. Credential Rotation & Live Auth Verification

```bash
.venv/Scripts/python.exe -m reqhq.cli auth rotate GROQ_API_KEY
.venv/Scripts/python.exe -m reqhq.cli auth status --verify-live
```

## 6. Full Regression + 100% Coverage + Live Alloy 6.2 Gate (PowerShell)

```powershell
$jdk = Get-ChildItem "C:\Program Files\Eclipse Adoptium" -Directory | Select-Object -First 1 -ExpandProperty FullName
if ($jdk) { $env:PATH = "$jdk\bin;$env:PATH" }
$env:REQHQ_ALLOY_JAR_PATH = "C:/tools/alloy/org.alloytools.alloy.dist.jar"
.venv/Scripts/python.exe -m pytest tests/test_app.py tests/test_llm_retry.py tests/test_match_rubric.py tests/test_merge_verdicts.py tests/test_metrics.py tests/test_pipeline.py -q
if ($LASTEXITCODE -eq 0) {
  .venv/Scripts/python.exe -m pytest tests/reqhq --ignore=tests/reqhq/system -W error::hypothesis.errors.HypothesisWarning -W error::pytest.PytestUnknownMarkWarning --cov=src/reqhq --cov-report=term-missing -q
}
if ($LASTEXITCODE -eq 0) {
  .venv/Scripts/python.exe -m pytest tests/reqhq/system -m "alloy_live" -W error::hypothesis.errors.HypothesisWarning -W error::pytest.PytestUnknownMarkWarning -q
}
```
