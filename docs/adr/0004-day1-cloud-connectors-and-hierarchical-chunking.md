# 0004. Day 1 Cloud Connectors and Hierarchical Chunking

- **Date:** 2026-11-24
- **Status:** Accepted
- **PR / Branch:** `adr/0004-day1-cloud-connectors-and-hierarchical-chunking`
- **Supersedes:** None

## Context

ReqHQ v1 suffered from two major ingestion limitations documented in `README.md`:

1. **120,000-Character Prefix Truncation:** 18 of 61 PURE dev documents exceeded 120,000 characters and were truncated to a fixed prefix, making full-document requirement recall impossible on enterprise PRDs and SRS bundles.
2. **Manual Copy-Paste Only:** Users could only paste raw text or upload single files in Streamlit without read-only enterprise cloud connectors, immutable source snapshots, or structured page/section anchors for traceability.

Additionally, ReqHQ v2 Phase 1 runs on **Python 3.15.0 on Windows (`win_amd64`)**. A pre-implementation wheel audit (`pip download --only-binary=:all:`) on Python 3.15.0 Windows produced concrete packaging findings:

- **Pre-built wheels available & installed:** `pypdf` (`7.1.1`, pure Python), `python-docx` (`1.2.0` + `lxml` `cp315` wheel), `google-api-python-client` (`2.202.0`), `google-auth-oauthlib` (`1.5.0`), `msal` (`1.39.0`), `httpx` (`0.28.1`), `pydantic` (`2.14.4`).
- **`pymupdf`:** `pymupdf-1.28.2-cp310-abi3-win_amd64.whl` is available via stable ABI, making it viable as an optional accelerator extra (`[pdf-fast]`) without burdening the pure-Python base install.
- **`docling`:** **Fails wheel resolution on Python 3.15.0 Windows** due to missing Python 3.15 binary wheels / transitive dependency conflicts. Requiring `docling` in base dependencies would break installation on Python 3.15 or force native C/Rust compilation on Windows laptops.

## Decision

We implement a unified, read-only, tamper-evident ingestion and lossless chunking pipeline under `src/reqhq/ingestion/`:

1. **Local & Day 1 Read-Only Cloud Connectors (`connectors.py`):**
   - `LocalFileLoader`: Loads single files or recursive directories of `.md`, `.txt`, `.pdf`, and `.docx`, computes SHA-256 digests, and cleanly rejects unsupported extensions.
   - `GDriveConnector`: Day 1 read-only OAuth2 connector strictly enforcing the `https://www.googleapis.com/auth/drive.readonly` scope (rejecting broader or write scopes); supports exporting Google Docs (Markdown/DOCX/plain text) and downloading binary `.pdf`/`.docx`/`.md`/`.txt` files via an injectable Drive client.
   - `SharePointConnector`: Day 1 read-only OneDrive/SharePoint connector strictly enforcing Microsoft Graph `Files.Read.All` via `msal` and downloading items over `httpx` with injectable MSAL app and HTTP client.
2. **Immutable Local Snapshots (`snapshot.py`):**
   - `SnapshotStore` persists every ingested source as a read-only local snapshot at `<sessions_dir>/<session_id>/snapshots/<sha256[:12]>_<safe_filename>` alongside a JSON manifest (`SnapshotRecord`).
   - `verify_snapshot()` recomputes SHA-256 on demand and detects missing files or byte tampering, guaranteeing reproducible traceability even if upstream cloud documents change later.
3. **Deterministic Markdown Normalization via Pluggable Parser Interface (`normalizer.py`):**
   - A `DocumentParser` Protocol and `ParserRegistry` decouple normalization from any single PDF/DOCX library.
   - **Phase 1 Default Parsers (Python 3.15-Safe Base):** Pure-Python `PlainTextMarkdownParser` (`.md`, `.txt`), `PyPdfParser` (`pypdf` 7.1.1, emitting explicit `[page:N]` anchors on every page), and `PythonDocxParser` (`python-docx` 1.2.0, preserving Markdown headings `#..######`, paragraphs, tables, and `[section:slug]` anchors).
   - **Optional High-Layout Extras:** `PyMuPDFParser` (`[pdf-fast]`, `pymupdf>=1.25`) and `DoclingParser` (`[docling]`, `docling>=2.0; python_version < "3.14"`) register dynamically only when installed and fall back seamlessly to `PyPdfParser` / `PythonDocxParser` when absent.
4. **Hierarchical Heading-Aware Chunking & Global Glossary (`chunker.py`):**
   - `HierarchicalChunker(max_chunk_chars=8000, overlap_chars=0)` recursively splits normalized Markdown along heading boundaries while preserving `heading_path` and `[page:N]` / `[section:...]` anchors on every chunk.
   - Extracts a persistent `GlobalGlossary` (defined terms, acronyms, key entities, section titles) shared across chunks so local SLMs retain document-wide terminology without overflowing small context windows.
   - Guarantees **0% truncation (`truncation_ratio == 0.0`)** verified on synthetic documents exceeding 150,000 characters, completely eliminating the legacy 120K prefix truncation ceiling.

## Consequences

- **Positive:** Installs cleanly with zero compilation on Windows Python 3.15.0 (`--only-binary=:all:`) while keeping an upgrade path to `pymupdf` or `docling` when high-complexity layout OCR/table extraction is needed on supported runtimes.
- **Positive:** Read-only OAuth scopes (`drive.readonly`, `Files.Read.All`) enforce least privilege by construction, and immutable SHA-256 snapshots anchor every downstream requirement to exact source bytes.
- **Positive:** Zero-truncation hierarchical chunking + `GlobalGlossary` unlocks full-document coverage on 150K+ character enterprise specifications within 4B–8B local SLM context limits.
- **Trade-off:** Pure-Python `pypdf` cannot OCR scanned image-only PDFs or reconstruct intricate multi-column visual layouts as accurately as `docling` or `pymupdf`; users processing complex PDFs on Python 3.11–3.13 can opt into `pip install -e ".[pdf-fast]"` or `pip install -e ".[docling]"`.
