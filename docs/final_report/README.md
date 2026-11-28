# ReqHQ v2 Final Report & Publication Artifacts

**Team:** `req-hq` — IIIT Hyderabad  
**Authors:**
- Panem Chaitanya Pavan Kumar (`2025204023`)
- Anjali Yadav (`2025204015`)
- Anshul Agrawal (`2025901015`)

## Overview

This directory is the target home for auto-generated Phase 4 publication artifacts produced by `src/reqhq/reporting/` from SHA-256-verified evaluation runs:

| Artifact | Producer | Purpose |
| :--- | :--- | :--- |
| `FINAL_REPORT.md` | `reqhq eval report --format md\|both` | Complete 12-section Markdown technical report with H1–H4 tables, embedded SVG figures, 7-category error taxonomy, threats to validity, and `SystemContextBase` brownfield seam |
| `tables_latex.tex` | `reqhq eval report --format tex\|both` | Overleaf-ready `booktabs` LaTeX tables (`\toprule`, `\midrule`, `\bottomrule`) with single-pass escaping of all LaTeX metacharacters |
| `pareto_quality_vs_cloud_cost.svg` | `reqhq eval analyze` (copied by `report`) | Pure-Python XML/SVG Pareto frontier chart (Quality vs. Cloud Cost) |
| `pareto_quality_vs_latency.svg` | `reqhq eval analyze` (copied by `report`) | Pure-Python XML/SVG Pareto frontier chart (Quality vs. Latency) |
| `cond_abc_comparison.svg` | `reqhq eval analyze` (copied by `report`) | Pure-Python XML/SVG comparison across Conditions A (`raw_prd`), B (`single_pass_nl_spec`), and C (`reqhq_full_bundle`) |
| `error_taxonomy_bar.svg` | `reqhq eval analyze` (copied by `report`) | Pure-Python XML/SVG breakdown across the 7 standardized error categories |
| `artifacts_manifest.json` | `reqhq eval package` | Tamper-evident SHA-256 manifest pinning `TIER_0`–`TIER_3` models, frozen dataset hashes, and every packaged file |

## Scientific Honesty & Mock Quarantine

`src/reqhq/reporting/generator.py` enforces **fail-closed Mock Quarantine**:

- Whenever `analysis_summary.json` has `run_mode != "live"`, `quarantined: true`, or `verdict_status != "VERIFIED_LIVE"`, BOTH `FINAL_REPORT.md` and `tables_latex.tex` stamp a prominent `DRY-RUN / UNSCORED MOCK DATA — NOT VALID FOR HYPOTHESIS CLAIMS` warning banner.
- Every H1–H4 verdict cell is forced to `QUARANTINED_MOCK_OR_INCOMPLETE`, and neither `VERIFIED_LIVE` nor empirical hypothesis claims are emitted.
- All four SVG figures are watermarked `DRY-RUN MOCK DATA` during dry-run analysis.

## Quick Generation Commands (Windows 11 PowerShell, Python 3.15.0)

### 1. Offline CI Dry-Run (Plumbing Verification Only)

```powershell
$env:REQHQ_ALLOY_JAR_PATH = "C:/tools/alloy/org.alloytools.alloy.dist.jar"

.venv/Scripts/python.exe -m reqhq.cli eval run --layers 1,2,3,4 --out runs/dry_run --mode mock --replicates 2 --seed 42
.venv/Scripts/python.exe -m reqhq.cli eval analyze --in runs/dry_run --out runs/dry_run/analysis
.venv/Scripts/python.exe -m reqhq.cli eval report --in runs/dry_run/analysis --out docs/final_report/dry_run --format both
.venv/Scripts/python.exe -m reqhq.cli eval package --in runs/dry_run --out runs/dry_run_bundle
.venv/Scripts/python.exe -m reqhq.cli eval verify-bundle runs/dry_run_bundle/artifacts_manifest.json
```

### 2. Live Windows 11 Empirical Run (`TIER_0` Ollama + Free-Tier Cloud + Alloy 6.2)

```powershell
$env:REQHQ_ALLOY_JAR_PATH = "C:/tools/alloy/org.alloytools.alloy.dist.jar"

# Ensure Ollama TIER_0 models are available locally
ollama pull qwen3:4b
ollama pull phi4-mini

# Run Layers 1-4 in live mode (resumable across free-tier RPD pauses)
.venv/Scripts/python.exe -m reqhq.cli eval run --layers 1,2,3,4 --out runs/live_eval --mode live --replicates 3 --seed 42
# If interrupted by free-tier cooldowns, resume cleanly with zero duplicate records:
.venv/Scripts/python.exe -m reqhq.cli eval resume --out runs/live_eval

# Strict-live statistical analysis, report generation, packaging, and SHA-256 verification
.venv/Scripts/python.exe -m reqhq.cli eval analyze --in runs/live_eval --out runs/live_eval/analysis --strict-live
.venv/Scripts/python.exe -m reqhq.cli eval report --in runs/live_eval/analysis --out docs/final_report/live_release --format both
.venv/Scripts/python.exe -m reqhq.cli eval package --in runs/live_eval --out runs/live_release_bundle
.venv/Scripts/python.exe -m reqhq.cli eval verify-bundle runs/live_release_bundle/artifacts_manifest.json
```

### 3. Overleaf LaTeX Import

Add `\usepackage{booktabs}` to your Overleaf document preamble and `\input{tables_latex.tex}` directly where your evaluation tables belong. For full environment setup, pinned models, and frozen dataset verification, see [REPRODUCIBILITY.md](../../REPRODUCIBILITY.md).
