# 0026. Cleaner v2 Refinements, SRS Heading Promotion, Section-Role Triage, and TF-IDF Semantic Chunking

- **Date:** 2026-11-27
- **Status:** Accepted
- **PR / Branch:** `feat/large-scale-eval-promise-corpus-ablations`
- **Supersedes:** None

## Context

Manual inspection of the 15-document PURE `dev` cleaning trial (`ADR-0025`, `eval_cleaner_trial/review_ui.html`) surfaced five concrete PDF formatting edge cases (multi-column tables flattened by whitespace compaction, multi-line sentences split across proper nouns and page breaks, body/reference URL false positives in `WEB_PRINT_NAVIGATION`, right-aligned page-numbered TOC entries quoting `"must"`/`"shall"` in `2007 - puget sound.pdf.txt`, and vertically-centered 2-column table row numbers dropped mid-sentence in `0000 - cctns.pdf.txt`). Furthermore, auditing `HierarchicalChunker` and `RequirementDecomposer` revealed two upstream bottlenecks on `TIER_0` hardware: (1) plain `.txt` PDFs lack Markdown `#` headings, causing flat 8,000-char slicing and missed glossary tables, and (2) `RequirementDecomposer._decompose_single_chunk()` caps prompts at `_MAX_CHUNK_EXCERPT_CHARS = 3600`, silently clipping the second half of 8,000-char chunks while still burning local Ollama calls on `Glossary` and `References` sections.

## Decision

Extend the standalone pre-LLM ingestion pipeline (building on `ADR-0025` without mutating immutable raw snapshots or existing Phase 1–5 defaults) with five human-verified cleaner refinements and an intelligent zero-dependency semantic chunking module:

1. **Cleaner v2 Refinements (`src/reqhq/ingestion/cleaner.py`):**
   - **Column-Aligned Table Reconstruction (`TABLE_RECONSTRUCTION`, `_try_reconstruct_pipe_table`):** Reconstruct genuine multi-column PDF tables (requiring `>= 2` non-heading rows aligned within `±3` chars on column 2) into Markdown pipe tables (`| Term | Definition |`, `| Name | Role | Title |`) with X-offset wrapped-cell merging, while rejecting full-justified prose and section headings. Add a **Rendered Clean View** tab in `review_ui.html`.
   - **Heading-Aware & Cross-Page Reflow (`_is_heading_line`, `_PAGE_BREAK_MARKER`):** Separate real headings from numbered requirement sentences (`shall`/`must`/modal verbs or `> 9` words) and rejoin wrapped sentences across capitalized proper nouns and stripped page breaks.
   - **Strict URL Footer Guard:** Require a trailing print date (`MM/DD/YYYY`) AND page-edge position for `_WEB_PRINT_FOOTER_RE`, restoring 15 body/reference URLs across `space fractions`, `znix`, `gamma j`, and `mdot`.
   - **Precision TOC State Machine:** Strip unambiguous right-aligned page-numbered TOC entries (including 2-line wrapped TOC entries) even when titles quote `"must"`/`"shall"`, and exit TOC mode immediately via duplicate-section-number body-start detection (`toc_seen_sections`) + 5-line lookahead so consecutive real body headings (`1.0 Introduction` / `1.1 Purpose`) are always preserved.
   - **Mid-Block Vertically-Centered Row-Number Hoisting (`_hoist_mid_block_row_numbers_in_block`):** Hoist mid-sentence standalone/inline PDF row numbers (84 occurrences in `cctns`) cleanly to the start of each requirement block, exclude pure numeric tokens (`1.`) from repeated-header signatures, and normalize Wingdings `z ` bullets to `- `.

2. **Intelligent Pre-LLM Semantic Front-End (`src/reqhq/ingestion/semantic_chunker.py`):**
   - **Heading Promotion (`promote_srs_headings`):** Promote IEEE-830 numbered and standalone SRS titles to Markdown `#` hierarchy without touching normative requirement sentences.
   - **Section-Role Triage (`SectionRole`, `classify_section_role`):** Classify sections into `GLOSSARY_ONLY`, `BACKGROUND_REFERENCE`, `CONSTRAINTS_ASSUMPTIONS`, and `REQUIREMENT_DENSE`. Route `GLOSSARY_ONLY` directly to `GlobalGlossary` and `BACKGROUND_REFERENCE` to System Context (`0` LLM calls), with a hard safety guarantee that any section containing normative modals (`shall`/`must`/`REQ-`) is never skipped.
   - **Zero-Dependency TF-IDF + RE Concept Synonym Vectorizer (`TFIDFConceptVectorizer`, pluggable `TextEmbedder` Protocol):** Expand domain synonyms (`login/auth/rbac/mfa -> _re_security`, `latency/p95/sla -> _re_performance`, `backup/failover/uptime -> _re_reliability`, etc.) in `< 15 ms/doc` with `0 MB` GPU/RAM model-swap overhead on `TIER_0`.
   - **Cosine-Valley Semantic Splitting & Packing (`SemanticSRSChunker`):** Pack small sibling LLM-routed sections and split large sections at local TF-IDF cosine-similarity valleys inside a **`3,400`-char target budget** (matching `_MAX_CHUNK_EXCERPT_CHARS = 3600` for `0%` silent tail clipping).
   - **Critic Batch Co-Location & Trace-Preserving Deduplication:** Provide `cluster_requirements_into_batches()` (co-locating semantically closest requirements into the same `_BATCH_SIZE = 5` critic prompt at zero extra LLM cost) and `deduplicate_requirements()` (merging cosine `>= 0.85` near-duplicates while uniting all `TraceLink`s and tags).
   - **Verification Studio Integration:** Add the **Smart Sections & Semantic Chunks** tab to `review_ui.html` and emit `semantic_plans/<doc_id>.semantic.json` in `clean_batch()`.

## Consequences

- **Positive:** Across the 15-document stratified PURE `dev` trial (`1,649,072` raw chars), Cleaner v2 removes **`43.96%`** of raw boilerplate (`924,219` chars, `0` normative lines lost), and Section-Role Triage skips an additional **`11.32%`** (`89` zero-LLM glossary/background chunks), achieving a **`49.98%` combined raw-to-LLM context reduction** (`1,649,072 -> 824,945` LLM-routed chars across `355` cohesive `<= 3,400`-char chunks, up to **`-64.4%`** on `2003 - pnnl.pdf.txt`) with `0%` prompt tail clipping.
- **Positive:** `100.00%` line coverage across both `cleaner.py` and `semantic_chunker.py`, deterministic Hypothesis PBT verification of lossless batch partitioning and provenance preservation, and zero disruption to Phases 1–5.
- **Trade-off:** Pure-Python TF-IDF + RE synonym expansion trades rare open-domain paraphrase recall for `< 15 ms` deterministic speed and zero Ollama model-swap latency on `TIER_0`; the `TextEmbedder` Protocol allows opting into local neural embeddings (`nomic-embed-text`) on `TIER_1+` hardware later.
