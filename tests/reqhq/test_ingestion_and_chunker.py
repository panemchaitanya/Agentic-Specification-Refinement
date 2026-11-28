"""Offline unit tests for reqhq.ingestion connectors, snapshots, normalizer, and chunker."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from docx import Document as DocxDocument
from pydantic import ValidationError
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)

# Ensure src/ is importable even before editable install.
_SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from reqhq.ingestion import (  # noqa: E402
    GDRIVE_READONLY_SCOPE,
    GOOGLE_DOC_MIME,
    SHAREPOINT_READONLY_SCOPE,
    ChunkingResult,
    ConnectorError,
    DoclingParser,
    DocumentAnchor,
    DocumentChunk,
    DocumentParser,
    GDriveConnector,
    GlobalGlossary,
    GlossaryEntry,
    HierarchicalChunker,
    LocalFileLoader,
    NormalizedDocument,
    ParserRegistry,
    PlainTextMarkdownParser,
    PyMuPDFParser,
    PyPdfParser,
    PythonDocxParser,
    ScopeViolationError,
    SharePointConnector,
    SnapshotIntegrityError,
    SnapshotRecord,
    SnapshotStore,
    SourceDocument,
    UnsupportedFileTypeError,
    compute_sha256,
    infer_media_type,
    normalize_file,
    sanitize_filename,
    slugify_heading,
)


# ---------------------------------------------------------------------------
# Synthetic document generators (100% offline, real binary formats)
# ---------------------------------------------------------------------------


def _escape_pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _create_sample_pdf(path: Path, page_texts: list[str]) -> Path:
    """Create a valid multi-page PDF with extractable Helvetica text using pypdf.PdfWriter."""
    writer = PdfWriter()
    for text in page_texts:
        page = writer.add_blank_page(width=612, height=792)
        if text:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            font_ref = writer._add_object(font)
            resources = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
            )
            page[NameObject("/Resources")] = resources

            stream = DecodedStreamObject()
            stream.set_data(
                f"BT /F1 12 Tf 72 720 Td ({_escape_pdf_text(text)}) Tj ET".encode("latin-1")
            )
            stream_ref = writer._add_object(stream)
            page[NameObject("/Contents")] = stream_ref

    with path.open("wb") as fh:
        writer.write(fh)
    return path


def _create_sample_docx(path: Path) -> Path:
    """Create a valid .docx with title, headings, paragraphs, and a table."""
    doc = DocxDocument()
    doc.add_heading("Platform Specification", level=0)
    doc.add_paragraph("Introductory overview for OrderService and PaymentGateway.")
    doc.add_heading("Functional Requirements", level=1)
    doc.add_paragraph("Service Level Agreement (SLA) must meet 99.95% uptime.")
    doc.add_heading("Details", level=2)
    doc.add_paragraph("First details section.")
    table = doc.add_table(rows=3, cols=2)
    table.cell(0, 0).text = "Term"
    table.cell(0, 1).text = "Definition"
    table.cell(1, 0).text = "RBAC"
    table.cell(1, 1).text = "Role-Based Access Control | strict"
    table.cell(2, 0).text = "Idempotency"
    table.cell(2, 1).text = "Repeated calls produce identical state"
    doc.add_heading("Details", level=2)
    doc.add_paragraph("Second details section verifying duplicate slug handling.")
    doc.save(str(path))
    return path


def _build_large_markdown_document(target_min_chars: int = 160_000) -> str:
    """Build a deterministic >150,000-character multi-section Markdown document."""
    parts: list[str] = [
        "Preamble metadata line before any heading.\n\n",
        "# Enterprise Specification Document\n",
        "[section:enterprise-specification-document]\n",
        "[page:1]\n",
        "This document defines the Role-Based Access Control (RBAC) and "
        "Service Level Agreement (SLA) standards for OrderService and PaymentGateway.\n",
        'In this specification, "Tenant Isolation" means strict cryptographic separation '
        "of customer records across storage partitions.\n\n",
        "## Glossary and Definitions\n",
        "[section:glossary-and-definitions]\n",
        "- **Idempotency Key**: Client-generated UUID preventing duplicate ledger writes.\n",
        "- **Circuit Breaker** — Automatic fault boundary isolating degraded downstreams.\n",
        "PBT (Property-Based Testing) validates invariants automatically.\n\n",
        "| Term | Definition |\n",
        "| --- | --- |\n",
        "| Audit Trail | Immutable append-only ledger of user and system actions |\n",
        "| Cold Start | Initial container boot latency before serving traffic |\n\n",
    ]

    sec_idx = 1
    page_idx = 2
    while sum(len(p) for p in parts) < target_min_chars:
        parts.append(f"## Domain Module {sec_idx}: Subsystem Architecture\n")
        parts.append(f"[section:domain-module-{sec_idx}]\n")
        parts.append(f"[page:{page_idx}]\n")
        parts.append(
            f"### Requirements for Module {sec_idx}\n"
            f"OrderService coordinates with PaymentGateway and AuditLedger during workflow {sec_idx}. "
            f"Every API request must include a valid Idempotency Key and obey RBAC policy rules.\n\n"
        )
        # Add multiple rich paragraphs plus a long single-line sentence block.
        for para_idx in range(1, 7):
            parts.append(
                f"Paragraph {sec_idx}.{para_idx}: "
                + (
                    f"REQ-{sec_idx:03d}-{para_idx:02d} requires deterministic validation, "
                    "zero-truncation traceability, strict SHA-256 snapshotting, and repeatable "
                    "verification across local and cloud ingestion pipelines. "
                )
                * 8
                + "\n\n"
            )
        # Include an unbroken token block in module 3 to exercise hard character splitting.
        if sec_idx == 3:
            parts.append("UNBROKEN_TOKEN_" + ("X" * 1200) + "\n\n")
        sec_idx += 1
        page_idx += 1

    return "".join(parts)


# ---------------------------------------------------------------------------
# Strict Pydantic extra="forbid" tests
# ---------------------------------------------------------------------------


def test_models_reject_extra_fields() -> None:
    sha = compute_sha256(b"abc")
    with pytest.raises(ValidationError):
        SourceDocument(
            filename="a.md",
            source_uri="file:///a.md",
            source_type="local",
            media_type="text/markdown",
            extension=".md",
            content=b"abc",
            sha256=sha,
            size_bytes=3,
            unexpected_field=True,  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        DocumentAnchor(kind="page", value="1", tag="[page:1]", char_offset=0, bogus=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        GlossaryEntry(term="SLA", category="acronym", extra_x=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        GlobalGlossary(unknown="nope")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        DocumentChunk(
            chunk_id="chunk-0000",
            chunk_index=0,
            start_char=0,
            end_char=3,
            char_count=3,
            text="abc",
            source_sha256=sha,
            extra_field=1,  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        ChunkingResult(
            source_sha256=sha,
            source_path=Path("a.md"),
            total_chars=3,
            covered_chars=3,
            truncation_ratio=0.0,
            chunk_count=0,
            max_chunk_chars=100,
            overlap_chars=0,
            glossary=GlobalGlossary(),
            bad=1,  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        NormalizedDocument(
            source_path=Path("a.md"),
            source_sha256=sha,
            parser_name="plain_text_markdown",
            media_type="text/markdown",
            markdown="abc",
            char_count=3,
            bad=1,  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        SnapshotRecord(
            snapshot_id="abc_a.md",
            session_id="s1",
            original_filename="a.md",
            safe_filename="a.md",
            source_uri="file:///a.md",
            sha256=sha,
            size_bytes=3,
            snapshot_path=Path("a.md"),
            manifest_path=Path("a.md.manifest.json"),
            created_at="2026-01-01T00:00:00Z",
            extra=1,  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# LocalFileLoader tests
# ---------------------------------------------------------------------------


def test_local_file_loader_loads_all_supported_formats(tmp_path: Path) -> None:
    md_file = tmp_path / "spec.md"
    md_file.write_text("# Spec\nHello world\n", encoding="utf-8")

    txt_file = tmp_path / "notes.txt"
    txt_file.write_text("Plain text notes", encoding="utf-8")

    sub = tmp_path / "nested"
    sub.mkdir()
    docx_file = _create_sample_docx(sub / "design.docx")
    pdf_file = _create_sample_pdf(sub / "arch.pdf", ["Page one content", "Page two content"])
    ignored = sub / "diagram.png"
    ignored.write_bytes(b"\x89PNG\r\n")

    loader = LocalFileLoader()

    md_doc = loader.load_file(md_file)
    assert md_doc.source_type == "local"
    assert md_doc.extension == ".md"
    assert md_doc.media_type == "text/markdown"
    assert md_doc.sha256 == compute_sha256(md_file.read_bytes())
    assert md_doc.size_bytes == len(md_file.read_bytes())

    pdf_doc = loader.load_file(pdf_file)
    assert pdf_doc.extension == ".pdf"
    assert pdf_doc.media_type == "application/pdf"

    docx_doc = loader.load_file(docx_file)
    assert docx_doc.extension == ".docx"

    # Recursive folder load skips unsupported .png by default
    docs = loader.load_path(tmp_path, recursive=True)
    assert sorted(d.filename for d in docs) == ["arch.pdf", "design.docx", "notes.txt", "spec.md"]

    # Non-recursive load only picks top-level files
    top_docs = loader.load_directory(tmp_path, recursive=False)
    assert sorted(d.filename for d in top_docs) == ["notes.txt", "spec.md"]

    # Single-file load_path
    single = loader.load_path(txt_file)
    assert len(single) == 1 and single[0].filename == "notes.txt"

    # Rejects unsupported extension cleanly
    with pytest.raises(UnsupportedFileTypeError):
        loader.load_file(ignored)

    with pytest.raises(UnsupportedFileTypeError):
        loader.load_directory(tmp_path, recursive=True, strict_extensions=True)

    with pytest.raises(UnsupportedFileTypeError):
        infer_media_type(".exe")


def test_local_file_loader_error_cases(tmp_path: Path) -> None:
    loader = LocalFileLoader()
    with pytest.raises(FileNotFoundError):
        loader.load_file(tmp_path / "missing.md")
    with pytest.raises(IsADirectoryError):
        loader.load_file(tmp_path)
    with pytest.raises(FileNotFoundError):
        loader.load_directory(tmp_path / "missing_dir")
    f = tmp_path / "a.txt"
    f.write_text("hi", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        loader.load_directory(f)
    with pytest.raises(FileNotFoundError):
        loader.load_path(tmp_path / "missing_any")


# ---------------------------------------------------------------------------
# GDriveConnector tests (mocked Drive service, 100% offline)
# ---------------------------------------------------------------------------


class _FakeDriveRequest:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def execute(self) -> Any:
        return self._payload


class _FakeDriveFilesResource:
    def __init__(
        self,
        metadata: dict[str, dict[str, Any]],
        binary_data: dict[str, bytes],
        exported_data: dict[tuple[str, str], bytes],
        folders: dict[str, list[dict[str, Any]]],
    ) -> None:
        self._metadata = metadata
        self._binary_data = binary_data
        self._exported_data = exported_data
        self._folders = folders
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, **kwargs: Any) -> _FakeDriveRequest:
        self.calls.append(("get", kwargs))
        return _FakeDriveRequest(self._metadata[kwargs["fileId"]])

    def get_media(self, **kwargs: Any) -> _FakeDriveRequest:
        self.calls.append(("get_media", kwargs))
        return _FakeDriveRequest(self._binary_data[kwargs["fileId"]])

    def export_media(self, **kwargs: Any) -> _FakeDriveRequest:
        self.calls.append(("export_media", kwargs))
        return _FakeDriveRequest(self._exported_data[(kwargs["fileId"], kwargs["mimeType"])])

    def list(self, **kwargs: Any) -> _FakeDriveRequest:
        self.calls.append(("list", kwargs))
        q = kwargs["q"]
        for fid, items in self._folders.items():
            if f"'{fid}'" in q:
                return _FakeDriveRequest({"files": items})
        return _FakeDriveRequest({"files": []})


class _FakeDriveService:
    def __init__(self, files_res: _FakeDriveFilesResource) -> None:
        self._files_res = files_res

    def files(self) -> _FakeDriveFilesResource:
        return self._files_res


def test_gdrive_scope_enforcement() -> None:
    conn = GDriveConnector(scopes=(GDRIVE_READONLY_SCOPE,))
    assert conn.scopes == (GDRIVE_READONLY_SCOPE,)

    for bad_scopes in (
        (),
        ("https://www.googleapis.com/auth/drive",),
        ("https://www.googleapis.com/auth/drive.file",),
        (GDRIVE_READONLY_SCOPE, "https://www.googleapis.com/auth/drive"),
    ):
        with pytest.raises(ScopeViolationError):
            GDriveConnector(scopes=bad_scopes)


def test_gdrive_export_and_binary_download(tmp_path: Path) -> None:
    docx_path = _create_sample_docx(tmp_path / "exported.docx")
    docx_bytes = docx_path.read_bytes()
    pdf_path = _create_sample_pdf(tmp_path / "remote.pdf", ["Remote PDF page 1"])
    pdf_bytes = pdf_path.read_bytes()

    files_res = _FakeDriveFilesResource(
        metadata={
            "doc-1": {
                "id": "doc-1",
                "name": "Product PRD",
                "mimeType": GOOGLE_DOC_MIME,
                "modifiedTime": "2026-11-20T00:00:00Z",
            },
            "pdf-2": {
                "id": "pdf-2",
                "name": "Architecture.pdf",
                "mimeType": "application/pdf",
                "modifiedTime": "2026-11-21T00:00:00Z",
            },
            "img-3": {
                "id": "img-3",
                "name": "logo.png",
                "mimeType": "image/png",
            },
        },
        binary_data={"pdf-2": pdf_bytes, "img-3": b"pngbytes"},
        exported_data={
            ("doc-1", "text/markdown"): b"# Product PRD\nRequirements body.",
            ("doc-1", "text/plain"): b"Product PRD\nRequirements body.",
            (
                "doc-1",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ): docx_bytes,
        },
        folders={
            "folder-99": [
                {"id": "doc-1", "name": "Product PRD", "mimeType": GOOGLE_DOC_MIME},
                {"id": "pdf-2", "name": "Architecture.pdf", "mimeType": "application/pdf"},
                {"id": "img-3", "name": "logo.png", "mimeType": "image/png"},
            ]
        },
    )
    service = _FakeDriveService(files_res)
    connector = GDriveConnector(service=service)

    # Export Google Doc as Markdown
    md_doc = connector.fetch_file("doc-1", export_format="md")
    assert md_doc.filename == "Product PRD.md"
    assert md_doc.source_type == "gdrive"
    assert md_doc.source_uri == "gdrive://doc-1"
    assert md_doc.extension == ".md"
    assert md_doc.metadata["exported_from_google_doc"] is True

    # Export Google Doc as DOCX and plain text
    docx_doc = connector.fetch_file("doc-1", export_format="docx")
    assert docx_doc.filename == "Product PRD.docx"
    assert docx_doc.sha256 == compute_sha256(docx_bytes)

    txt_doc = connector.fetch_file("doc-1", export_format="txt")
    assert txt_doc.filename == "Product PRD.txt"

    # Unsupported export format rejected
    with pytest.raises(UnsupportedFileTypeError):
        connector.fetch_file("doc-1", export_format="html")  # type: ignore[arg-type]

    # Binary PDF download
    pdf_doc = connector.fetch_file("pdf-2")
    assert pdf_doc.filename == "Architecture.pdf"
    assert pdf_doc.sha256 == compute_sha256(pdf_bytes)
    assert pdf_doc.metadata["exported_from_google_doc"] is False

    # Unsupported binary rejected
    with pytest.raises(UnsupportedFileTypeError):
        connector.fetch_file("img-3")

    # Folder fetch skips unsupported by default
    folder_docs = connector.fetch_folder("folder-99", export_format="md")
    assert [d.filename for d in folder_docs] == ["Product PRD.md", "Architecture.pdf"]

    with pytest.raises(UnsupportedFileTypeError):
        connector.fetch_folder("folder-99", skip_unsupported=False)


def test_gdrive_missing_secrets_raises_connector_error(tmp_path: Path) -> None:
    connector = GDriveConnector(client_secrets_path=tmp_path / "nonexistent_secrets.json")
    with pytest.raises(ConnectorError):
        connector._get_service()


# ---------------------------------------------------------------------------
# SharePointConnector tests (mocked MSAL + httpx.MockTransport, 100% offline)
# ---------------------------------------------------------------------------


class _FakeMsalApp:
    def __init__(
        self,
        *,
        accounts: list[dict[str, Any]] | None = None,
        silent_result: dict[str, Any] | None = None,
        interactive_result: dict[str, Any] | None = None,
    ) -> None:
        self._accounts = accounts or []
        self._silent_result = silent_result
        self._interactive_result = interactive_result
        self.requested_scopes: list[list[str]] = []

    def get_accounts(self) -> list[dict[str, Any]]:
        return list(self._accounts)

    def acquire_token_silent(
        self, scopes: list[str], account: dict[str, Any]
    ) -> dict[str, Any] | None:
        self.requested_scopes.append(scopes)
        return self._silent_result

    def acquire_token_interactive(self, scopes: list[str]) -> dict[str, Any] | None:
        self.requested_scopes.append(scopes)
        return self._interactive_result


def test_sharepoint_scope_enforcement() -> None:
    conn = SharePointConnector(scopes=(SHAREPOINT_READONLY_SCOPE,))
    assert conn.scopes == (SHAREPOINT_READONLY_SCOPE,)

    conn_full = SharePointConnector(scopes=("https://graph.microsoft.com/Files.Read.All",))
    assert conn_full.scopes == ("https://graph.microsoft.com/Files.Read.All",)

    for bad_scopes in (
        (),
        ("Files.ReadWrite",),
        ("Files.ReadWrite.All",),
        ("Sites.ReadWrite.All",),
        ("Files.Read.All", "Files.ReadWrite.All"),
    ):
        with pytest.raises(ScopeViolationError):
            SharePointConnector(scopes=bad_scopes)


def test_sharepoint_token_and_graph_download() -> None:
    seen_auth_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth_headers.append(request.headers.get("Authorization"))
        url = str(request.url)
        if url.endswith("/me/drive/root/children"):
            return httpx.Response(
                200,
                json={"value": [{"id": "item-1", "name": "spec.md"}]},
            )
        if url.endswith("/sites/site-42/drive/items/folder-7/children"):
            return httpx.Response(
                200,
                json={"value": [{"id": "item-2", "name": "notes.txt"}]},
            )
        if url.endswith("/me/drive/items/item-1"):
            return httpx.Response(
                200,
                json={
                    "id": "item-1",
                    "name": "spec.md",
                    "webUrl": "https://contoso.sharepoint.com/docs/spec.md",
                    "file": {"mimeType": "text/markdown"},
                    "@microsoft.graph.downloadUrl": "https://download.local/item-1",
                },
            )
        if url == "https://download.local/item-1":
            return httpx.Response(200, content=b"# SharePoint Spec\nContent here.")
        if url.endswith("/drives/drv-9/items/item-2"):
            return httpx.Response(
                200,
                json={
                    "id": "item-2",
                    "name": "notes.txt",
                    "file": {"mimeType": "text/plain"},
                },
            )
        if url.endswith("/drives/drv-9/items/item-2/content"):
            return httpx.Response(200, content=b"Direct Graph content bytes.")
        if url.endswith("/sites/site-42/drive/items/item-bad"):
            return httpx.Response(
                200,
                json={
                    "id": "item-bad",
                    "name": "archive.zip",
                    "file": {"mimeType": "application/zip"},
                },
            )
        return httpx.Response(404)

    msal_app = _FakeMsalApp(
        accounts=[{"username": "analyst@contoso.com"}],
        silent_result={"access_token": "tok-silent-123"},
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        connector = SharePointConnector(msal_app=msal_app, http_client=client)

        children = connector.list_drive_children()
        assert len(children) == 1 and children[0]["id"] == "item-1"

        site_children = connector.list_drive_children("folder-7", site_id="site-42")
        assert len(site_children) == 1 and site_children[0]["id"] == "item-2"

        doc1 = connector.fetch_drive_item("item-1")
        assert doc1.filename == "spec.md"
        assert doc1.source_type == "sharepoint"
        assert doc1.source_uri == "https://contoso.sharepoint.com/docs/spec.md"
        assert doc1.sha256 == compute_sha256(b"# SharePoint Spec\nContent here.")
        assert "Bearer tok-silent-123" in seen_auth_headers

        # Download via /content fallback with drive_id
        doc2 = connector.fetch_drive_item("item-2", drive_id="drv-9")
        assert doc2.filename == "notes.txt"
        assert doc2.source_uri == "sharepoint://item-2"
        assert doc2.content == b"Direct Graph content bytes."

        # Unsupported extension rejected
        with pytest.raises(UnsupportedFileTypeError):
            connector.fetch_drive_item("item-bad", site_id="site-42")
    finally:
        client.close()


def test_sharepoint_msal_fallback_and_errors() -> None:
    interactive_msal = _FakeMsalApp(
        accounts=[],
        interactive_result={"access_token": "tok-interactive-456"},
    )
    conn = SharePointConnector(msal_app=interactive_msal)
    assert conn.acquire_access_token() == "tok-interactive-456"
    assert interactive_msal.requested_scopes == [[SHAREPOINT_READONLY_SCOPE]]

    failing_msal = _FakeMsalApp(
        accounts=[],
        interactive_result={"error": "access_denied", "error_description": "User cancelled"},
    )
    with pytest.raises(ConnectorError, match="User cancelled"):
        SharePointConnector(msal_app=failing_msal).acquire_access_token()

    no_interactive_msal = SimpleNamespace(get_accounts=lambda: [])
    with pytest.raises(ConnectorError):
        SharePointConnector(msal_app=no_interactive_msal).acquire_access_token()

    with pytest.raises(ConnectorError, match="client_id"):
        SharePointConnector(client_id="").acquire_access_token()


# ---------------------------------------------------------------------------
# SnapshotStore tests (immutability + tamper detection)
# ---------------------------------------------------------------------------


def test_snapshot_store_immutability_and_tamper_detection(tmp_path: Path) -> None:
    store = SnapshotStore(sessions_dir=tmp_path / ".reqhq_sessions")
    payload = b"# Requirements\nStrict immutability test.\n"

    record = store.save_snapshot(
        session_id="sess-001",
        filename="../../Customer Spec (v1).md",
        content=payload,
        source_uri="file:///spec.md",
        source_type="local",
    )

    assert record.safe_filename == "Customer_Spec_v1_.md" or record.safe_filename.endswith(".md")
    assert record.snapshot_path.name.startswith(record.sha256[:12] + "_")
    assert record.snapshot_path.exists()
    assert record.manifest_path.exists()
    assert store.verify_snapshot(record) is True
    store.verify_snapshot_or_raise(record)

    # Idempotent re-save returns identical record
    record_again = store.save_snapshot(
        session_id="sess-001",
        filename="../../Customer Spec (v1).md",
        content=payload,
        source_uri="file:///spec.md",
    )
    assert record_again.sha256 == record.sha256
    assert len(store.list_snapshots("sess-001")) == 1
    assert store.list_snapshots("nonexistent-session") == []

    # File is marked read-only (immutable)
    mode = record.snapshot_path.stat().st_mode
    assert not (mode & stat.S_IWRITE)

    # Simulate tampering: make writable and alter 1 byte
    os.chmod(record.snapshot_path, stat.S_IREAD | stat.S_IWRITE)
    record.snapshot_path.write_bytes(payload + b"TAMPERED")
    assert store.verify_snapshot(record) is False
    with pytest.raises(SnapshotIntegrityError):
        store.verify_snapshot_or_raise(record)

    # Re-saving over tampered snapshot with same filename/prefix detects corruption
    with pytest.raises(SnapshotIntegrityError):
        store.save_snapshot(
            session_id="sess-001",
            filename="../../Customer Spec (v1).md",
            content=payload,
            source_uri="file:///spec.md",
        )

    # Restore exact content, then test corrupted/missing manifest detection
    record.snapshot_path.write_bytes(payload)
    assert store.verify_snapshot(record) is True
    record.manifest_path.write_text("{broken json", encoding="utf-8")
    assert store.verify_snapshot(record) is False
    record.manifest_path.unlink()
    assert store.verify_snapshot(record) is False

    # Missing snapshot file detection
    record.snapshot_path.unlink()
    assert store.verify_snapshot(record) is False


def test_snapshot_store_sanitization_and_save_document(tmp_path: Path) -> None:
    assert sanitize_filename("..") == "document.bin"
    assert sanitize_filename("///") == "document.bin"
    assert sanitize_filename("...") == "document.bin"

    store = SnapshotStore(sessions_dir=tmp_path / "sessions")
    with pytest.raises(ValueError):
        store.snapshots_dir("../escape")

    md = tmp_path / "doc.md"
    md.write_text("# Doc\nBody", encoding="utf-8")
    source_doc = LocalFileLoader().load_file(md)
    rec = store.save_document("session_ok", source_doc)
    assert rec.sha256 == source_doc.sha256
    assert store.verify_snapshot(rec) is True


# ---------------------------------------------------------------------------
# ParserRegistry & Normalizer tests
# ---------------------------------------------------------------------------


def test_normalizer_default_parsers_md_txt_pdf_docx(tmp_path: Path) -> None:
    md_path = tmp_path / "guide.md"
    md_path.write_bytes(b"# Overview\r\n[page:1]\r\nBody text.\r\n## Details\r\nMore.")
    txt_path = tmp_path / "plain.txt"
    txt_path.write_bytes(b"Line 1\r\nLine 2\r\n")
    pdf_path = _create_sample_pdf(
        tmp_path / "manual.pdf",
        ["First page requirements", "", "Third page appendix"],
    )
    docx_path = _create_sample_docx(tmp_path / "spec.docx")

    registry = ParserRegistry.default()
    active_names = registry.list_parsers()
    assert "plain_text_markdown" in active_names
    assert "pypdf" in active_names
    assert "python_docx" in active_names
    # Docling is optional: it must be registered exactly when the package is importable,
    # and the core parsers above must work either way.
    import importlib.util

    assert ("docling" in active_names) == (importlib.util.find_spec("docling") is not None)
    assert isinstance(PlainTextMarkdownParser(), DocumentParser)

    md_doc = normalize_file(md_path, registry=registry)
    assert md_doc.parser_name == "plain_text_markdown"
    assert "\r" not in md_doc.markdown
    assert any(a.tag == "[page:1]" for a in md_doc.anchors)
    assert any(a.tag == "[section:overview]" for a in md_doc.anchors)
    assert any(a.tag == "[section:details]" for a in md_doc.anchors)

    txt_doc = normalize_file(txt_path, registry=registry)
    assert txt_doc.parser_name == "plain_text_markdown"
    assert txt_doc.markdown == "Line 1\nLine 2\n"

    pdf_doc = normalize_file(pdf_path, registry=registry)
    assert pdf_doc.parser_name == "pypdf"
    assert pdf_doc.page_count == 3
    assert "[page:1]" in pdf_doc.markdown
    assert "[page:2]" in pdf_doc.markdown
    assert "[page:3]" in pdf_doc.markdown
    assert "First page requirements" in pdf_doc.markdown
    assert "Third page appendix" in pdf_doc.markdown

    docx_doc = normalize_file(docx_path, registry=registry)
    assert docx_doc.parser_name == "python_docx"
    assert "# Platform Specification" in docx_doc.markdown
    assert "# Functional Requirements" in docx_doc.markdown
    assert "## Details" in docx_doc.markdown
    assert "[section:details]" in docx_doc.markdown
    assert "[section:details-2]" in docx_doc.markdown
    assert "| Term | Definition |" in docx_doc.markdown
    assert r"Role-Based Access Control \| strict" in docx_doc.markdown


def test_normalizer_optional_adapters_and_errors(tmp_path: Path) -> None:
    pdf_path = _create_sample_pdf(tmp_path / "sample.pdf", ["PDF text"])

    # Unavailable DoclingParser raises RuntimeError if called directly
    unavailable_docling = DoclingParser()
    if not unavailable_docling.is_available():
        with pytest.raises(RuntimeError):
            unavailable_docling.parse(pdf_path, compute_sha256(pdf_path.read_bytes()))

    # Mocked PyMuPDF adapter dispatch
    class _FakeFitzPage:
        def __init__(self, text: str) -> None:
            self._text = text

        def get_text(self, mode: str) -> str:
            return self._text

    class _FakeFitzDoc:
        def __init__(self) -> None:
            self.closed = False
            self._pages = [_FakeFitzPage("Fast page 1"), _FakeFitzPage("")]

        def __iter__(self) -> Any:
            return iter(self._pages)

        def close(self) -> None:
            self.closed = True

    fake_fitz = SimpleNamespace(open=lambda _: _FakeFitzDoc())
    pymupdf_parser = PyMuPDFParser(fitz_module=fake_fitz)
    assert pymupdf_parser.is_available() is True

    # Mocked Docling adapter dispatch
    class _FakeDoclingConverter:
        def convert(self, path_str: str) -> Any:
            return SimpleNamespace(
                document=SimpleNamespace(
                    export_to_markdown=lambda: "# Docling Output\n[page:1]\nTable content"
                )
            )

    docling_parser = DoclingParser(converter_factory=_FakeDoclingConverter)
    assert docling_parser.is_available() is True

    reg = ParserRegistry.default()
    assert reg.register(pymupdf_parser, priority=200) is True
    assert reg.register(docling_parser, priority=210) is True

    # Highest priority wins (.pdf -> docling)
    doc_auto = normalize_file(pdf_path, registry=reg)
    assert doc_auto.parser_name == "docling"
    assert "Docling Output" in doc_auto.markdown

    # Explicit preferred_parser selects pymupdf or pypdf
    doc_fitz = reg.parse(pdf_path, preferred_parser="pymupdf")
    assert doc_fitz.parser_name == "pymupdf"
    assert doc_fitz.page_count == 2
    assert "[page:1]\nFast page 1" in doc_fitz.markdown
    assert "[page:2]" in doc_fitz.markdown

    doc_pypdf = reg.parse(pdf_path, preferred_parser="pypdf")
    assert doc_pypdf.parser_name == "pypdf"

    # Error handling for missing file and unsupported extension
    with pytest.raises(FileNotFoundError):
        normalize_file(tmp_path / "does_not_exist.md")
    unknown_file = tmp_path / "binary.xyz"
    unknown_file.write_bytes(b"xyz")
    with pytest.raises(UnsupportedFileTypeError):
        normalize_file(unknown_file)
    with pytest.raises(UnsupportedFileTypeError):
        PlainTextMarkdownParser().parse(pdf_path, compute_sha256(b"x"))

    assert slugify_heading("!!!") == "section"


# ---------------------------------------------------------------------------
# HierarchicalChunker tests (>150,000-character synthetic document, 0% truncation)
# ---------------------------------------------------------------------------


def test_hierarchical_chunker_large_document_zero_truncation_and_glossary(tmp_path: Path) -> None:
    large_md = _build_large_markdown_document(target_min_chars=160_000)
    assert len(large_md) > 150_000

    md_path = tmp_path / "large_spec.md"
    md_path.write_text(large_md, encoding="utf-8")
    normalized = normalize_file(md_path)

    chunker = HierarchicalChunker(max_chunk_chars=8000, overlap_chars=0)
    result = chunker.chunk(normalized)

    # Proven 0% truncation & exact lossless reconstruction
    assert result.total_chars == len(large_md)
    assert result.total_chars > 150_000
    assert result.covered_chars == result.total_chars
    assert result.truncation_ratio == 0.0
    assert result.chunk_count == len(result.chunks)
    assert result.chunk_count >= 20
    assert "".join(c.text for c in result.chunks) == large_md

    # Every chunk obeys size limit and contiguous character offsets
    expected_start = 0
    for idx, chunk in enumerate(result.chunks):
        assert chunk.chunk_index == idx
        assert chunk.chunk_id == f"chunk-{idx:04d}"
        assert chunk.char_count == len(chunk.text)
        assert chunk.char_count <= 8000
        assert chunk.start_char == expected_start
        assert chunk.end_char == expected_start + chunk.char_count
        assert chunk.source_sha256 == normalized.source_sha256
        expected_start = chunk.end_char

    # Heading hierarchy preserved
    assert any(
        "Enterprise Specification Document" in c.heading_path
        and any(h.startswith("Domain Module 1") for h in c.heading_path)
        for c in result.chunks
    )
    assert any(
        any(h.startswith("Requirements for Module 1") for h in c.heading_path)
        for c in result.chunks
    )

    # Page & section anchors preserved and inherited across chunks
    chunks_with_anchors = [c for c in result.chunks if c.anchors]
    assert len(chunks_with_anchors) >= result.chunk_count - 1  # only preamble has no anchor
    assert any("[page:1]" in c.anchors for c in result.chunks)
    assert any("[section:domain-module-1]" in c.anchors for c in result.chunks)

    # Complete GlobalGlossary extraction
    glossary = result.glossary
    assert glossary.acronyms["RBAC"] == "Role-Based Access Control"
    assert glossary.acronyms["SLA"] == "Service Level Agreement"
    assert glossary.acronyms["PBT"] == "Property-Based Testing"
    assert "Idempotency Key" in glossary.defined_terms
    assert "Circuit Breaker" in glossary.defined_terms
    assert "Tenant Isolation" in glossary.defined_terms
    assert "Audit Trail" in glossary.defined_terms
    assert "Cold Start" in glossary.defined_terms
    assert "OrderService" in glossary.key_entities
    assert "PaymentGateway" in glossary.key_entities
    assert "Enterprise Specification Document" in glossary.section_titles
    assert "Glossary and Definitions" in glossary.section_titles
    assert len(glossary.entries) > 10

    prompt_header = glossary.to_prompt_header()
    assert "## Global Glossary" in prompt_header
    assert "RBAC: Role-Based Access Control" in prompt_header
    assert "OrderService" in prompt_header


def test_hierarchical_chunker_overlap_edge_cases_and_validation(tmp_path: Path) -> None:
    # Validation errors
    with pytest.raises(ValueError):
        HierarchicalChunker(max_chunk_chars=0)
    with pytest.raises(ValueError):
        HierarchicalChunker(max_chunk_chars=100, overlap_chars=-1)
    with pytest.raises(ValueError):
        HierarchicalChunker(max_chunk_chars=100, overlap_chars=100)

    # Empty document
    empty_path = tmp_path / "empty.md"
    empty_path.write_text("", encoding="utf-8")
    empty_res = HierarchicalChunker().chunk(normalize_file(empty_path))
    assert empty_res.total_chars == 0
    assert empty_res.covered_chars == 0
    assert empty_res.truncation_ratio == 0.0
    assert empty_res.chunks == []

    # Overlap mode + sentence/word/hard split boundaries
    text = (
        "# Alpha\n[page:1]\n"
        + ("Short sentence number one. " * 20)
        + "\n"
        + ("word " * 80)
        + "\n"
        + ("Z" * 350)
    )
    p = tmp_path / "overlap.md"
    p.write_text(text, encoding="utf-8")
    doc = normalize_file(p)

    chunker = HierarchicalChunker(max_chunk_chars=200, overlap_chars=40)
    res = chunker.chunk(doc)
    assert res.truncation_ratio == 0.0
    assert res.covered_chars == len(text)
    assert res.chunk_count >= 5
    for c in res.chunks:
        assert c.char_count <= 200
    # Verify actual overlap exists between consecutive chunks
    assert res.chunks[1].start_char < res.chunks[0].end_char
    assert res.chunks[0].end_char - res.chunks[1].start_char == 40


def test_end_to_end_local_snapshot_normalize_chunk(tmp_path: Path) -> None:
    docx_path = _create_sample_docx(tmp_path / "intake.docx")
    source_doc = LocalFileLoader().load_file(docx_path)

    store = SnapshotStore(sessions_dir=tmp_path / "sessions")
    snap = store.save_document("e2e-session", source_doc)
    assert store.verify_snapshot(snap) is True

    normalized = normalize_file(snap.snapshot_path, source_sha256=snap.sha256)
    assert normalized.source_sha256 == snap.sha256
    assert normalized.parser_name == "python_docx"

    chunked = HierarchicalChunker(max_chunk_chars=500).chunk(normalized)
    assert chunked.truncation_ratio == 0.0
    assert chunked.glossary.acronyms.get("SLA") == "Service Level Agreement"


# ---------------------------------------------------------------------------
# Hardened edge-case, pagination, fallback, and persistence tests
# ---------------------------------------------------------------------------


def test_gdrive_pagination_mime_inference_and_oauth_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_bytes = _create_sample_pdf(tmp_path / "extless.pdf", ["Extensionless PDF"]).read_bytes()

    class _PagedFilesResource:
        def __init__(self) -> None:
            self.list_calls: list[dict[str, Any]] = []

        def list(self, **kwargs: Any) -> _FakeDriveRequest:
            self.list_calls.append(kwargs)
            if kwargs.get("pageToken") == "page-2":
                return _FakeDriveRequest(
                    {"files": [{"id": "extless-1", "name": "SpecWithoutExt", "mimeType": "application/pdf"}]}
                )
            return _FakeDriveRequest(
                {
                    "files": [{"id": "doc-1", "name": "Overview.md", "mimeType": "text/markdown"}],
                    "nextPageToken": "page-2",
                }
            )

        def get(self, **kwargs: Any) -> _FakeDriveRequest:
            if kwargs["fileId"] == "extless-1":
                return _FakeDriveRequest(
                    {"id": "extless-1", "name": "SpecWithoutExt", "mimeType": "application/pdf"}
                )
            return _FakeDriveRequest(
                {"id": "doc-1", "name": "Overview.md", "mimeType": "text/markdown"}
            )

        def get_media(self, **kwargs: Any) -> _FakeDriveRequest:
            if kwargs["fileId"] == "extless-1":
                return _FakeDriveRequest(pdf_bytes)
            return _FakeDriveRequest(b"# Overview\nBody")

    paged_res = _PagedFilesResource()
    connector = GDriveConnector(service=_FakeDriveService(paged_res))

    # Escapes single quotes and follows nextPageToken pagination
    items = connector.list_folder("folder'with'quote")
    assert [i["id"] for i in items] == ["doc-1", "extless-1"]
    assert "\\'" in paged_res.list_calls[0]["q"]
    assert paged_res.list_calls[1]["pageToken"] == "page-2"

    # Extension inferred from MIME type and appended to filename
    pdf_doc = connector.fetch_file("extless-1")
    assert pdf_doc.filename == "SpecWithoutExt.pdf"
    assert pdf_doc.extension == ".pdf"
    assert pdf_doc.media_type == "application/pdf"

    # Offline mocked OAuth2 token refresh + InstalledAppFlow path
    import google.oauth2.credentials as g_creds
    import google_auth_oauthlib.flow as g_flow
    import googleapiclient.discovery as g_disc

    token_file = tmp_path / "tok" / "token.json"
    secrets_file = tmp_path / "credentials.json"
    secrets_file.write_text("{}", encoding="utf-8")

    class _FakeCreds:
        def __init__(self, valid: bool, expired: bool, refresh_token: str | None) -> None:
            self.valid = valid
            self.expired = expired
            self.refresh_token = refresh_token
            self.refreshed = False

        def refresh(self, req: Any) -> None:
            self.refreshed = True
            self.valid = True

        def to_json(self) -> str:
            return '{"token":"mock"}'

    built_services: list[tuple[str, str, Any]] = []

    def _fake_build(api: str, version: str, credentials: Any) -> Any:
        built_services.append((api, version, credentials))
        return _FakeDriveService(paged_res)

    monkeypatch.setattr(g_disc, "build", _fake_build)

    # Case 1: Fresh flow from client_secrets_path writes token_path
    class _FakeFlow:
        def run_local_server(self, port: int = 0) -> _FakeCreds:
            return _FakeCreds(valid=True, expired=False, refresh_token="rt")

    monkeypatch.setattr(
        g_flow.InstalledAppFlow,
        "from_client_secrets_file",
        staticmethod(lambda path, scopes: _FakeFlow()),
    )
    conn_fresh = GDriveConnector(client_secrets_path=secrets_file, token_path=token_file)
    svc1 = conn_fresh._get_service()
    assert svc1 is not None
    assert token_file.exists()

    # Case 2: Expired token refreshed via refresh_token
    expired_creds = _FakeCreds(valid=False, expired=True, refresh_token="rt-1")
    monkeypatch.setattr(
        g_creds.Credentials,
        "from_authorized_user_file",
        staticmethod(lambda path, scopes: expired_creds),
    )
    conn_refresh = GDriveConnector(client_secrets_path=secrets_file, token_path=token_file)
    conn_refresh._get_service()
    assert expired_creds.refreshed is True


def test_sharepoint_pagination_readonly_method_guard_and_mime_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/me/drive/root/children"):
            return httpx.Response(
                200,
                json={
                    "value": [{"id": "p1", "name": "a.md"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/drive/root/children?$skiptoken=2",
                },
            )
        if "$skiptoken=2" in url:
            return httpx.Response(
                200,
                json={"value": [{"id": "p2", "name": "NoExtDoc"}]},
            )
        if url.endswith("/me/drive/items/p2"):
            return httpx.Response(
                200,
                json={
                    "id": "p2",
                    "name": "NoExtDoc",
                    "file": {"mimeType": "text/markdown"},
                },
            )
        if url.endswith("/me/drive/items/p2/content"):
            return httpx.Response(200, content=b"# Extensionless SharePoint MD")
        return httpx.Response(404)

    msal_app = _FakeMsalApp(
        accounts=[{"username": "u@contoso.com"}],
        silent_result={"access_token": "tok-1"},
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        conn = SharePointConnector(msal_app=msal_app, http_client=client)
        children = conn.list_drive_children()
        assert [c["id"] for c in children] == ["p1", "p2"]

        doc = conn.fetch_drive_item("p2")
        assert doc.filename == "NoExtDoc.md"
        assert doc.extension == ".md"

        # Non-GET HTTP methods are blocked at transport layer
        for forbidden_method in ("POST", "PUT", "PATCH", "DELETE"):
            with pytest.raises(ScopeViolationError):
                conn._request_with_client(forbidden_method, "https://graph.microsoft.com/v1.0/me", {})
    finally:
        client.close()

    # Exercise _get_msal_app() with monkeypatched msal.PublicClientApplication
    import msal

    captured: dict[str, str] = {}

    class _MockPCA:
        def __init__(self, *, client_id: str, authority: str) -> None:
            captured["client_id"] = client_id
            captured["authority"] = authority

    monkeypatch.setattr(msal, "PublicClientApplication", _MockPCA)
    conn_pca = SharePointConnector(client_id="cid-123", tenant_id="tenant-xyz")
    app = conn_pca._get_msal_app()
    assert isinstance(app, _MockPCA)
    assert captured == {
        "client_id": "cid-123",
        "authority": "https://login.microsoftonline.com/tenant-xyz",
    }


def test_snapshot_same_length_tamper_corrupted_manifest_and_filename_property(
    tmp_path: Path,
) -> None:
    assert sanitize_filename(r"..\..\win\path\Spec.pdf") == "Spec.pdf"

    store = SnapshotStore(sessions_dir=tmp_path / ".reqhq_sessions")
    payload = b"ABCDEFGH"
    rec = store.save_snapshot(
        session_id="sess-same-len",
        filename="report.txt",
        content=payload,
        source_uri="file:///report.txt",
    )
    # CLI compatibility property works without adding extra Pydantic schema fields
    assert rec.filename == "report.txt"
    assert "filename" not in rec.model_dump()

    # Flip 1 byte while keeping exact same file length -> triggers SHA-256 mismatch
    os.chmod(rec.snapshot_path, stat.S_IREAD | stat.S_IWRITE)
    rec.snapshot_path.write_bytes(b"XBCDEFGH")
    assert len(rec.snapshot_path.read_bytes()) == rec.size_bytes
    assert store.verify_snapshot(rec) is False

    # Restore exact bytes, corrupt manifest JSON, re-save must raise SnapshotIntegrityError
    rec.snapshot_path.write_bytes(payload)
    rec.manifest_path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(SnapshotIntegrityError):
        store.save_snapshot(
            session_id="sess-same-len",
            filename="report.txt",
            content=payload,
            source_uri="file:///report.txt",
        )

    # Valid JSON manifest but wrong SHA-256 -> re-save must also raise SnapshotIntegrityError
    bad_manifest = rec.model_copy(update={"sha256": "0" * 64})
    rec.manifest_path.write_text(bad_manifest.model_dump_json(indent=2), encoding="utf-8")
    with pytest.raises(SnapshotIntegrityError):
        store.save_snapshot(
            session_id="sess-same-len",
            filename="report.txt",
            content=payload,
            source_uri="file:///report.txt",
        )


def test_normalizer_python315_fallback_fences_and_adapter_guards(tmp_path: Path) -> None:
    pdf_path = _create_sample_pdf(tmp_path / "fallback.pdf", ["Fallback PDF text"])
    docx_path = _create_sample_docx(tmp_path / "fallback.docx")
    md_path = tmp_path / "fenced.md"
    md_path.write_text(
        "# Real Heading\n"
        "[section:real-heading]\n"
        "```python\n"
        "# Not A Heading Inside Fence\n"
        "x = 1\n"
        "```\n"
        "## Second Heading\n"
        "Body text.\n",
        encoding="utf-8",
    )

    # On Python 3.15 without fitz/docling, preferred_parser gracefully falls back to pypdf/python-docx
    reg = ParserRegistry.default()
    if not PyMuPDFParser().is_available():
        with pytest.raises(RuntimeError):
            PyMuPDFParser().parse(pdf_path, compute_sha256(pdf_path.read_bytes()))
        pdf_fallback = normalize_file(pdf_path, registry=reg, preferred_parser="pymupdf")
        assert pdf_fallback.parser_name == "pypdf"
        assert "[page:1]" in pdf_fallback.markdown

    if not DoclingParser().is_available():
        docx_fallback = normalize_file(docx_path, registry=reg, preferred_parser="docling")
        assert docx_fallback.parser_name == "python_docx"

    # Extension mismatch guards on adapters
    sha = compute_sha256(b"x")
    with pytest.raises(UnsupportedFileTypeError):
        PyPdfParser().parse(md_path, sha)
    with pytest.raises(UnsupportedFileTypeError):
        PythonDocxParser().parse(md_path, sha)
    fake_fitz = SimpleNamespace(open=lambda _: None)
    with pytest.raises(UnsupportedFileTypeError):
        PyMuPDFParser(fitz_module=fake_fitz).parse(md_path, sha)
    with pytest.raises(UnsupportedFileTypeError):
        DoclingParser(converter_factory=lambda: None).parse(md_path, sha)

    # Empty paragraph and zero-row table handling in PythonDocxParser
    assert PythonDocxParser._render_table(SimpleNamespace(rows=[])) == ""
    blank_docx = DocxDocument()
    blank_docx.add_paragraph("   ")
    blank_docx.add_paragraph("Actual paragraph.")
    blank_docx_path = tmp_path / "blank_para.docx"
    blank_docx.save(str(blank_docx_path))
    parsed_blank = normalize_file(blank_docx_path)
    assert parsed_blank.markdown == "Actual paragraph."

    # Fenced code block comments are ignored as headings and explicit [section:...] is not duplicated
    md_doc = normalize_file(md_path)
    section_tags = [a.tag for a in md_doc.anchors if a.kind == "section"]
    assert section_tags == ["[section:real-heading]", "[section:second-heading]"]


def test_chunker_fences_anchor_inheritance_glossary_persistence_and_helpers(
    tmp_path: Path,
) -> None:
    from reqhq.ingestion.chunker import _merge_covered_length, _trim_acronym_expansion

    # Helper edge cases
    assert _merge_covered_length([]) == 0
    assert _merge_covered_length([(0, 5), (10, 15), (12, 20)]) == 15
    assert _trim_acronym_expansion("Anything", "A") is None
    assert _trim_acronym_expansion("   ", "AB") is None
    assert (
        _trim_acronym_expansion("the Department of Homeland Security", "DHS")
        == "Department of Homeland Security"
    )
    assert _trim_acronym_expansion("unrelated phrase here", "XYZ") is None
    assert HierarchicalChunker._partition_by_headings("") == []

    # Fenced code blocks do not split sections, and heading-derived anchors propagate
    md_text = (
        "# Architecture\n"
        "[page:1]\n"
        "Department of Homeland Security (DHS) compliance rules apply.\n"
        "```bash\n"
        "# Fake heading inside bash block\n"
        "echo hello\n"
        "```\n\n"
        "## Billing\n"
        "[section:billing]\n"
        "[page:2]\n"
        "Billing prose line.\n"
    )
    md_path = tmp_path / "arch.md"
    md_path.write_text(md_text, encoding="utf-8")
    doc = normalize_file(md_path)
    res = HierarchicalChunker(max_chunk_chars=8000).chunk(doc)

    assert res.truncation_ratio == 0.0
    assert len(res.chunks) == 2
    assert res.chunks[0].heading_path == ["Architecture"]
    assert "[section:architecture]" in res.chunks[0].anchors
    assert "[page:1]" in res.chunks[0].anchors
    # Chunk 1 starts cleanly on [section:billing] and [page:2] -> no spurious [page:1] leakage
    assert res.chunks[1].heading_path == ["Architecture", "Billing"]
    assert res.chunks[1].anchors == ["[section:billing]", "[page:2]"]
    assert res.glossary.acronyms["DHS"] == "Department of Homeland Security"
    assert "Fake heading inside bash block" not in res.glossary.section_titles

    # GlobalGlossary persistence (save/load) and merge
    glossary_path = tmp_path / "glossary" / "global_glossary.json"
    res.glossary.save(glossary_path)
    loaded = GlobalGlossary.load(glossary_path)
    assert loaded == res.glossary

    other = GlobalGlossary(
        defined_terms={"Zero Trust": "Never trust, always verify"},
        acronyms={"DHS": "Should Not Overwrite", "SOC": "Security Operations Center"},
        key_entities=["BillingEngine"],
        section_titles=["Billing", "Security"],
        entries=[
            GlossaryEntry(term="SOC", definition="Security Operations Center", category="acronym")
        ],
    )
    merged = loaded.merge(other)
    assert merged.acronyms["DHS"] == "Department of Homeland Security"
    assert merged.acronyms["SOC"] == "Security Operations Center"
    assert merged.defined_terms["Zero Trust"] == "Never trust, always verify"
    assert "BillingEngine" in merged.key_entities
    assert merged.section_titles.count("Billing") == 1
    assert "Security" in merged.section_titles
