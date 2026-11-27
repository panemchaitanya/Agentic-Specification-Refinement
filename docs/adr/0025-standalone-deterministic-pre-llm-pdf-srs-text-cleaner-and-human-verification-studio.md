# 0025. Standalone Deterministic Pre-LLM PDF/SRS Text Cleaner and Human Verification Studio

- **Date:** 2026-11-27
- **Status:** Accepted
- **PR / Branch:** `feat/large-scale-eval-promise-corpus-ablations`
- **Supersedes:** None

## Context

Empirical auditing of 15 size-stratified real-world SRS documents in `data/frozen/pure/text/dev/` (`10.7 KB` to `456.2 KB`, `1` to `176` pages, `1,649,072` total raw characters) revealed that `23%` to `57%` of raw PDF-to-text characters consist of non-requirement boilerplate: repeating page-edge headers/footers across `
` form-feed page breaks (up to `681` lines / `49.3 KB` in a single document), multi-page Tables of Contents and Lists of Figures/Tables, Revision/Document History blocks, browser print URL footers and HTML navigation links (`Back to Contents`), standalone page/section stamps (`1 - 1`, `C - 1`, `iii`, `Page 2 of 19`), cover-page metadata/copyright notices, decorative rules, excessive blank-line/column padding (`15%–49%` blank lines), and mid-sentence PDF hard wraps. Sending raw PDF conversions directly to `TIER_0` local SLMs wastes ~45% of context tokens and triggers redundant 45–60s local LLM calls on cover/TOC pages. While `RequirementDecomposer` previously applied basic noise filtering only in its offline fallback path (`_is_noise_line`), live LLM decomposition received raw uncleaned chunks.

## Decision

Introduce a standalone, opt-in deterministic pre-LLM PDF/SRS text cleaning module and human-in-the-loop verification studio without altering immutable raw snapshot provenance (`SnapshotStore` SHA-256) or mutating existing Phase 1–5 pipelines by default:

1. **Standalone Module (`src/reqhq/ingestion/cleaner.py`, exported from `reqhq.ingestion`):**
   - Implement `PDFTextCleaner`, `CleanerConfig`, `NoiseCategory` (10 empirically derived categories), `RemovedLineAudit`, `DocumentCleanResult`, `BatchCleanSummary`, and `select_stratified_files()`.
   - Split raw text via `split("\n")` (explicitly avoiding Python `str.splitlines()`, which strips `
` page breaks) to preserve exact 1-based line numbers and page boundaries for frequency-based running header/footer detection (`>= max(3, ceil(0.15 * pages))` occurrences in the top/bottom 3 non-empty lines per page).
   - Apply deterministic block state machines for `TOC_AND_LISTS` and `REVISION_AND_DOC_HISTORY` (bounded by an 80-line safety ceiling), line-level filters for `PAGE_NUMBER_STAMP`, `WEB_PRINT_NAVIGATION`, `COPYRIGHT_AND_LEGAL`, `COVER_FRONTMATTER`, and `DECORATIVE_RULE`, whitespace compaction (`EXCESS_WHITESPACE`), and safe mid-sentence PDF wrap/hyphen reflow (`PDF_REFLOW_NORMALIZATION`).
   - Enforce a hard normative safety shield (`protect_normative_lines=True`): lines matching `\b(shall(?:\s+not)?|must(?:\s+not)?|(?:REQ|FR|NFR|SRS|UC)-\d+)\b` are never silently dropped, and every removed line is recorded in `RemovedLineAudit` with original line number, page number, rule detail, and normative-modal flag.

2. **Interactive Human Verification Studio & CLI (`render_cleaning_review_ui()` & `reqhq clean trial`):**
   - Generate a zero-dependency single-file HTML studio (`review_ui.html`) alongside `raw/`, `cleaned/`, `audits/*.audit.json`, and `batch_summary.json`.
   - Provide side-by-side Raw vs. Cleaned diff highlighting by rule category, filterable Removed Lines Audit table, dedicated Normative Safety Check tab, per-rule reduction bar charts, and per-document human review verdicts (`Approve` / `Needs Tuning` / `Flag False Positive` + notes) exportable as `human_review_verdicts.json`.
   - Enforce `docs/adr/` write-refusal guards on `reqhq clean trial`.

3. **No Runtime LLM Calls for Boilerplate Cleaning:**
   - Reject runtime LLM-based document cleaning because it consumes local SLM compute on noise, introduces non-deterministic silent requirement rewrites, and lacks whole-document cross-page frequency visibility. Any future pattern discovery remains strictly offline and human-approved before codification as deterministic rules.

## Consequences

- **Positive:** Across the 15-document stratified PURE `dev` trial (`eval_cleaner_trial/`), deterministic cleaning eliminates **`44.94%`** of raw context characters (`1,649,072 -> 908,058` chars, `~185,247` tokens saved, `214 -> 121` 8K chunks) in `< 1s` with **`0` normative lines removed**, while allowing full manual human verification before integrating into live `corpus-bench` or elicitation flows.
- **Positive:** Standalone opt-in design preserves `100.00%` backward compatibility with Phases 1–5 (`SnapshotStore`, `HierarchicalChunker`, `RequirementDecomposer`) and can be experimented on in parallel with ongoing live Layer 1/Stage B runs.
- **Trade-off:** Purely deterministic heuristics may leave rare domain-specific cover boilerplate untouched (preferring conservative recall over false positives); the interactive Verification Studio lets reviewers flag misses and export verdicts for iterative rule refinement.
