"""Deterministic Markdown normalization with pluggable parser adapters."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from reqhq.ingestion.connectors import (
    UnsupportedFileTypeError,
    compute_sha256,
    infer_media_type,
)

_ANCHOR_RE = re.compile(r"\[(page|section):([^\]]+)\]")
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def slugify_heading(text: str) -> str:
    """Convert heading text into a deterministic URL/anchor slug."""
    slug = _SLUG_NON_ALNUM_RE.sub("-", text.strip().lower()).strip("-")
    return slug or "section"


def extract_anchors(markdown: str) -> list[DocumentAnchor]:
    """Extract all ``[page:N]`` and ``[section:slug]`` anchors with character offsets."""
    anchors: list[DocumentAnchor] = []
    for match in _ANCHOR_RE.finditer(markdown):
        kind = match.group(1)
        value = match.group(2).strip()
        if kind in ("page", "section"):
            anchors.append(
                DocumentAnchor(
                    kind=kind,  # type: ignore[arg-type]
                    value=value,
                    tag=match.group(0),
                    char_offset=match.start(),
                )
            )
    return anchors


class DocumentAnchor(BaseModel):
    """Location anchor embedded in normalized Markdown."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["page", "section"]
    value: str = Field(..., min_length=1)
    tag: str = Field(..., min_length=1)
    char_offset: int = Field(..., ge=0)


class NormalizedDocument(BaseModel):
    """Deterministic Markdown representation of an ingested source document."""

    model_config = ConfigDict(extra="forbid")

    source_path: Path
    source_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    parser_name: str = Field(..., min_length=1)
    media_type: str = Field(..., min_length=1)
    markdown: str
    char_count: int = Field(..., ge=0)
    page_count: int | None = Field(default=None, ge=0)
    anchors: list[DocumentAnchor] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class DocumentParser(Protocol):
    """Pluggable parser interface for deterministic document normalization."""

    name: str
    supported_extensions: frozenset[str]

    def is_available(self) -> bool:
        """Return True if this parser's runtime dependencies are available."""
        ...

    def parse(self, path: Path, source_sha256: str) -> NormalizedDocument:
        """Normalize ``path`` into a ``NormalizedDocument``."""
        ...


class PlainTextMarkdownParser:
    """Python 3.15-safe parser for ``.md`` and ``.txt`` documents."""

    name: str = "plain_text_markdown"
    supported_extensions: frozenset[str] = frozenset({".md", ".txt"})

    def is_available(self) -> bool:
        return True

    def parse(self, path: Path, source_sha256: str) -> NormalizedDocument:
        ext = path.suffix.lower()
        if ext not in self.supported_extensions:
            raise UnsupportedFileTypeError(
                f"{self.name} does not support extension {ext!r}"
            )
        raw_text = path.read_bytes().decode("utf-8", errors="replace")
        normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")

        # Collect explicit anchors plus heading-derived section anchors (ignoring code fences).
        anchors = extract_anchors(normalized)
        seen_section_values = {a.value for a in anchors if a.kind == "section"}
        slug_counts: dict[str, int] = {}
        in_fence = False
        fence_char = ""
        offset = 0
        for line in normalized.splitlines(keepends=True):
            stripped = line.rstrip("\n")
            fence_match = _FENCE_RE.match(stripped)
            if fence_match:
                marker = fence_match.group(1)[0]
                if not in_fence:
                    in_fence = True
                    fence_char = marker
                elif marker == fence_char:
                    in_fence = False
                    fence_char = ""
                offset += len(line)
                continue
            if not in_fence:
                match = _MD_HEADING_RE.match(stripped)
                if match:
                    base_slug = slugify_heading(match.group(2))
                    count = slug_counts.get(base_slug, 0) + 1
                    slug_counts[base_slug] = count
                    slug = base_slug if count == 1 else f"{base_slug}-{count}"
                    if slug not in seen_section_values:
                        seen_section_values.add(slug)
                        anchors.append(
                            DocumentAnchor(
                                kind="section",
                                value=slug,
                                tag=f"[section:{slug}]",
                                char_offset=offset,
                            )
                        )
            offset += len(line)
        anchors.sort(key=lambda a: a.char_offset)

        return NormalizedDocument(
            source_path=path,
            source_sha256=source_sha256,
            parser_name=self.name,
            media_type=infer_media_type(ext),
            markdown=normalized,
            char_count=len(normalized),
            page_count=None,
            anchors=anchors,
            metadata={"line_count": normalized.count("\n") + (1 if normalized else 0)},
        )


class PyPdfParser:
    """Default pure-Python PDF parser backed by ``pypdf``, preserving ``[page:N]`` anchors."""

    name: str = "pypdf"
    supported_extensions: frozenset[str] = frozenset({".pdf"})

    def is_available(self) -> bool:
        return importlib.util.find_spec("pypdf") is not None

    def parse(self, path: Path, source_sha256: str) -> NormalizedDocument:
        ext = path.suffix.lower()
        if ext not in self.supported_extensions:
            raise UnsupportedFileTypeError(
                f"{self.name} does not support extension {ext!r}"
            )
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        page_blocks: list[str] = []
        for idx, page in enumerate(reader.pages, start=1):
            extracted = (page.extract_text() or "").replace("\r\n", "\n").replace("\r", "\n").strip()
            if extracted:
                page_blocks.append(f"[page:{idx}]\n{extracted}")
            else:
                page_blocks.append(f"[page:{idx}]")

        markdown = "\n\n".join(page_blocks)
        anchors = extract_anchors(markdown)
        return NormalizedDocument(
            source_path=path,
            source_sha256=source_sha256,
            parser_name=self.name,
            media_type=infer_media_type(".pdf"),
            markdown=markdown,
            char_count=len(markdown),
            page_count=len(reader.pages),
            anchors=anchors,
            metadata={"backend": "pypdf"},
        )


class PythonDocxParser:
    """Default Python 3.15-safe DOCX parser preserving headings, tables, and ``[section:slug]`` anchors."""

    name: str = "python_docx"
    supported_extensions: frozenset[str] = frozenset({".docx"})

    def is_available(self) -> bool:
        return importlib.util.find_spec("docx") is not None

    @staticmethod
    def _heading_level(style_name: str) -> int | None:
        normalized = style_name.strip().lower()
        if normalized == "title":
            return 1
        if normalized.startswith("heading"):
            suffix = normalized.replace("heading", "", 1).strip()
            if suffix.isdigit():
                level = int(suffix)
                return max(1, min(6, level))
        return None

    @staticmethod
    def _render_table(table: Any) -> str:
        rows: list[list[str]] = []
        for row in table.rows:
            cells = [cell.text.replace("\n", " ").strip().replace("|", r"\|") for cell in row.cells]
            rows.append(cells)
        if not rows:
            return ""
        col_count = max(len(r) for r in rows)
        padded = [r + [""] * (col_count - len(r)) for r in rows]
        header = "| " + " | ".join(padded[0]) + " |"
        separator = "| " + " | ".join("---" for _ in range(col_count)) + " |"
        body = ["| " + " | ".join(r) + " |" for r in padded[1:]]
        return "\n".join([header, separator, *body])

    def parse(self, path: Path, source_sha256: str) -> NormalizedDocument:
        ext = path.suffix.lower()
        if ext not in self.supported_extensions:
            raise UnsupportedFileTypeError(
                f"{self.name} does not support extension {ext!r}"
            )
        from docx import Document
        from docx.oxml.table import CT_Tbl
        from docx.oxml.text.paragraph import CT_P
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        doc = Document(str(path))
        blocks: list[str] = []
        slug_counts: dict[str, int] = {}
        heading_count = 0
        table_count = 0

        for child in doc.element.body.iterchildren():
            if isinstance(child, CT_P):
                paragraph = Paragraph(child, doc)
                text = paragraph.text.strip()
                if not text:
                    continue
                style_name = paragraph.style.name if paragraph.style is not None else ""
                level = self._heading_level(style_name)
                if level is not None:
                    heading_count += 1
                    base_slug = slugify_heading(text)
                    count = slug_counts.get(base_slug, 0) + 1
                    slug_counts[base_slug] = count
                    slug = base_slug if count == 1 else f"{base_slug}-{count}"
                    blocks.append(f"{'#' * level} {text}\n[section:{slug}]")
                else:
                    blocks.append(text)
            elif isinstance(child, CT_Tbl):
                table = Table(child, doc)
                rendered = self._render_table(table)
                if rendered:
                    table_count += 1
                    blocks.append(rendered)

        markdown = "\n\n".join(blocks)
        anchors = extract_anchors(markdown)
        return NormalizedDocument(
            source_path=path,
            source_sha256=source_sha256,
            parser_name=self.name,
            media_type=infer_media_type(".docx"),
            markdown=markdown,
            char_count=len(markdown),
            page_count=None,
            anchors=anchors,
            metadata={
                "backend": "python-docx",
                "heading_count": heading_count,
                "table_count": table_count,
            },
        )


class PyMuPDFParser:
    """Optional high-speed PDF parser adapter activated only when ``fitz`` is installed."""

    name: str = "pymupdf"
    supported_extensions: frozenset[str] = frozenset({".pdf"})

    def __init__(self, *, fitz_module: Any | None = None) -> None:
        self._fitz_module = fitz_module

    def is_available(self) -> bool:
        if self._fitz_module is not None:
            return True
        return importlib.util.find_spec("fitz") is not None

    def parse(self, path: Path, source_sha256: str) -> NormalizedDocument:
        if not self.is_available():
            raise RuntimeError("PyMuPDFParser is unavailable because 'fitz' is not installed.")
        ext = path.suffix.lower()
        if ext not in self.supported_extensions:
            raise UnsupportedFileTypeError(
                f"{self.name} does not support extension {ext!r}"
            )
        fitz = self._fitz_module
        if fitz is None:  # pragma: no cover - exercised only when pymupdf is installed
            import fitz as _fitz

            fitz = _fitz

        pdf_doc = fitz.open(str(path))
        try:
            page_blocks: list[str] = []
            for idx, page in enumerate(pdf_doc, start=1):
                extracted = str(page.get_text("text") or "").replace("\r\n", "\n").strip()
                if extracted:
                    page_blocks.append(f"[page:{idx}]\n{extracted}")
                else:
                    page_blocks.append(f"[page:{idx}]")
            page_count = len(page_blocks)
        finally:
            if hasattr(pdf_doc, "close"):
                pdf_doc.close()

        markdown = "\n\n".join(page_blocks)
        return NormalizedDocument(
            source_path=path,
            source_sha256=source_sha256,
            parser_name=self.name,
            media_type=infer_media_type(".pdf"),
            markdown=markdown,
            char_count=len(markdown),
            page_count=page_count,
            anchors=extract_anchors(markdown),
            metadata={"backend": "pymupdf"},
        )


class DoclingParser:
    """Optional layout-aware parser adapter activated only when ``docling`` is installed."""

    name: str = "docling"
    supported_extensions: frozenset[str] = frozenset({".pdf", ".docx"})

    def __init__(self, *, converter_factory: Any | None = None) -> None:
        self._converter_factory = converter_factory

    def is_available(self) -> bool:
        if self._converter_factory is not None:
            return True
        return importlib.util.find_spec("docling") is not None

    def parse(self, path: Path, source_sha256: str) -> NormalizedDocument:
        if not self.is_available():
            raise RuntimeError("DoclingParser is unavailable because 'docling' is not installed.")
        ext = path.suffix.lower()
        if ext not in self.supported_extensions:
            raise UnsupportedFileTypeError(
                f"{self.name} does not support extension {ext!r}"
            )

        factory = self._converter_factory
        if factory is None:  # pragma: no cover - exercised only when docling is installed
            from docling.document_converter import DocumentConverter

            factory = DocumentConverter

        converter = factory()
        result = converter.convert(str(path))
        markdown = str(result.document.export_to_markdown()).replace("\r\n", "\n")
        ext = path.suffix.lower()
        return NormalizedDocument(
            source_path=path,
            source_sha256=source_sha256,
            parser_name=self.name,
            media_type=infer_media_type(ext),
            markdown=markdown,
            char_count=len(markdown),
            page_count=None,
            anchors=extract_anchors(markdown),
            metadata={"backend": "docling"},
        )


class ParserRegistry:
    """Registry that dispatches file extensions to available ``DocumentParser`` adapters."""

    def __init__(self) -> None:
        self._entries: list[tuple[int, DocumentParser]] = []

    def register(self, parser: DocumentParser, *, priority: int = 100) -> bool:
        """Register ``parser`` if available; higher priority wins dispatch."""
        if not parser.is_available():
            return False
        self._entries = [
            (prio, existing)
            for prio, existing in self._entries
            if existing.name != parser.name
        ]
        self._entries.append((priority, parser))
        self._entries.sort(key=lambda item: (-item[0], item[1].name))
        return True

    def list_parsers(self) -> list[str]:
        """Return names of currently active/registered parsers in priority order."""
        return [parser.name for _, parser in self._entries]

    def get_parser(
        self,
        extension: str,
        *,
        preferred_parser: str | None = None,
    ) -> DocumentParser:
        """Resolve the highest-priority available parser for ``extension``."""
        ext = extension.lower()
        candidates = [
            parser
            for _, parser in self._entries
            if ext in parser.supported_extensions and parser.is_available()
        ]
        if not candidates:
            raise UnsupportedFileTypeError(
                f"No available parser registered for extension {ext!r}."
            )
        if preferred_parser is not None:
            for candidate in candidates:
                if candidate.name == preferred_parser:
                    return candidate
        return candidates[0]

    def parse(
        self,
        path: Path | str,
        source_sha256: str | None = None,
        *,
        preferred_parser: str | None = None,
    ) -> NormalizedDocument:
        """Parse ``path`` using the selected registered parser."""
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found for normalization: {file_path}")
        digest = source_sha256 or compute_sha256(file_path.read_bytes())
        parser = self.get_parser(file_path.suffix, preferred_parser=preferred_parser)
        return parser.parse(file_path, digest)

    @classmethod
    def default(cls, *, prefer_optional: bool = True) -> ParserRegistry:
        """Construct the default Phase 1 registry with Python 3.15-safe defaults.

        Default parsers (`PlainTextMarkdownParser`, `PyPdfParser`, `PythonDocxParser`)
        are always registered. Optional adapters (`PyMuPDFParser`, `DoclingParser`)
        activate dynamically only if their packages are installed.
        """
        registry = cls()
        registry.register(PlainTextMarkdownParser(), priority=100)
        registry.register(PyPdfParser(), priority=100)
        registry.register(PythonDocxParser(), priority=100)

        optional_priority = 200 if prefer_optional else 50
        registry.register(PyMuPDFParser(), priority=optional_priority)
        registry.register(DoclingParser(), priority=optional_priority + 10)
        return registry


def normalize_file(
    path: Path | str,
    source_sha256: str | None = None,
    registry: ParserRegistry | None = None,
    *,
    preferred_parser: str | None = None,
) -> NormalizedDocument:
    """Normalize a local or snapshotted file into a deterministic ``NormalizedDocument``."""
    active_registry = registry if registry is not None else ParserRegistry.default()
    return active_registry.parse(
        Path(path),
        source_sha256=source_sha256,
        preferred_parser=preferred_parser,
    )
