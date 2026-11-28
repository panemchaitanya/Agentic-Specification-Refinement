# 0017. Reproducible Report Pipeline and Release Manifest

- **Date:** 2026-11-25
- **Status:** Accepted
- **PR / Branch:** `adr/0017-reproducible-report-pipeline-and-release-manifest`
- **Supersedes:** None

## Context

Hand-transcribing experimental tables into final project reports and LaTeX papers is notoriously error-prone: stale numbers from earlier runs silently survive edits, unescaped `%`, `_`, `&`, or `#` characters break Overleaf LaTeX builds, mock dry-run outputs risk leaking into prose claims, and external reviewers have no cryptographic way to verify that submitted logs, charts, and reports came from an untampered run tied to pinned models and frozen datasets.

## Decision

We implement an **Automated Report Generator, Overleaf LaTeX Table Emitter, and SHA-256 Tamper-Evident Release Bundler** under `src/reqhq/reporting/` (`models.py`, `generator.py`, `packager.py`), paired with repository reproducibility documentation (`docs/final_report/README.md`, `REPRODUCIBILITY.md`) and wired into `reqhq eval report`, `reqhq eval package`, and `reqhq eval verify-bundle` in `src/reqhq/cli.py`:

1. **Manifest-Driven `FINAL_REPORT.md` & Escaped `tables_latex.tex` (`generator.py`):**
   - `generate_final_report(analysis_dir, out_dir, *, fmt="both")` reads `analysis_summary.json` (ADR-0016) and emits LF-normalized `FINAL_REPORT.md` and Overleaf-ready `tables_latex.tex` (`booktabs`: `\toprule`, `\midrule`, `\bottomrule`) strictly from verified analysis metrics—never hand-typed numbers.
   - `escape_latex(text)` deterministically escapes `\`, `{`, `}`, `$`, `&`, `#`, `_`, `%`, `~`, and `^` so zero unescaped special characters can break LaTeX compilation.
   - **Fail-Closed Mock Quarantine Banner:** Whenever `run_mode != "live"` or `quarantined is True`, the generator stamps `> [!WARNING] DRY-RUN / UNSCORED MOCK DATA — NOT VALID FOR HYPOTHESIS CLAIMS` at the top of `FINAL_REPORT.md`, renders verdict cells as `QUARANTINED_MOCK_OR_INCOMPLETE`, and strictly forbids emitting `VERIFIED_LIVE`, `SUPPORTED`, or `REFUTED` claims.
   - Includes full architecture, `src/specpipe/` -> `src/reqhq/` evolution, experimental setup, `H1`–`H4` tables, embedded SVG figures, 7-category error analysis, threats to validity, and Phase 5 brownfield seam (`SystemContextBase`, ADR-0006).
2. **Tamper-Evident Release Manifest & Verification (`packager.py`):**
   - `package_release_bundle(input_dir, out_dir, *, frozen_root=Path("data/frozen"))` collects evaluation manifests, JSONL logs, `analysis_summary.json`, SVG charts, `FINAL_REPORT.md`, and `tables_latex.tex`, records pinned local model tiers (`TIER_0`–`TIER_3`), seed, and read-only SHA-256 digests of `data/frozen/pure/FREEZE.json` and `data/frozen/promise_nfr/FREEZE.json`, and writes `artifacts_manifest.json` (`ArtifactsManifest` with `ReleaseArtifactEntry` hashes, LF endings).
   - `verify_bundle(manifest_path, *, raise_on_error=False) -> BundleVerificationResult` validates every tracked artifact remains strictly inside the bundle root (rejecting path traversal and symlink escapes) and matches exact byte length and SHA-256 digest, raising `BundleTamperError` on any 1-byte modification or missing file.
3. **End-to-End Reproducibility Guide (`REPRODUCIBILITY.md`, `docs/final_report/README.md`):**
   - Documents copy-paste commands using `.venv/Scripts/python.exe` (Python 3.15.0, Windows 11) and `REQHQ_ALLOY_JAR_PATH` covering: (a) offline CI dry-run (`reqhq eval run --mode mock` -> `analyze` -> `report` -> `package` -> `verify-bundle`), (b) live `TIER_0` Ollama + Gemini/Groq + Alloy 6.2 execution and `reqhq eval resume`, and (c) cryptographic bundle verification.

## Consequences

- **Positive:** Eliminates transcription drift between empirical logs, Markdown reports, and LaTeX tables while guaranteeing clean Overleaf compilation via property-tested `escape_latex()`.
- **Positive:** `artifacts_manifest.json` + `reqhq eval verify-bundle` gives reviewers single-command cryptographic verification of the entire evaluation release, including frozen dataset provenance.
- **Positive:** Strictly preserves repository ADR namespace isolation (`docs/adr/0001`–`0017` remain repo governance Nygard ADRs, separate from `docs/final_report/` and session export bundles).
- **Trade-off:** Regenerating reports overwrites prose inside `FINAL_REPORT.md`; narrative refinements belong in the generator template or companion `docs/final_report/README.md` rather than manual post-hoc edits to generated output files.
- **Obligation:** Release packaging must remain strictly read-only toward `data/frozen/` and `src/specpipe/`, and `verify_bundle()` must continue failing closed on path traversal, missing files, or single-byte tampering.
