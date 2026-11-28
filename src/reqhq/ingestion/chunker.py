"""Heading-aware hierarchical chunker with Global Glossary extraction and 0% truncation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from reqhq.ingestion.normalizer import NormalizedDocument

_HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_ANCHOR_TAG_RE = re.compile(r"\[(?:page|section):[^\]]+\]")

# Glossary extraction patterns
_ACRONYM_AFTER_FULL_RE = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9-]*(?:\s+[A-Za-z0-9-]+){0,8})\s*\(([A-Z][A-Z0-9-]{1,10})\)"
)
_ACRONYM_BEFORE_FULL_RE = re.compile(
    r"\b([A-Z][A-Z0-9-]{1,10})\s*\(([A-Z][A-Za-z0-9-]+(?:\s+[A-Za-z0-9-]+){1,6})\)"
)
_ACRONYM_CONNECTORS = frozenset({"of", "for", "and", "the", "in", "to", "on", "with", "by", "a", "an"})


def _trim_acronym_expansion(candidate: str, acronym: str) -> str | None:
    """Align the trailing phrase of ``candidate`` to the initials of ``acronym``."""
    letters = [ch.lower() for ch in acronym if ch.isalnum()]
    if len(letters) < 2:
        return None
    words = candidate.strip().split()
    if not words:
        return None

    letter_idx = len(letters) - 1
    start_word_idx: int | None = None

    for w_idx in range(len(words) - 1, -1, -1):
        word = words[w_idx]
        subparts = [p for p in word.split("-") if p]
        sub_initials = [p[0].lower() for p in subparts if p[0].isalnum()]

        # Match all hyphenated sub-initials from right to left if they align.
        if (
            sub_initials
            and letter_idx - len(sub_initials) + 1 >= 0
            and letters[letter_idx - len(sub_initials) + 1 : letter_idx + 1] == sub_initials
        ):
            letter_idx -= len(sub_initials)
            start_word_idx = w_idx
        elif sub_initials and letters[letter_idx] == sub_initials[0]:
            letter_idx -= 1
            start_word_idx = w_idx
        elif word.lower() in _ACRONYM_CONNECTORS and start_word_idx is not None:
            continue
        else:
            break

        if letter_idx < 0:
            return " ".join(words[start_word_idx:])

    return None
_BOLD_DEF_RE = re.compile(
    r"^\s*(?:[-*]\s+)?\*\*([^*]{2,80})\*\*\s*(?::|—|--|-|\bis defined as\b|\bmeans\b)\s*(.+?)\s*$",
    re.IGNORECASE,
)
_TABLE_DEF_RE = re.compile(r"^\s*\|\s*([^|]{2,80}?)\s*\|\s*([^|]{4,300}?)\s*\|\s*$")
_QUOTED_MEANS_RE = re.compile(
    r'["\']([A-Za-z][A-Za-z0-9 _-]{1,60})["\']\s+(?:means|refers to|is defined as)\s+([^.\n]{5,240}\.?)'
)
_PASCAL_ENTITY_RE = re.compile(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b")
_STOP_ENTITIES = frozenset({"Markdown", "GitHub", "OneDrive", "SharePoint", "PyMuPDF"})


class GlossaryEntry(BaseModel):
    """Single extracted term, acronym, entity, or section reference."""

    model_config = ConfigDict(extra="forbid")

    term: str = Field(..., min_length=1)
    definition: str = Field(default="")
    category: Literal["acronym", "defined_term", "entity", "section"]
    first_anchor: str | None = None


class GlobalGlossary(BaseModel):
    """Persistent document-wide glossary preserved across all chunks."""

    model_config = ConfigDict(extra="forbid")

    defined_terms: dict[str, str] = Field(default_factory=dict)
    acronyms: dict[str, str] = Field(default_factory=dict)
    key_entities: list[str] = Field(default_factory=list)
    section_titles: list[str] = Field(default_factory=list)
    entries: list[GlossaryEntry] = Field(default_factory=list)

    def to_prompt_header(self) -> str:
        """Render a concise summary suitable for prepending to chunk LLM prompts."""
        lines: list[str] = ["## Global Glossary"]
        if self.acronyms:
            lines.append("### Acronyms")
            for abbr, expansion in self.acronyms.items():
                lines.append(f"- {abbr}: {expansion}")
        if self.defined_terms:
            lines.append("### Defined Terms")
            for term, definition in self.defined_terms.items():
                lines.append(f"- {term}: {definition}")
        if self.key_entities:
            lines.append(f"### Key Entities\n- {', '.join(self.key_entities)}")
        if self.section_titles:
            lines.append(f"### Sections ({len(self.section_titles)})")
            for title in self.section_titles[:25]:
                lines.append(f"- {title}")
        return "\n".join(lines)

    def save(self, path: Path | str) -> Path:
        """Persist this glossary as deterministic JSON on disk."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: Path | str) -> GlobalGlossary:
        """Load and strictly validate a persisted ``GlobalGlossary`` JSON file."""
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def merge(self, other: GlobalGlossary) -> GlobalGlossary:
        """Return a new glossary combining ``self`` and ``other`` deterministically."""
        defined_terms = dict(self.defined_terms)
        for term, definition in other.defined_terms.items():
            if term not in defined_terms:
                defined_terms[term] = definition

        acronyms = dict(self.acronyms)
        for abbr, expansion in other.acronyms.items():
            if abbr not in acronyms:
                acronyms[abbr] = expansion

        key_entities = _dedupe_preserve_order([*self.key_entities, *other.key_entities])
        section_titles = _dedupe_preserve_order([*self.section_titles, *other.section_titles])

        seen_entries = {(e.category, e.term) for e in self.entries}
        entries = list(self.entries)
        for entry in other.entries:
            key = (entry.category, entry.term)
            if key not in seen_entries:
                seen_entries.add(key)
                entries.append(entry)

        return GlobalGlossary(
            defined_terms=defined_terms,
            acronyms=acronyms,
            key_entities=key_entities,
            section_titles=section_titles,
            entries=entries,
        )


class DocumentChunk(BaseModel):
    """Single heading-scoped chunk carrying full provenance and character offsets."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(..., min_length=1)
    chunk_index: int = Field(..., ge=0)
    heading_path: list[str] = Field(default_factory=list)
    anchors: list[str] = Field(default_factory=list)
    start_char: int = Field(..., ge=0)
    end_char: int = Field(..., ge=0)
    char_count: int = Field(..., ge=0)
    text: str
    source_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")


class ChunkingResult(BaseModel):
    """Complete chunking output guaranteeing 0% truncation and persistent glossary."""

    model_config = ConfigDict(extra="forbid")

    source_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    source_path: Path
    total_chars: int = Field(..., ge=0)
    covered_chars: int = Field(..., ge=0)
    truncation_ratio: float = Field(..., ge=0.0, le=1.0)
    chunk_count: int = Field(..., ge=0)
    max_chunk_chars: int = Field(..., ge=1)
    overlap_chars: int = Field(..., ge=0)
    glossary: GlobalGlossary
    chunks: list[DocumentChunk] = Field(default_factory=list)


def _merge_covered_length(intervals: list[tuple[int, int]]) -> int:
    """Compute exact number of characters covered by the union of half-open intervals."""
    if not intervals:
        return 0
    sorted_iv = sorted(intervals)
    merged_start, merged_end = sorted_iv[0]
    total = 0
    for start, end in sorted_iv[1:]:
        if start <= merged_end:
            if end > merged_end:
                merged_end = end
        else:
            total += merged_end - merged_start
            merged_start, merged_end = start, end
    total += merged_end - merged_start
    return total


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


class HierarchicalChunker:
    """Heading-aware recursive chunker with Global Glossary extraction and 0% truncation."""

    def __init__(self, max_chunk_chars: int = 8000, overlap_chars: int = 0) -> None:
        if max_chunk_chars < 1:
            raise ValueError(f"max_chunk_chars must be >= 1, got {max_chunk_chars}")
        if overlap_chars < 0:
            raise ValueError(f"overlap_chars must be >= 0, got {overlap_chars}")
        if overlap_chars >= max_chunk_chars:
            raise ValueError(
                f"overlap_chars ({overlap_chars}) must be strictly less than "
                f"max_chunk_chars ({max_chunk_chars})."
            )
        self.max_chunk_chars = max_chunk_chars
        self.overlap_chars = overlap_chars

    @staticmethod
    def extract_glossary(markdown: str) -> GlobalGlossary:
        """Extract defined terms, acronyms, key entities, and section titles."""
        defined_terms: dict[str, str] = {}
        acronyms: dict[str, str] = {}
        section_titles: list[str] = []
        entity_counts: dict[str, int] = {}
        entries: list[GlossaryEntry] = []

        current_anchor: str | None = None
        in_glossary_section = False
        in_fence = False
        fence_char = ""

        for line in markdown.splitlines():
            fence_match = _FENCE_RE.match(line)
            if fence_match:
                marker = fence_match.group(1)[0]
                if not in_fence:
                    in_fence = True
                    fence_char = marker
                elif marker == fence_char:
                    in_fence = False
                    fence_char = ""
                continue
            if in_fence:
                continue

            anchor_match = _ANCHOR_TAG_RE.search(line)
            if anchor_match:
                current_anchor = anchor_match.group(0)

            heading_match = _HEADING_LINE_RE.match(line)
            if heading_match:
                title = heading_match.group(2).strip()
                if title not in section_titles:
                    section_titles.append(title)
                    entries.append(
                        GlossaryEntry(
                            term=title,
                            definition=f"Heading level {len(heading_match.group(1))}",
                            category="section",
                            first_anchor=current_anchor,
                        )
                    )
                title_lower = title.lower()
                in_glossary_section = any(
                    kw in title_lower for kw in ("glossary", "definitions", "terminology", "acronyms")
                )
                continue

            # Acronyms: "Full Expansion (ACRONYM)"
            for m in _ACRONYM_AFTER_FULL_RE.finditer(line):
                raw_candidate, abbr = m.group(1).strip(), m.group(2).strip()
                if abbr not in acronyms and len(abbr) >= 2:
                    expansion = _trim_acronym_expansion(raw_candidate, abbr)
                    if expansion:
                        acronyms[abbr] = expansion
                        entries.append(
                            GlossaryEntry(
                                term=abbr,
                                definition=expansion,
                                category="acronym",
                                first_anchor=current_anchor,
                            )
                        )

            # Acronyms: "ACRONYM (Full Expansion)"
            for m in _ACRONYM_BEFORE_FULL_RE.finditer(line):
                abbr, expansion = m.group(1).strip(), m.group(2).strip()
                if abbr not in acronyms and len(abbr) >= 2:
                    acronyms[abbr] = expansion
                    entries.append(
                        GlossaryEntry(
                            term=abbr,
                            definition=expansion,
                            category="acronym",
                            first_anchor=current_anchor,
                        )
                    )

            # Bold definition list items: "- **Term**: Definition"
            bold_match = _BOLD_DEF_RE.match(line)
            if bold_match:
                term = bold_match.group(1).strip()
                definition = bold_match.group(2).strip()
                if term and definition and term not in defined_terms:
                    defined_terms[term] = definition
                    entries.append(
                        GlossaryEntry(
                            term=term,
                            definition=definition,
                            category="defined_term",
                            first_anchor=current_anchor,
                        )
                    )

            # Quoted definitions: '"Term" means ...'
            for qm in _QUOTED_MEANS_RE.finditer(line):
                term = qm.group(1).strip()
                definition = qm.group(2).strip()
                if term not in defined_terms:
                    defined_terms[term] = definition
                    entries.append(
                        GlossaryEntry(
                            term=term,
                            definition=definition,
                            category="defined_term",
                            first_anchor=current_anchor,
                        )
                    )

            # Markdown table definitions inside glossary sections
            if in_glossary_section:
                tbl_match = _TABLE_DEF_RE.match(line)
                if tbl_match:
                    col1 = tbl_match.group(1).strip().strip("*")
                    col2 = tbl_match.group(2).strip()
                    if (
                        col1
                        and col2
                        and not set(col1) <= {"-", ":"}
                        and col1.lower() not in {"term", "acronym", "symbol", "name"}
                        and col1 not in defined_terms
                    ):
                        defined_terms[col1] = col2
                        entries.append(
                            GlossaryEntry(
                                term=col1,
                                definition=col2,
                                category="defined_term",
                                first_anchor=current_anchor,
                            )
                        )

            # PascalCase / CamelCase system entities (e.g. PaymentGateway, OrderService)
            for em in _PASCAL_ENTITY_RE.finditer(line):
                entity = em.group(1)
                if entity not in _STOP_ENTITIES:
                    entity_counts[entity] = entity_counts.get(entity, 0) + 1

        # Keep key entities deterministically ordered by frequency then alphabetically.
        key_entities = [
            name
            for name, _ in sorted(entity_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:50]
        ]
        for entity in key_entities:
            entries.append(
                GlossaryEntry(
                    term=entity,
                    definition="Key domain/system entity",
                    category="entity",
                    first_anchor=None,
                )
            )

        return GlobalGlossary(
            defined_terms=defined_terms,
            acronyms=acronyms,
            key_entities=key_entities,
            section_titles=section_titles,
            entries=entries,
        )

    @staticmethod
    def _partition_by_headings(text: str) -> list[tuple[int, int, list[str]]]:
        """Partition ``text`` into contiguous half-open spans scoped by Markdown headings."""
        if not text:
            return []

        lines = text.splitlines(keepends=True)
        sections: list[tuple[int, int, list[str]]] = []
        heading_stack: list[tuple[int, str]] = []
        section_start = 0
        current_path: list[str] = []
        offset = 0
        in_fence = False
        fence_char = ""

        for line in lines:
            stripped = line.rstrip("\r\n")
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

            match = _HEADING_LINE_RE.match(stripped) if not in_fence else None
            if match:
                if offset > section_start:
                    sections.append((section_start, offset, list(current_path)))
                level = len(match.group(1))
                title = match.group(2).strip()
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, title))
                current_path = [h[1] for h in heading_stack]
                section_start = offset
            offset += len(line)

        if offset > section_start:
            sections.append((section_start, offset, list(current_path)))

        return sections

    @staticmethod
    def _choose_split_point(text: str, start: int, max_end: int) -> int:
        """Choose the cleanest split offset in ``(start, max_end]`` without dropping chars."""
        window = text[start:max_end]
        min_accept = max(1, len(window) // 4)

        # 1. Paragraph break (\n\n)
        idx = window.rfind("\n\n")
        if idx >= min_accept:
            return start + idx + 2

        # 2. Line break (\n)
        idx = window.rfind("\n")
        if idx >= min_accept:
            return start + idx + 1

        # 3. Sentence boundary
        best_sent = -1
        for sep in (". ", "! ", "? "):
            pos = window.rfind(sep)
            if pos > best_sent:
                best_sent = pos
        if best_sent >= min_accept:
            return start + best_sent + 2

        # 4. Word boundary
        idx = window.rfind(" ")
        if idx >= min_accept:
            return start + idx + 1

        # 5. Hard character boundary (unbroken token)
        return max_end

    def _split_span(
        self,
        text: str,
        start: int,
        end: int,
        heading_path: list[str],
        budget: int,
    ) -> list[tuple[int, int, list[str]]]:
        """Recursively split ``[start, end)`` into exact contiguous spans <= ``budget``."""
        spans: list[tuple[int, int, list[str]]] = []
        cursor = start
        while cursor < end:
            if end - cursor <= budget:
                spans.append((cursor, end, list(heading_path)))
                break
            split_at = self._choose_split_point(text, cursor, cursor + budget)
            if split_at <= cursor:  # pragma: no cover - defensive guard
                split_at = cursor + budget
            spans.append((cursor, split_at, list(heading_path)))
            cursor = split_at
        return spans

    @staticmethod
    def _has_prose_before_offset(chunk_text: str, rel_offset: int) -> bool:
        """Return True if non-heading, non-anchor prose precedes ``rel_offset`` in ``chunk_text``."""
        prefix = chunk_text[:rel_offset]
        for line in prefix.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if _HEADING_LINE_RE.match(stripped):
                continue
            if _ANCHOR_TAG_RE.fullmatch(stripped):
                continue
            return True
        return False

    def chunk(self, doc: NormalizedDocument) -> ChunkingResult:
        """Chunk ``doc`` with heading awareness, anchor tracking, glossary, and 0% truncation."""
        text = doc.markdown
        total_chars = len(text)
        glossary = self.extract_glossary(text)

        if total_chars == 0:
            return ChunkingResult(
                source_sha256=doc.source_sha256,
                source_path=doc.source_path,
                total_chars=0,
                covered_chars=0,
                truncation_ratio=0.0,
                chunk_count=0,
                max_chunk_chars=self.max_chunk_chars,
                overlap_chars=self.overlap_chars,
                glossary=glossary,
                chunks=[],
            )

        core_budget = self.max_chunk_chars - self.overlap_chars
        heading_sections = self._partition_by_headings(text)
        base_spans: list[tuple[int, int, list[str]]] = []
        for s_start, s_end, h_path in heading_sections:
            base_spans.extend(self._split_span(text, s_start, s_end, h_path, core_budget))

        # Index document-level anchors (including heading-derived section anchors) by offset.
        doc_anchors_sorted = sorted(doc.anchors, key=lambda a: a.char_offset)

        chunks: list[DocumentChunk] = []
        active_page_anchor: str | None = None
        active_section_anchor: str | None = None

        for idx, (base_start, base_end, heading_path) in enumerate(base_spans):
            if idx > 0 and self.overlap_chars > 0:
                start_char = max(0, base_start - self.overlap_chars)
            else:
                start_char = base_start
            end_char = base_end
            chunk_text = text[start_char:end_char]

            # Combine inline anchor tags with document-level anchors falling in [start_char, end_char).
            positioned_anchors: list[tuple[int, str]] = [
                (m.start(), m.group(0)) for m in _ANCHOR_TAG_RE.finditer(chunk_text)
            ]
            for anchor in doc_anchors_sorted:
                if start_char <= anchor.char_offset < end_char:
                    positioned_anchors.append((anchor.char_offset - start_char, anchor.tag))
            positioned_anchors.sort(key=lambda item: item[0])

            first_page_rel = next(
                (rel for rel, tag in positioned_anchors if tag.startswith("[page:")), None
            )
            first_section_rel = next(
                (rel for rel, tag in positioned_anchors if tag.startswith("[section:")), None
            )

            chunk_anchors: list[str] = []
            # Inherit active page/section anchors when chunk continues mid-page/mid-section.
            if active_page_anchor and (
                first_page_rel is None
                or self._has_prose_before_offset(chunk_text, first_page_rel)
            ):
                chunk_anchors.append(active_page_anchor)
            if active_section_anchor and (
                first_section_rel is None
                or self._has_prose_before_offset(chunk_text, first_section_rel)
            ):
                chunk_anchors.append(active_section_anchor)
            chunk_anchors.extend(tag for _, tag in positioned_anchors)
            chunk_anchors = _dedupe_preserve_order(chunk_anchors)

            # Advance active anchors based on the non-overlapping tail of this chunk.
            core_positioned: list[tuple[int, str]] = [
                (base_start + m.start(), m.group(0))
                for m in _ANCHOR_TAG_RE.finditer(text[base_start:base_end])
            ]
            for anchor in doc_anchors_sorted:
                if base_start <= anchor.char_offset < base_end:
                    core_positioned.append((anchor.char_offset, anchor.tag))
            core_positioned.sort(key=lambda item: item[0])

            core_pages = [tag for _, tag in core_positioned if tag.startswith("[page:")]
            if core_pages:
                active_page_anchor = core_pages[-1]
            core_sections = [tag for _, tag in core_positioned if tag.startswith("[section:")]
            if core_sections:
                active_section_anchor = core_sections[-1]

            chunks.append(
                DocumentChunk(
                    chunk_id=f"chunk-{idx:04d}",
                    chunk_index=idx,
                    heading_path=heading_path,
                    anchors=chunk_anchors,
                    start_char=start_char,
                    end_char=end_char,
                    char_count=len(chunk_text),
                    text=chunk_text,
                    source_sha256=doc.source_sha256,
                )
            )

        covered_chars = _merge_covered_length([(c.start_char, c.end_char) for c in chunks])
        truncation_ratio = (total_chars - covered_chars) / total_chars

        return ChunkingResult(
            source_sha256=doc.source_sha256,
            source_path=doc.source_path,
            total_chars=total_chars,
            covered_chars=covered_chars,
            truncation_ratio=truncation_ratio,
            chunk_count=len(chunks),
            max_chunk_chars=self.max_chunk_chars,
            overlap_chars=self.overlap_chars,
            glossary=glossary,
            chunks=chunks,
        )
