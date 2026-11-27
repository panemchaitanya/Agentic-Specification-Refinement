"""Deterministic pre-LLM PDF/SRS text cleaner with line-level audit trail and interactive review UI."""

from __future__ import annotations

import collections
from collections.abc import Sequence
from enum import Enum
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict, Field


class NoiseCategory(str, Enum):
    """Taxonomy of empirically observed PDF-to-text noise categories in PURE SRS documents."""

    REPEATED_HEADER_FOOTER = "REPEATED_HEADER_FOOTER"
    PAGE_NUMBER_STAMP = "PAGE_NUMBER_STAMP"
    WEB_PRINT_NAVIGATION = "WEB_PRINT_NAVIGATION"
    TOC_AND_LISTS = "TOC_AND_LISTS"
    REVISION_AND_DOC_HISTORY = "REVISION_AND_DOC_HISTORY"
    COPYRIGHT_AND_LEGAL = "COPYRIGHT_AND_LEGAL"
    COVER_FRONTMATTER = "COVER_FRONTMATTER"
    DECORATIVE_RULE = "DECORATIVE_RULE"
    EXCESS_WHITESPACE = "EXCESS_WHITESPACE"
    PDF_REFLOW_NORMALIZATION = "PDF_REFLOW_NORMALIZATION"
    TABLE_RECONSTRUCTION = "TABLE_RECONSTRUCTION"


class CleanerConfig(BaseModel):
    """Toggleable configuration for deterministic pre-LLM PDF noise cleaning."""

    model_config = ConfigDict(extra="forbid")

    strip_repeated_edges: bool = True
    strip_page_numbers: bool = True
    strip_web_nav: bool = True
    strip_toc_blocks: bool = True
    strip_revision_history: bool = True
    strip_copyright_legal: bool = True
    strip_cover_frontmatter: bool = True
    strip_decorative_rules: bool = True
    collapse_whitespace: bool = True
    reconstruct_tables: bool = True
    hoist_mid_block_row_numbers: bool = True
    reflow_pdf_wraps: bool = True
    protect_normative_lines: bool = True
    min_repeat_pages: int = Field(default=3, ge=2)
    min_repeat_ratio: float = Field(default=0.15, ge=0.05, le=1.0)
    chunk_size_chars: int = Field(default=8000, ge=500)


class RemovedLineAudit(BaseModel):
    """Single audited line removed by a deterministic cleaning rule."""

    model_config = ConfigDict(extra="forbid")

    line_no: int = Field(..., ge=1)
    page_no: int = Field(..., ge=1)
    category: NoiseCategory
    rule_detail: str = Field(..., min_length=1)
    text: str
    contains_normative_modal: bool = False


class DocumentCleanResult(BaseModel):
    """Complete deterministic cleaning output and provenance audit for one document."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(..., min_length=1)
    source_path: str = Field(..., min_length=1)
    source_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    raw_chars: int = Field(..., ge=0)
    cleaned_chars: int = Field(..., ge=0)
    chars_saved: int = Field(..., ge=0)
    reduction_pct: float = Field(..., ge=0.0, le=100.0, allow_inf_nan=False)
    raw_lines: int = Field(..., ge=0)
    cleaned_lines: int = Field(..., ge=0)
    page_count: int = Field(..., ge=1)
    est_tokens_saved: int = Field(..., ge=0)
    est_chunks_before: int = Field(..., ge=0)
    est_chunks_after: int = Field(..., ge=0)
    category_counts: dict[str, int] = Field(default_factory=dict)
    category_chars_saved: dict[str, int] = Field(default_factory=dict)
    normative_removed_count: int = Field(..., ge=0)
    removed_lines: list[RemovedLineAudit] = Field(default_factory=list)
    cleaned_text: str


class BatchCleanSummary(BaseModel):
    """Aggregate cleaning summary across a trial batch of SRS documents."""

    model_config = ConfigDict(extra="forbid")

    total_docs: int = Field(..., ge=0)
    total_raw_chars: int = Field(..., ge=0)
    total_cleaned_chars: int = Field(..., ge=0)
    total_chars_saved: int = Field(..., ge=0)
    overall_reduction_pct: float = Field(..., ge=0.0, le=100.0, allow_inf_nan=False)
    total_est_tokens_saved: int = Field(..., ge=0)
    total_chunks_before: int = Field(..., ge=0)
    total_chunks_after: int = Field(..., ge=0)
    total_normative_removed: int = Field(..., ge=0)
    category_totals: dict[str, int] = Field(default_factory=dict)
    review_ui_path: str | None = None
    doc_ids: list[str] = Field(default_factory=list)


# --- Compiled deterministic pattern catalog (empirically tuned across 15 PURE dev SRS docs) ---

_PAGE_BREAK_MARKER = "\x00__REQHQ_PAGE_BREAK__\x00"

_DIGIT_RE = re.compile(r"\d+")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
_CTRL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0e-\x1f]")
_PUA_BULLET_RE = re.compile(r"[\uf0b7\uf0a7\uf0d8\uf076\uf0a8\u2022]")
_WINGDING_Z_BULLET_RE = re.compile(r"^(\s*)z\s{1,4}(?=[A-Z\"'(\[])")

_NORMATIVE_SHIELD_RE = re.compile(
    r"\b(?:shall(?:\s+not)?|must(?:\s+not)?|(?:REQ|FR|NFR|SRS|UC)-\d+)\b",
    re.IGNORECASE,
)
_TABLE_REQ_HEADER_RE = re.compile(
    r"\b(?:req(?:uirement)?\s*id|requirement\s+statement|use\s+case|description\s+and\s+priority)\b",
    re.IGNORECASE,
)

_PAGE_NUMBER_ONLY_RE = re.compile(
    r"^\s*(?:[-–—]\s*)?(?:\d{1,4}|[ivxlcdm]{1,7})(?:\s*[-–—])?\s*$",
    re.IGNORECASE,
)
_SECTION_PAGE_STAMP_RE = re.compile(r"^\s*(?:[A-Z]|\d{1,2})\s*[-–—]\s*\d{1,3}\s*$")
_PAGE_OF_LINE_RE = re.compile(
    r"^(?:.{0,80}\s)?page\s+\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?\s*$",
    re.IGNORECASE,
)

# Browser print footers ALWAYS include a trailing print date (never match bare body/reference URLs!)
_WEB_PRINT_FOOTER_RE = re.compile(
    r"^https?://\S+\s+\d{1,2}/\d{1,2}/\d{2,4}\s*$",
    re.IGNORECASE,
)
_WEB_NAV_LINK_RE = re.compile(
    r"^(?:back\s+to\s+(?:contents|top|table\s+of\s+contents)|return\s+to\s+(?:top|contents)|"
    r"(?:previous|next)\s+page|top\s+of\s+page)\s*$",
    re.IGNORECASE,
)

_TOC_HEADING_RE = re.compile(
    r"^(?:table\s+of\s+contents|contents|list\s+of\s+(?:figures|tables|acronyms|abbreviations|illustrations))\s*:?$",
    re.IGNORECASE,
)
_TOC_COL_HEADER_RE = re.compile(
    r"^(?:s\.?\s*no\.?\s+contents|section\s+title\s+page|chapter\s+page|figure\s+page|table\s+page)\s*$",
    re.IGNORECASE,
)
_TOC_DOT_LEADER_RE = re.compile(r"(?:\.{4,}|(?:\.\s){4,})\s*(?:[A-Za-z0-9-]{1,10})?\s*$")
_TOC_TRAILING_PAGE_RE = re.compile(
    r"^(?:\S.{1,130}?)\s{2,}(?:\d{1,4}|[ivxlcdm]{1,6}|[A-Z]-\d{1,3})\s*$",
    re.IGNORECASE,
)
_TOC_SHORT_ENTRY_RE = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*\.?|[A-Z]\d+)\s+[A-Za-z][A-Za-z0-9 /&(),:'\"-]{1,95}|(?:Figure|Table|Appendix)\s+[A-Za-z0-9.-]+.{0,85})$"
)
_SECTION_NUM_PREFIX_RE = re.compile(r"^(\d+(?:\.\d+)*|[A-Z]\d+)\.?\s+([A-Za-z].*)$")

_REV_HISTORY_HEADING_RE = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*\.?\s+)?(?:revision\s+history|document\s+(?:history|change\s+history|control|approval|sign[- ]?off|information)|"
    r"change\s+(?:log|history|record)|version\s+history|approval\s+sheet|sign[- ]?off|acknowledg(?:e)?ments))\s*:?$",
    re.IGNORECASE,
)
_BODY_SECTION_START_RE = re.compile(
    r"^(?:1(?:\.0)?\.?\s+(?:introduction|overview|purpose|scope|general|system\s+overview)|"
    r"chapter\s+1\b|section\s+1\b)",
    re.IGNORECASE,
)
_MAJOR_NUMBERED_HEADING_RE = re.compile(r"^\d+(?:\.\d+)*\.?\s+[A-Z][A-Za-z0-9 /&(),:'\"-]{2,80}$")

_COPYRIGHT_LEGAL_RE = re.compile(
    r"(?:copyright\s*(?:©|\(c\))?|©|\ball\s+rights\s+reserved\b|"
    r"\bthis\s+page\s+(?:is\s+)?intentionally\s+left\s+blank\b|\bblank\s+page\b|"
    r"^\s*confidential(?:\s+and\s+proprietary)?\b)",
    re.IGNORECASE,
)
_DECORATIVE_RULE_RE = re.compile(r"^\s*[-_=*#~.]{5,}\s*$")

_COVER_META_RE = re.compile(
    r"^(?:version|release\s+date|date|prepared\s+by|prepared\s+for|submitted\s+by|submitted\s+to|"
    r"author(?:s)?|team\s+members?|group\s+name|group\s+project|document\s+(?:id|number|no\.?)|"
    r"project\s+(?:id|name|title)|draft\s+core\s+scope|final\s+report|status)\s*[:\-]?",
    re.IGNORECASE,
)
_US_ZIP_ADDR_RE = re.compile(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b")
_DATE_ONLY_RE = re.compile(
    r"^(?:(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},?\s+\d{4}|"
    r"(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{4}|"
    r"\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})\s*$",
    re.IGNORECASE,
)
_LIST_OR_HEADING_START_RE = re.compile(
    r"^(?:#{1,6}\s+|[-*+]\s+|\d+(?:\.\d+)*[.)]\s+|\d+(?:\.\d+)+\s+|[A-Z]{2,5}-\d+|\||[A-Z]\d+\s+)"
)
_NUMBERED_HEADING_STRICT_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+[A-Z][A-Za-z0-9 /&(),:'\"-]{1,75}|(?:Chapter|Section|Appendix|Use\s+Case)\s+[A-Za-z0-9.-]+.*)$"
)
_STANDALONE_ROW_NUM_RE = re.compile(r"^(\s{1,12})(\d{1,3}\.)\s*$")
_INLINE_ROW_NUM_RE = re.compile(r"^(\s{1,12})(\d{1,3}\.)(\s{3,})(\S.*)$")
_ROW_SEGMENT_RE = re.compile(r"\S+(?:[ \t]\S+)*")

_CONTINUATION_TAIL_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "for",
        "with",
        "within",
        "by",
        "on",
        "at",
        "from",
        "into",
        "as",
        "that",
        "which",
        "who",
        "is",
        "are",
        "be",
        "been",
        "will",
        "shall",
        "must",
        "should",
        "can",
        "may",
        "not",
        "its",
        "their",
        "this",
        "these",
        "such",
    }
)
_TITLE_CONNECTORS = frozenset({"of", "and", "the", "for", "in", "to", "on", "with", "by", "a", "an", "or", "at", "from", "as", "&", "-", "–"})


def _atomic_write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(normalized)
    os.replace(tmp_path, path)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _edge_signature(line: str) -> str:
    collapsed = _MULTI_SPACE_RE.sub(" ", line.strip())
    return _DIGIT_RE.sub("#", collapsed)


def _is_all_caps_banner(line: str) -> bool:
    letters = [ch for ch in line if ch.isalpha()]
    return len(letters) >= 4 and all(ch.isupper() for ch in letters) and len(line) <= 100


def _looks_like_body_sentence(line: str) -> bool:
    words = re.findall(r"[A-Za-z]{2,}", line)
    return len(words) >= 8 and line.endswith((".", "!", "?")) and not _TOC_DOT_LEADER_RE.search(line)


def _extract_section_number(line: str) -> str | None:
    m = _SECTION_NUM_PREFIX_RE.match(line.strip())
    return m.group(1) if m else None


def _is_heading_line(line: str, *, prev_blank: bool, next_blank: bool) -> bool:
    """Distinguish standalone section/subsection headings from wrapped prose lines."""
    s = line.strip()
    if not s or s.startswith("|") or s.startswith("- "):
        return False
    if s.startswith("#"):
        return True
    # Requirement/prose sentences with modals or >= 10 words are never headings
    if _NORMATIVE_SHIELD_RE.search(s) or len(s.split()) > 9:
        return False
    if re.search(r"\b(?:should|will|allow|provide|ensure|enables?)\b", s, re.IGNORECASE):
        return False
    if _NUMBERED_HEADING_STRICT_RE.match(s) and not s.endswith((".", ",", ";")):
        return True
    if _is_all_caps_banner(s) and len(s) <= 75:
        return True
    # Short Title-Case label (e.g. "Registration", "Ease of Use", "Access Module", "User A (Alice)")
    if len(s) <= 60 and not s.endswith((".", ",", ";", "?", "!")):
        words = s.split()
        if 1 <= len(words) <= 8:
            content_words = [w for w in words if w.lower() not in _TITLE_CONNECTORS]
            cap_count = sum(1 for w in content_words if w[0].isupper() or w[0].isdigit() or w[0] == "(")
            if content_words and (cap_count / len(content_words)) >= 0.75:
                if prev_blank or next_blank or s.endswith(":"):
                    return True
    return False


def _detect_repeated_edge_signatures(
    pages_lines: list[list[tuple[int, str]]],
    *,
    min_repeat_pages: int,
    min_repeat_ratio: float,
) -> dict[str, int]:
    """Identify header/footer signatures repeated in the top/bottom 3 non-empty lines across pages."""
    page_count = len(pages_lines)
    if page_count < min_repeat_pages:
        return {}

    threshold = max(min_repeat_pages, math.ceil(page_count * min_repeat_ratio))
    sig_counts: collections.Counter[str] = collections.Counter()

    for page_rows in pages_lines:
        non_empty = [txt.strip() for _, txt in page_rows if txt.strip()]
        if not non_empty:
            continue
        edge_sigs: set[str] = set()
        for cand in (*non_empty[:3], *non_empty[-3:]):
            sig = _edge_signature(cand)
            if not (2 <= len(sig) <= 160) or not any(ch.isalpha() for ch in sig):
                continue
            if _NORMATIVE_SHIELD_RE.search(cand) or _TABLE_REQ_HEADER_RE.search(cand):
                continue
            edge_sigs.add(sig)
        for sig in edge_sigs:
            sig_counts[sig] += 1

    return {sig: cnt for sig, cnt in sig_counts.items() if cnt >= threshold}


def _split_column_segments(raw_line: str) -> list[tuple[int, str]]:
    """Split a raw layout-preserved line into (start_col, text) segments separated by >= 3 spaces."""
    segments: list[tuple[int, str]] = []
    pos = 0
    for part in re.split(r" {3,}", raw_line):
        if part.strip():
            lead = len(part) - len(part.lstrip(" "))
            start_col = pos + lead
            segments.append((start_col, _MULTI_SPACE_RE.sub(" ", part.strip())))
        pos += len(part) + 3
        # Re-align exact offset in raw_line
        if pos < len(raw_line):
            while pos < len(raw_line) and raw_line[pos] == " ":
                pos += 1
    # Recompute exact character start offsets via find to stay 100% accurate
    exact: list[tuple[int, str]] = []
    cursor = 0
    for _approx, text in segments:
        first_tok = text.split(" ", 1)[0]
        found = raw_line.find(first_tok, cursor)
        col = found if found >= 0 else cursor
        exact.append((col, text))
        cursor = col + len(first_tok)
    return exact


def _hoist_mid_block_row_numbers_in_block(block: list[str]) -> tuple[list[str], int]:
    """Fix 2-column vertically-centered PDF requirement tables (e.g., CCTNS NFRs).

    When pdftotext extracts a 2-column table [row_num | multi-line text], the row number
    lands at the vertical midpoint of the paragraph (either standalone `   4.` or inline
    `   5.    affecting...`). Hoist it cleanly to the start of the block!
    """
    if len(block) < 2:
        return block, 0

    indents = [len(ln) - len(ln.lstrip(" ")) for ln in block if ln.strip()]
    if not indents or max(indents) < 6:
        return block, 0

    hoisted_num: str | None = None
    cleaned_lines: list[str] = []
    hoist_count = 0

    # Check if block already begins with a row number on line 0
    first_s = block[0].strip()
    if re.match(r"^\d{1,3}\.\s+\S", first_s):
        return block, 0

    for idx, ln in enumerate(block):
        if idx > 0 and hoisted_num is None:
            m_standalone = _STANDALONE_ROW_NUM_RE.match(ln)
            if m_standalone:
                hoisted_num = m_standalone.group(2)
                hoist_count += 1
                continue
            m_inline = _INLINE_ROW_NUM_RE.match(ln)
            if m_inline:
                hoisted_num = m_inline.group(2)
                hoist_count += 1
                # Preserve continuation text at standard body indent
                body_indent = " " * (len(m_inline.group(1)) + len(m_inline.group(2)) + len(m_inline.group(3)))
                cleaned_lines.append(body_indent + m_inline.group(4))
                continue
        cleaned_lines.append(ln)

    if hoisted_num is not None and cleaned_lines:
        target_idx = 0
        # If line 0 is a left-aligned sub-heading right above deeply-indented requirement text
        # (e.g., CCTNS " Access Module" above "               The System must..."), hoist onto line 1!
        if len(cleaned_lines) >= 2:
            ind0 = len(cleaned_lines[0]) - len(cleaned_lines[0].lstrip(" "))
            ind1 = len(cleaned_lines[1]) - len(cleaned_lines[1].lstrip(" "))
            if ind0 <= 3 and ind1 >= 8 and len(cleaned_lines[0].strip()) <= 50:
                target_idx = 1
        cleaned_lines[target_idx] = f"{hoisted_num} {cleaned_lines[target_idx].lstrip(' ')}"
        return cleaned_lines, hoist_count
    return block, 0


def _try_reconstruct_pipe_table(block: list[str]) -> tuple[list[str] | None, int]:
    """Convert a genuine column-aligned PDF table block into clean Markdown pipe rows.

    Requires >= 2 non-heading multi-column rows sharing a consistent second-column X-offset
    (+/- 3 chars) so section headings and full-justified prose are never misclassified as tables.
    """
    if len(block) < 2:
        return None, 0

    parsed_rows = [_split_column_segments(ln) for ln in block]
    # Reject blocks containing full-width single-column prose lines (not a table!)
    for segs in parsed_rows:
        if len(segs) == 1 and segs[0][0] <= 5 and len(segs[0][1]) >= 65:
            return None, 0

    # Ignore 2-column section headings like "1.   Introduction" or "1.1   Purpose"
    _sec_num_only = re.compile(r"^\d+(?:\.\d+)*\.?$")
    multi_rows = [
        segs
        for segs in parsed_rows
        if len(segs) >= 2 and not (len(segs) == 2 and _sec_num_only.match(segs[0][1]))
    ]
    if len(multi_rows) < 2:
        return None, 0

    # Reject blocks whose lines are mostly bullets
    bullet_rows = sum(1 for ln in block if ln.lstrip(" ").startswith(("- ", "* ")))
    if bullet_rows >= len(block) / 2:
        return None, 0

    # Verify column-2 alignment across multi-column rows (tolerance +/- 3 chars)
    col2_offsets = sorted(segs[1][0] for segs in multi_rows)
    median_c2 = col2_offsets[len(col2_offsets) // 2]
    aligned_c2 = sum(1 for c in col2_offsets if abs(c - median_c2) <= 3)
    if aligned_c2 < max(2, math.ceil(len(multi_rows) * 0.65)):
        return None, 0

    # Derive canonical column anchor offsets from aligned multi-column rows
    max_cols = min(6, max(len(segs) for segs in multi_rows))
    col_buckets: list[list[int]] = [[] for _ in range(max_cols)]
    for segs in multi_rows:
        if abs(segs[1][0] - median_c2) <= 3:
            for c_idx, (col_x, _txt) in enumerate(segs[:max_cols]):
                col_buckets[c_idx].append(col_x)
    anchors = [
        sorted(b)[len(b) // 2] if b else (i * 20)
        for i, b in enumerate(col_buckets)
    ]

    def _nearest_col(col_x: int) -> int:
        return min(range(len(anchors)), key=lambda i: abs(anchors[i] - col_x))

    table_rows: list[list[str]] = []
    for segs in parsed_rows:
        if not segs:
            continue
        mapped: dict[int, str] = {}
        for col_x, txt in segs:
            ci = _nearest_col(col_x)
            mapped[ci] = f"{mapped[ci]} {txt}".strip() if ci in mapped else txt

        # Case A: Continuation line (no col-0 text)
        if 0 not in mapped and table_rows:
            prev_row = table_rows[-1]
            # Never merge into header row (row 0), and start a new row if previous col-1 already ended a sentence!
            prev_col1 = prev_row[1] if len(prev_row) > 1 else ""
            if len(table_rows) == 1 or (prev_row[0] and prev_col1.endswith((".", "!", "?"))):
                table_rows.append([mapped.get(i, "") for i in range(len(anchors))])
            else:
                for ci, txt in mapped.items():
                    table_rows[-1][ci] = f"{table_rows[-1][ci]} {txt}".strip()
            continue

        # Case B: Vertically-centered col-0 label (only col-0 text, previous row has empty col-0!)
        if list(mapped.keys()) == [0] and table_rows and not table_rows[-1][0]:
            table_rows[-1][0] = mapped[0]
            continue

        # Case C: Normal row (or stacked col-0 wrap like "Macromedia" + "Flash")
        if (
            list(mapped.keys()) == [0]
            and len(table_rows) >= 2
            and table_rows[-1][0]
            and not table_rows[-1][0].endswith(":")
            and len(table_rows[-1]) > 1
            and table_rows[-1][1]
            and not table_rows[-1][1].endswith((".", "!", "?"))
        ):
            table_rows[-1][0] = f"{table_rows[-1][0]} {mapped[0]}".strip()
            continue

        row_cells = [mapped.get(i, "") for i in range(len(anchors))]
        table_rows.append(row_cells)

    def _fmt_row(cells: list[str]) -> str:
        safe = [c.replace("|", r"\|").strip() for c in cells]
        return "| " + " | ".join(safe) + " |"

    md_lines = [_fmt_row(table_rows[0])]
    if all(len(c) <= 40 for c in table_rows[0]) and any(c for c in table_rows[0][1:]):
        md_lines.append("| " + " | ".join("---" for _ in anchors) + " |")
    for row in table_rows[1:]:
        md_lines.append(_fmt_row(row))
    return md_lines, len(table_rows)


class PDFTextCleaner:
    """Deterministic, provenance-preserving PDF-to-text noise cleaner."""

    def __init__(self, config: CleanerConfig | None = None) -> None:
        self.config = config if config is not None else CleanerConfig()

    def _process_layout_blocks(
        self,
        raw_kept: list[str],
    ) -> tuple[list[str], int, int, int]:
        """Apply row-number hoisting, table reconstruction, and whitespace compaction per block.

        Returns ``(lines, table_rows_reconstructed, hoisted_row_nums, ws_chars_saved)``.
        """
        cfg = self.config
        out: list[str] = []
        table_rows_total = 0
        hoisted_total = 0
        ws_chars_saved = 0

        def _flush_block(block: list[str]) -> None:
            nonlocal table_rows_total, hoisted_total, ws_chars_saved
            if not block:
                return

            # 1. Try reconstructing true column-aligned PDF tables into Markdown pipe tables
            if cfg.reconstruct_tables:
                pipe_lines, n_rows = _try_reconstruct_pipe_table(block)
                if pipe_lines is not None:
                    raw_len = sum(len(ln) for ln in block)
                    new_len = sum(len(ln) for ln in pipe_lines)
                    if raw_len > new_len:
                        ws_chars_saved += raw_len - new_len
                    out.extend(pipe_lines)
                    table_rows_total += n_rows
                    return

            # 2. Hoist mid-sentence vertically-centered row numbers (e.g., CCTNS NFR tables)
            working = block
            if cfg.hoist_mid_block_row_numbers:
                working, hoisted = _hoist_mid_block_row_numbers_in_block(working)
                hoisted_total += hoisted

            # 3. Collapse wide padding on normal prose lines (preserve existing Markdown pipes)
            for ln in working:
                stripped = ln.strip()
                if cfg.collapse_whitespace and not stripped.startswith("|"):
                    compact = _MULTI_SPACE_RE.sub(" ", stripped)
                    saved = len(ln) - len(compact)
                    if saved > 0:
                        ws_chars_saved += saved
                    out.append(compact)
                else:
                    out.append(stripped)

        current_block: list[str] = []
        for entry in raw_kept:
            if entry == "" or entry == _PAGE_BREAK_MARKER:
                _flush_block(current_block)
                current_block = []
                out.append(entry)
            else:
                current_block.append(entry)
        _flush_block(current_block)
        return out, table_rows_total, hoisted_total, ws_chars_saved

    def _reflow_lines(self, kept_lines: list[str]) -> tuple[list[str], int]:
        """Heading-aware PDF line-wrap & cross-page-break sentence reflow."""
        if not kept_lines:
            return [], 0

        out: list[str] = []
        merges = 0
        n = len(kept_lines)
        i = 0

        while i < n:
            line = kept_lines[i]
            # Handle page-break sentinel: bridge split sentences across page boundaries!
            if line == _PAGE_BREAK_MARKER:
                if out and out[-1] not in ("", _PAGE_BREAK_MARKER) and i + 1 < n:
                    nxt = kept_lines[i + 1]
                    prev = out[-1]
                    if (
                        nxt not in ("", _PAGE_BREAK_MARKER)
                        and not prev.startswith("|")
                        and not nxt.startswith("|")
                        and not prev.endswith((".", "!", "?", ":", ";"))
                        and not _LIST_OR_HEADING_START_RE.match(nxt)
                        and not _is_heading_line(nxt, prev_blank=True, next_blank=(i + 2 >= n or kept_lines[i + 2] == ""))
                        and (nxt[0].islower() or prev.split()[-1].lower() in _CONTINUATION_TAIL_WORDS)
                    ):
                        out[-1] = f"{prev} {nxt}"
                        merges += 1
                        i += 2
                        continue
                # Otherwise collapse sentinel into a normal blank line if not duplicate
                if out and out[-1] != "":
                    out.append("")
                i += 1
                continue

            if not out:
                out.append(line)
                i += 1
                continue

            prev = out[-1]
            if not prev or not line:
                out.append(line)
                i += 1
                continue

            # Never merge Markdown pipe tables
            if prev.startswith("|") or line.startswith("|"):
                out.append(line)
                i += 1
                continue

            prev_was_blank = len(out) < 2 or out[-2] == ""
            next_is_blank = (i + 1 >= n) or kept_lines[i + 1] in ("", _PAGE_BREAK_MARKER)
            prev_is_heading = _is_heading_line(prev, prev_blank=prev_was_blank, next_blank=False)
            curr_is_heading = _is_heading_line(line, prev_blank=False, next_blank=next_is_blank)

            # De-hyphenate mid-word line break (e.g. "auto-\nmated")
            if (
                prev.endswith("-")
                and len(prev) >= 12
                and line[0].islower()
                and not prev_is_heading
                and not _LIST_OR_HEADING_START_RE.match(line)
            ):
                out[-1] = prev[:-1] + line
                merges += 1
                i += 1
                continue

            # Join wrapped prose sentences while preserving real headings & list items
            last_word = prev.split()[-1].rstrip(",;").lower() if prev.split() else ""
            should_join_prose = (
                not prev_is_heading
                and not curr_is_heading
                and not prev.endswith((".", "!", "?", ":", ";"))
                and not _LIST_OR_HEADING_START_RE.match(line)
                and (
                    line[0].islower()
                    or len(prev) >= 38
                    or prev.endswith(",")
                    or last_word in _CONTINUATION_TAIL_WORDS
                )
            )
            if should_join_prose:
                out[-1] = f"{prev} {line}"
                merges += 1
                i += 1
                continue

            out.append(line)
            i += 1

        return out, merges

    def clean_text(
        self,
        raw_text: str,
        *,
        doc_id: str = "document",
        source_path: str = "<memory>",
    ) -> DocumentCleanResult:
        """Clean raw PDF-extracted text deterministically with full line-level removal audit."""
        cfg = self.config
        source_sha = _sha256_text(raw_text)
        normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")
        raw_chars = len(normalized)

        # Pass 0: Split into pages via \x0c while preserving exact 1-based original line numbers.
        # NOTE: Use split("\n") (NOT splitlines()), because Python str.splitlines() consumes \x0c!
        raw_split_lines = normalized.split("\n") if normalized else []
        flat_rows: list[tuple[int, int, str]] = []
        pages_lines: list[list[tuple[int, str]]] = [[]]
        page_no = 1
        for line_no, orig_line in enumerate(raw_split_lines, start=1):
            if "\x0c" in orig_line:
                parts = orig_line.split("\x0c")
                for part_idx, part in enumerate(parts):
                    if part_idx > 0:
                        page_no += 1
                        pages_lines.append([])
                    cleaned_part = _CTRL_CHAR_RE.sub("", part).replace("\uf0d3", "©")
                    cleaned_part = _PUA_BULLET_RE.sub("- ", cleaned_part)
                    cleaned_part = _WINGDING_Z_BULLET_RE.sub(r"\1- ", cleaned_part)
                    if part_idx == 0 and not cleaned_part.strip() and len(parts) > 1:
                        continue
                    flat_rows.append((line_no, page_no, cleaned_part))
                    pages_lines[-1].append((line_no, cleaned_part))
            else:
                cleaned_part = _CTRL_CHAR_RE.sub("", orig_line).replace("\uf0d3", "©")
                cleaned_part = _PUA_BULLET_RE.sub("- ", cleaned_part)
                cleaned_part = _WINGDING_Z_BULLET_RE.sub(r"\1- ", cleaned_part)
                flat_rows.append((line_no, page_no, cleaned_part))
                pages_lines[-1].append((line_no, cleaned_part))

        page_count = max(1, len(pages_lines))
        raw_line_count = len(raw_split_lines)

        # Pass 1: Detect repeating page-edge headers/footers.
        repeated_sigs = (
            _detect_repeated_edge_signatures(
                pages_lines,
                min_repeat_pages=cfg.min_repeat_pages,
                min_repeat_ratio=cfg.min_repeat_ratio,
            )
            if cfg.strip_repeated_edges
            else {}
        )
        page_edge_positions: set[tuple[int, int]] = set()
        for p_idx, p_rows in enumerate(pages_lines, start=1):
            ne_linenos = [ln for ln, txt in p_rows if txt.strip()]
            for ln in (*ne_linenos[:4], *ne_linenos[-4:]):
                page_edge_positions.add((p_idx, ln))

        removed: list[RemovedLineAudit] = []
        cat_counts: dict[str, int] = {}
        cat_chars: dict[str, int] = {}
        kept_raw: list[str] = []

        def _record_removal(
            ln_no: int,
            pg_no: int,
            cat: NoiseCategory,
            detail: str,
            txt: str,
            *,
            count_as_normative_risk: bool = True,
        ) -> None:
            has_modal = bool(_NORMATIVE_SHIELD_RE.search(txt)) if count_as_normative_risk else False
            removed.append(
                RemovedLineAudit(
                    line_no=ln_no,
                    page_no=pg_no,
                    category=cat,
                    rule_detail=detail,
                    text=txt[:240],
                    contains_normative_modal=has_modal,
                )
            )
            key = cat.value
            cat_counts[key] = cat_counts.get(key, 0) + 1
            cat_chars[key] = cat_chars.get(key, 0) + len(txt) + 1

        def _is_repeated_edge_row(row_idx: int) -> bool:
            r_ln, r_pg, r_txt = flat_rows[row_idx]
            return _edge_signature(r_txt) in repeated_sigs and (r_pg, r_ln) in page_edge_positions

        # Lookahead: does a heading at index i lead into real body prose (even across 2-3 stacked headings)?
        def _heading_starts_body(idx: int) -> bool:
            checked = 0
            for j in range(idx + 1, min(len(flat_rows), idx + 22)):
                nxt = flat_rows[j][2].strip()
                if not nxt or _is_repeated_edge_row(j):
                    continue
                if _TOC_DOT_LEADER_RE.search(nxt) or _TOC_TRAILING_PAGE_RE.match(nxt):
                    return False
                if _looks_like_body_sentence(nxt) or _NORMATIVE_SHIELD_RE.search(nxt):
                    return True
                checked += 1
                if checked >= 5:
                    return False
            return False

        # Lookahead: is this a 2-line wrapped TOC entry whose next line ends in a page number?
        def _next_nonempty_has_toc_page_num(idx: int) -> bool:
            if idx + 1 >= len(flat_rows):
                return False
            nxt = flat_rows[idx + 1][2].strip()
            return bool(nxt and (_TOC_TRAILING_PAGE_RE.match(nxt) or _TOC_DOT_LEADER_RE.search(nxt)))

        in_toc_block = False
        toc_seen_sections: set[str] = set()
        in_rev_block = False
        rev_non_blank_consumed = 0
        seen_body_start = False
        non_blank_seen = 0
        prev_page_no = 1

        for idx, (ln_no, pg_no, line_txt) in enumerate(flat_rows):
            if pg_no != prev_page_no:
                # Replace page transition with sentinel so reflow can bridge cross-page sentence wraps
                if kept_raw and kept_raw[-1] == "":
                    kept_raw[-1] = _PAGE_BREAK_MARKER
                elif kept_raw and kept_raw[-1] != _PAGE_BREAK_MARKER:
                    kept_raw.append(_PAGE_BREAK_MARKER)
                prev_page_no = pg_no

            stripped = line_txt.strip()

            if not stripped:
                if cfg.collapse_whitespace:
                    if kept_raw and kept_raw[-1] not in ("", _PAGE_BREAK_MARKER):
                        kept_raw.append("")
                    else:
                        cat_counts[NoiseCategory.EXCESS_WHITESPACE.value] = (
                            cat_counts.get(NoiseCategory.EXCESS_WHITESPACE.value, 0) + 1
                        )
                        cat_chars[NoiseCategory.EXCESS_WHITESPACE.value] = (
                            cat_chars.get(NoiseCategory.EXCESS_WHITESPACE.value, 0) + 1
                        )
                else:
                    kept_raw.append("")
                continue

            non_blank_seen += 1
            is_normative = bool(_NORMATIVE_SHIELD_RE.search(stripped))
            sig = _edge_signature(stripped)

            # 1. Repeated page header / footer (top/bottom 4 non-empty lines on page)
            if (
                cfg.strip_repeated_edges
                and sig in repeated_sigs
                and (pg_no, ln_no) in page_edge_positions
                and not (cfg.protect_normative_lines and is_normative)
            ):
                _record_removal(
                    ln_no,
                    pg_no,
                    NoiseCategory.REPEATED_HEADER_FOOTER,
                    f"repeated_edge_{repeated_sigs[sig]}x",
                    stripped,
                )
                continue

            # 2. Standalone page numbers & section-page stamps
            if cfg.strip_page_numbers and not (cfg.protect_normative_lines and is_normative):
                if _PAGE_NUMBER_ONLY_RE.match(stripped) and (pg_no, ln_no) in page_edge_positions:
                    _record_removal(
                        ln_no,
                        pg_no,
                        NoiseCategory.PAGE_NUMBER_STAMP,
                        "standalone_page_number",
                        stripped,
                    )
                    continue
                if _SECTION_PAGE_STAMP_RE.match(stripped):
                    _record_removal(
                        ln_no,
                        pg_no,
                        NoiseCategory.PAGE_NUMBER_STAMP,
                        "section_page_stamp",
                        stripped,
                    )
                    continue
                if _PAGE_OF_LINE_RE.match(stripped):
                    _record_removal(
                        ln_no,
                        pg_no,
                        NoiseCategory.PAGE_NUMBER_STAMP,
                        "page_of_stamp",
                        stripped,
                    )
                    continue

            # 3. Web-printed PDF URL+date footers (at page edges ONLY) & HTML nav links
            if cfg.strip_web_nav and not (cfg.protect_normative_lines and is_normative):
                if _WEB_PRINT_FOOTER_RE.match(stripped) and (pg_no, ln_no) in page_edge_positions:
                    _record_removal(
                        ln_no,
                        pg_no,
                        NoiseCategory.WEB_PRINT_NAVIGATION,
                        "web_print_url_footer",
                        stripped,
                    )
                    continue
                if _WEB_NAV_LINK_RE.match(stripped):
                    _record_removal(
                        ln_no,
                        pg_no,
                        NoiseCategory.WEB_PRINT_NAVIGATION,
                        "html_nav_link",
                        stripped,
                    )
                    continue

            # 4. Decorative divider rules
            if cfg.strip_decorative_rules and _DECORATIVE_RULE_RE.match(stripped):
                _record_removal(
                    ln_no,
                    pg_no,
                    NoiseCategory.DECORATIVE_RULE,
                    "decorative_separator",
                    stripped,
                )
                continue

            # 5. Copyright, confidentiality banners & blank-page notices
            if (
                cfg.strip_copyright_legal
                and len(stripped) <= 180
                and _COPYRIGHT_LEGAL_RE.search(stripped)
                and not (cfg.protect_normative_lines and is_normative)
                and not _looks_like_body_sentence(stripped)
            ):
                _record_removal(
                    ln_no,
                    pg_no,
                    NoiseCategory.COPYRIGHT_AND_LEGAL,
                    "copyright_or_legal_notice",
                    stripped,
                )
                continue

            # 6. Precision TOC / List of Figures / List of Tables block state machine
            if cfg.strip_toc_blocks:
                if _TOC_HEADING_RE.match(stripped) or _TOC_COL_HEADER_RE.match(stripped):
                    in_toc_block = True
                    in_rev_block = False
                    _record_removal(
                        ln_no,
                        pg_no,
                        NoiseCategory.TOC_AND_LISTS,
                        "toc_block_heading",
                        stripped,
                    )
                    continue
                if _TOC_DOT_LEADER_RE.search(stripped) and not (
                    cfg.protect_normative_lines and is_normative
                ):
                    in_toc_block = True
                    sec_num = _extract_section_number(stripped)
                    if sec_num:
                        toc_seen_sections.add(sec_num)
                    _record_removal(
                        ln_no,
                        pg_no,
                        NoiseCategory.TOC_AND_LISTS,
                        "toc_dotted_leader",
                        stripped,
                    )
                    continue
                if in_toc_block:
                    has_explicit_toc_page = bool(
                        _TOC_TRAILING_PAGE_RE.match(stripped) or _TOC_DOT_LEADER_RE.search(stripped)
                    )
                    sec_num = _extract_section_number(stripped)

                    # Unambiguous right-aligned TOC page-numbered entry (even if heading quotes "must"/"shall"!)
                    if has_explicit_toc_page:
                        if sec_num:
                            toc_seen_sections.add(sec_num)
                        _record_removal(
                            ln_no,
                            pg_no,
                            NoiseCategory.TOC_AND_LISTS,
                            "toc_numbered_entry",
                            stripped,
                            count_as_normative_risk=False,
                        )
                        continue
                    # Wrapped 2-line TOC entry whose next line ends in the right-aligned page number
                    if sec_num and _next_nonempty_has_toc_page_num(idx):
                        toc_seen_sections.add(sec_num)
                        _record_removal(
                            ln_no,
                            pg_no,
                            NoiseCategory.TOC_AND_LISTS,
                            "toc_wrapped_entry",
                            stripped,
                            count_as_normative_risk=False,
                        )
                        continue
                    # Duplicate-section body-start detector: if a section number from the TOC repeats
                    # WITHOUT a trailing page number, the actual document body has begun!
                    if sec_num and sec_num in toc_seen_sections:
                        in_toc_block = False
                        seen_body_start = True
                    elif cfg.protect_normative_lines and is_normative:
                        in_toc_block = False
                    elif _REV_HISTORY_HEADING_RE.match(stripped):
                        in_toc_block = False
                    elif _BODY_SECTION_START_RE.match(stripped) and _heading_starts_body(idx):
                        in_toc_block = False
                        seen_body_start = True
                    elif _looks_like_body_sentence(stripped):
                        in_toc_block = False
                    elif _TOC_SHORT_ENTRY_RE.match(stripped) and not _heading_starts_body(idx):
                        if sec_num:
                            toc_seen_sections.add(sec_num)
                        _record_removal(
                            ln_no,
                            pg_no,
                            NoiseCategory.TOC_AND_LISTS,
                            "toc_entry",
                            stripped,
                        )
                        continue
                    else:
                        in_toc_block = False

            # 7. Revision History / Document Control / Acknowledgements block state machine
            if cfg.strip_revision_history:
                if _REV_HISTORY_HEADING_RE.match(stripped):
                    in_rev_block = True
                    rev_non_blank_consumed = 0
                    _record_removal(
                        ln_no,
                        pg_no,
                        NoiseCategory.REVISION_AND_DOC_HISTORY,
                        "revision_block_heading",
                        stripped,
                    )
                    continue
                if in_rev_block:
                    if (
                        (cfg.protect_normative_lines and is_normative)
                        or _BODY_SECTION_START_RE.match(stripped)
                        or _MAJOR_NUMBERED_HEADING_RE.match(stripped)
                        or rev_non_blank_consumed >= 80
                    ):
                        in_rev_block = False
                    else:
                        rev_non_blank_consumed += 1
                        _record_removal(
                            ln_no,
                            pg_no,
                            NoiseCategory.REVISION_AND_DOC_HISTORY,
                            "revision_block_row",
                            stripped,
                        )
                        continue

            # Detect start of Section 1 / body prose
            if _BODY_SECTION_START_RE.match(stripped) or _looks_like_body_sentence(stripped):
                seen_body_start = True

            # 8. Opening cover-page front-matter (strictly before body start & first 45 non-blank lines)
            if (
                cfg.strip_cover_frontmatter
                and not seen_body_start
                and non_blank_seen <= 45
                and not (cfg.protect_normative_lines and is_normative)
            ):
                if (
                    _COVER_META_RE.match(stripped)
                    or _DATE_ONLY_RE.match(stripped)
                    or _US_ZIP_ADDR_RE.search(stripped)
                    or _is_all_caps_banner(stripped)
                ):
                    _record_removal(
                        ln_no,
                        pg_no,
                        NoiseCategory.COVER_FRONTMATTER,
                        "cover_page_metadata",
                        stripped,
                    )
                    continue

            # Preserve exact leading indentation into Pass 3.5 (table reconstruction & row-num hoisting)
            kept_raw.append(line_txt.rstrip(" \t"))

        # Pass 3.5: Block-level table reconstruction, mid-block row-number hoisting, and whitespace compaction
        kept_stage1, table_rows_total, hoisted_total, ws_saved = self._process_layout_blocks(kept_raw)
        if table_rows_total > 0:
            cat_counts[NoiseCategory.TABLE_RECONSTRUCTION.value] = table_rows_total
        if ws_saved > 0:
            cat_chars[NoiseCategory.EXCESS_WHITESPACE.value] = (
                cat_chars.get(NoiseCategory.EXCESS_WHITESPACE.value, 0) + ws_saved
            )

        # Strip leading / trailing blank lines and sentinels
        while kept_stage1 and kept_stage1[0] in ("", _PAGE_BREAK_MARKER):
            kept_stage1.pop(0)
        while kept_stage1 and kept_stage1[-1] in ("", _PAGE_BREAK_MARKER):
            kept_stage1.pop()

        # Pass 4: Heading-aware PDF hard-wrap & cross-page sentence reflow
        if cfg.reflow_pdf_wraps:
            final_lines, merges = self._reflow_lines(kept_stage1)
            total_reflow = merges + hoisted_total
            if total_reflow > 0:
                cat_counts[NoiseCategory.PDF_REFLOW_NORMALIZATION.value] = total_reflow
        else:
            final_lines = [("" if ln == _PAGE_BREAK_MARKER else ln) for ln in kept_stage1]
            if hoisted_total > 0:
                cat_counts[NoiseCategory.PDF_REFLOW_NORMALIZATION.value] = hoisted_total

        cleaned_text = "\n".join(final_lines)
        cleaned_chars = len(cleaned_text)
        chars_saved = max(0, raw_chars - cleaned_chars)
        reduction_pct = round((chars_saved / raw_chars) * 100.0, 2) if raw_chars > 0 else 0.0
        est_tokens_saved = chars_saved // 4
        est_chunks_before = math.ceil(raw_chars / cfg.chunk_size_chars) if raw_chars > 0 else 0
        est_chunks_after = math.ceil(cleaned_chars / cfg.chunk_size_chars) if cleaned_chars > 0 else 0
        normative_removed = sum(1 for r in removed if r.contains_normative_modal)

        return DocumentCleanResult(
            doc_id=doc_id,
            source_path=source_path,
            source_sha256=source_sha,
            raw_chars=raw_chars,
            cleaned_chars=cleaned_chars,
            chars_saved=chars_saved,
            reduction_pct=reduction_pct,
            raw_lines=raw_line_count,
            cleaned_lines=len(final_lines),
            page_count=page_count,
            est_tokens_saved=est_tokens_saved,
            est_chunks_before=est_chunks_before,
            est_chunks_after=est_chunks_after,
            category_counts=dict(sorted(cat_counts.items())),
            category_chars_saved=dict(sorted(cat_chars.items())),
            normative_removed_count=normative_removed,
            removed_lines=removed,
            cleaned_text=cleaned_text,
        )

    def clean_file(self, path: Path | str) -> DocumentCleanResult:
        """Read a UTF-8 text file and return its deterministic DocumentCleanResult."""
        target = Path(path).resolve()
        if not target.is_file():
            raise FileNotFoundError(f"Input file not found: {target}")
        raw_text = target.read_bytes().decode("utf-8", errors="replace")
        safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", target.stem).strip("._-")[:90] or "doc"
        return self.clean_text(raw_text, doc_id=safe_id, source_path=target.as_posix())

    def clean_batch(
        self,
        paths: Sequence[Path | str],
        out_dir: Path | str,
        *,
        generate_ui: bool = True,
    ) -> BatchCleanSummary:
        """Run deterministic cleaning across multiple files, persist audits, and build Review UI."""
        if not paths:
            raise ValueError("paths must contain at least one input file")

        resolved_out = Path(out_dir).resolve()
        raw_dir = resolved_out / "raw"
        cleaned_dir = resolved_out / "cleaned"
        audits_dir = resolved_out / "audits"
        semantic_dir = resolved_out / "semantic_plans"
        raw_dir.mkdir(parents=True, exist_ok=True)
        cleaned_dir.mkdir(parents=True, exist_ok=True)
        audits_dir.mkdir(parents=True, exist_ok=True)
        semantic_dir.mkdir(parents=True, exist_ok=True)

        from reqhq.ingestion.semantic_chunker import SemanticSRSChunker

        sem_chunker = SemanticSRSChunker()
        results: list[DocumentCleanResult] = []
        raw_texts_by_id: dict[str, str] = {}
        category_totals: dict[str, int] = {}

        for p in paths:
            src_path = Path(p).resolve()
            res = self.clean_file(src_path)
            raw_txt = src_path.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
            raw_texts_by_id[res.doc_id] = raw_txt
            results.append(res)

            _atomic_write_lf(raw_dir / f"{res.doc_id}.raw.txt", raw_txt)
            _atomic_write_lf(cleaned_dir / f"{res.doc_id}.cleaned.txt", res.cleaned_text)
            audit_payload = res.model_dump(mode="json", exclude={"cleaned_text"})
            _atomic_write_lf(
                audits_dir / f"{res.doc_id}.audit.json",
                json.dumps(audit_payload, indent=2, sort_keys=True),
            )
            sem_plan = sem_chunker.plan_document(res.cleaned_text, doc_id=res.doc_id)
            _atomic_write_lf(
                semantic_dir / f"{res.doc_id}.semantic.json",
                json.dumps(sem_plan.model_dump(mode="json", exclude={"promoted_markdown"}), indent=2, sort_keys=True),
            )
            for cat, cnt in res.category_counts.items():
                category_totals[cat] = category_totals.get(cat, 0) + cnt

        total_raw = sum(r.raw_chars for r in results)
        total_clean = sum(r.cleaned_chars for r in results)
        total_saved = sum(r.chars_saved for r in results)
        overall_pct = round((total_saved / total_raw) * 100.0, 2) if total_raw > 0 else 0.0

        ui_path_str: str | None = None
        if generate_ui:
            ui_path = resolved_out / "review_ui.html"
            ui_html = render_cleaning_review_ui(results, raw_texts_by_id=raw_texts_by_id)
            _atomic_write_lf(ui_path, ui_html)
            ui_path_str = ui_path.as_posix()

        summary = BatchCleanSummary(
            total_docs=len(results),
            total_raw_chars=total_raw,
            total_cleaned_chars=total_clean,
            total_chars_saved=total_saved,
            overall_reduction_pct=overall_pct,
            total_est_tokens_saved=sum(r.est_tokens_saved for r in results),
            total_chunks_before=sum(r.est_chunks_before for r in results),
            total_chunks_after=sum(r.est_chunks_after for r in results),
            total_normative_removed=sum(r.normative_removed_count for r in results),
            category_totals=dict(sorted(category_totals.items())),
            review_ui_path=ui_path_str,
            doc_ids=[r.doc_id for r in results],
        )
        _atomic_write_lf(
            resolved_out / "batch_summary.json",
            json.dumps(summary.model_dump(mode="json"), indent=2, sort_keys=True),
        )
        return summary


def select_stratified_files(
    input_path: Path | str,
    *,
    limit: int = 15,
    stratified: bool = True,
) -> list[Path]:
    """Select either a single file or a size-stratified sample of .txt/.md files from a directory."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    target = Path(input_path).resolve()
    if target.is_file():
        return [target]
    if not target.is_dir():
        raise FileNotFoundError(f"Input path not found: {target}")

    candidates = sorted(
        (p for p in target.iterdir() if p.is_file() and p.suffix.lower() in (".txt", ".md")),
        key=lambda p: (p.stat().st_size, p.name),
    )
    if not candidates:
        raise ValueError(f"No .txt or .md files found under {target}")
    if len(candidates) <= limit:
        return candidates
    if not stratified:
        return candidates[:limit]
    if limit == 1:
        return [candidates[len(candidates) // 2]]

    indices = sorted({int(i * (len(candidates) - 1) / (limit - 1)) for i in range(limit)})
    return [candidates[i] for i in indices]


def render_cleaning_review_ui(
    results: Sequence[DocumentCleanResult],
    *,
    raw_texts_by_id: dict[str, str],
    max_preview_lines: int = 1200,
) -> str:
    """Render a zero-dependency interactive HTML studio for manual cleaning & semantic chunk review."""
    from reqhq.ingestion.semantic_chunker import SemanticSRSChunker

    sem_chunker = SemanticSRSChunker()
    docs_payload: list[dict[str, object]] = []
    for res in results:
        raw_lines = raw_texts_by_id.get(res.doc_id, "").split("\n")
        cleaned_lines = res.cleaned_text.split("\n") if res.cleaned_text else []
        removed_map = {r.line_no: r.category.value for r in res.removed_lines}
        sem_plan = sem_chunker.plan_document(res.cleaned_text, doc_id=res.doc_id)
        docs_payload.append(
            {
                "doc_id": res.doc_id,
                "source_path": res.source_path,
                "raw_chars": res.raw_chars,
                "cleaned_chars": res.cleaned_chars,
                "chars_saved": res.chars_saved,
                "reduction_pct": res.reduction_pct,
                "raw_lines_count": res.raw_lines,
                "cleaned_lines_count": res.cleaned_lines,
                "page_count": res.page_count,
                "est_tokens_saved": res.est_tokens_saved,
                "est_chunks_before": res.est_chunks_before,
                "est_chunks_after": res.est_chunks_after,
                "normative_removed_count": res.normative_removed_count,
                "category_counts": res.category_counts,
                "category_chars_saved": res.category_chars_saved,
                "removed_lines": [r.model_dump(mode="json") for r in res.removed_lines],
                "removed_line_map": removed_map,
                "raw_preview": raw_lines[:max_preview_lines],
                "cleaned_preview": cleaned_lines[:max_preview_lines],
                "preview_truncated": len(raw_lines) > max_preview_lines or len(cleaned_lines) > max_preview_lines,
                "semantic_plan": {
                    "total_sections": sem_plan.total_sections,
                    "role_counts": sem_plan.role_counts,
                    "role_chars": sem_plan.role_chars,
                    "total_chunks": sem_plan.total_chunks,
                    "llm_routed_chunks": sem_plan.llm_routed_chunks,
                    "skipped_chunks": sem_plan.skipped_chunks,
                    "llm_routed_chars": sem_plan.llm_routed_chars,
                    "skipped_chars": sem_plan.skipped_chars,
                    "llm_char_reduction_pct": sem_plan.llm_char_reduction_pct,
                    "sections": [
                        {
                            "title": s.title,
                            "heading_path": " > ".join(s.heading_path),
                            "role": s.role.value,
                            "route_to_llm": s.route_to_llm,
                            "char_count": s.char_count,
                            "normative_count": s.normative_sentence_count,
                        }
                        for s in sem_plan.sections[:200]
                    ],
                    "chunks": [
                        {
                            "chunk_id": c.chunk_id,
                            "role": c.role.value,
                            "route_to_llm": c.route_to_llm,
                            "heading_path": " > ".join(c.heading_path),
                            "char_count": c.char_count,
                            "cohesion_score": c.cohesion_score,
                            "top_terms": c.top_terms,
                            "preview": c.text[:420],
                        }
                        for c in sem_plan.chunks[:150]
                    ],
                },
            }
        )

    embedded_json = json.dumps(docs_payload, ensure_ascii=False).replace("</", "<\\/")
    title_text = html.escape(f"ReqHQ PDF Cleaner Verification Studio ({len(results)} Docs)")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title_text}</title>
<style>
  :root {{
    --bg: #0f172a; --panel: #1e293b; --panel-2: #162033; --border: #334155;
    --text: #e2e8f0; --muted: #94a3b8; --accent: #38bdf8;
    --ok: #22c55e; --warn: #f59e0b; --bad: #ef4444;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; background: var(--bg); color: var(--text); }}
  header {{ display: flex; justify-content: space-between; align-items: center; padding: 14px 20px; background: #0b1220; border-bottom: 1px solid var(--border); }}
  header h1 {{ margin: 0; font-size: 18px; font-weight: 700; color: #f8fafc; }}
  .btn {{ background: var(--accent); color: #082f49; border: none; border-radius: 6px; padding: 8px 14px; font-weight: 600; cursor: pointer; font-size: 13px; }}
  .btn:hover {{ opacity: 0.9; }}
  .btn-ok {{ background: var(--ok); color: #052e16; }}
  .btn-warn {{ background: var(--warn); color: #451a03; }}
  .btn-bad {{ background: var(--bad); color: #fff; }}
  .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)); gap: 10px; padding: 12px 20px; background: var(--panel-2); border-bottom: 1px solid var(--border); }}
  .kpi {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }}
  .kpi .lbl {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }}
  .kpi .val {{ font-size: 20px; font-weight: 700; margin-top: 4px; color: #f8fafc; }}
  .kpi.safe .val {{ color: var(--ok); }}
  .kpi.alert .val {{ color: var(--bad); }}
  .layout {{ display: grid; grid-template-columns: 290px 1fr; height: calc(100vh - 134px); }}
  .sidebar {{ border-right: 1px solid var(--border); overflow-y: auto; padding: 10px; background: #111b2e; }}
  .doc-item {{ padding: 10px; border-radius: 7px; margin-bottom: 6px; cursor: pointer; border: 1px solid transparent; background: var(--panel); }}
  .doc-item:hover {{ border-color: var(--accent); }}
  .doc-item.active {{ border-color: var(--accent); background: #1d3250; }}
  .doc-item .name {{ font-size: 13px; font-weight: 600; word-break: break-all; }}
  .doc-item .meta {{ font-size: 11px; color: var(--muted); margin-top: 4px; display: flex; justify-content: space-between; }}
  .badge {{ display: inline-block; padding: 2px 7px; border-radius: 999px; font-size: 10px; font-weight: 700; }}
  .b-pending {{ background: #334155; color: #cbd5e1; }}
  .b-approved {{ background: #14532d; color: #86efac; }}
  .b-flagged {{ background: #7f1d1d; color: #fca5a5; }}
  .b-tuning {{ background: #78350f; color: #fcd34d; }}
  .main {{ display: flex; flex-direction: column; overflow: hidden; }}
  .toolbar {{ padding: 10px 16px; border-bottom: 1px solid var(--border); background: var(--panel-2); display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: space-between; }}
  .tabs {{ display: flex; gap: 6px; }}
  .tab {{ padding: 6px 12px; border-radius: 6px; background: var(--panel); border: 1px solid var(--border); color: var(--text); cursor: pointer; font-size: 12px; font-weight: 600; }}
  .tab.active {{ background: var(--accent); color: #082f49; border-color: var(--accent); }}
  .review-bar {{ display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }}
  .review-bar input {{ background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 6px 10px; border-radius: 6px; width: 220px; font-size: 12px; }}
  .content {{ flex: 1; overflow-y: auto; padding: 14px 16px; }}
  .diff-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
  .pane {{ background: #0b1220; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }}
  .pane h3 {{ margin: 0; padding: 8px 12px; font-size: 12px; background: var(--panel); border-bottom: 1px solid var(--border); }}
  .code-box {{ font-family: Consolas, Menlo, monospace; font-size: 12px; line-height: 1.45; max-height: 68vh; overflow: auto; padding: 8px 0; }}
  .ln {{ display: flex; padding: 0 10px; white-space: pre-wrap; word-break: break-word; }}
  .ln .num {{ width: 46px; flex-shrink: 0; color: #64748b; user-select: none; }}
  .ln.removed {{ background: rgba(239, 68, 68, 0.16); color: #fecaca; }}
  .ln.tbl-row {{ background: rgba(56, 189, 248, 0.10); color: #bae6fd; }}
  .ln .cat-tag {{ font-size: 10px; background: #7f1d1d; color: #fecaca; padding: 1px 5px; border-radius: 4px; margin-right: 6px; flex-shrink: 0; }}
  .rendered-box {{ background: #0b1220; border: 1px solid var(--border); border-radius: 8px; padding: 16px 20px; max-height: 70vh; overflow: auto; font-size: 13px; line-height: 1.6; }}
  .rendered-box table {{ margin: 12px 0; background: #111b2e; border: 1px solid var(--border); }}
  .rendered-box th {{ background: #1e293b; color: #7dd3fc; }}
  .rendered-box td, .rendered-box th {{ border: 1px solid var(--border); padding: 6px 10px; }}
  .pills {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }}
  .pill {{ padding: 4px 10px; border-radius: 999px; font-size: 11px; cursor: pointer; border: 1px solid var(--border); background: var(--panel); color: var(--text); }}
  .pill.active {{ background: var(--accent); color: #082f49; font-weight: 700; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th, td {{ padding: 7px 10px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; }}
  th {{ background: var(--panel); position: sticky; top: 0; }}
  .bar-row {{ display: grid; grid-template-columns: 230px 1fr 110px; gap: 10px; align-items: center; margin-bottom: 8px; font-size: 12px; }}
  .bar-track {{ background: #0b1220; height: 14px; border-radius: 4px; overflow: hidden; border: 1px solid var(--border); }}
  .bar-fill {{ background: var(--accent); height: 100%; }}
  .search-in {{ background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 6px 10px; border-radius: 6px; width: 280px; margin-bottom: 10px; font-size: 12px; }}
  .empty {{ padding: 24px; text-align: center; color: var(--muted); background: var(--panel); border-radius: 8px; }}
</style>
</head>
<body>
<header>
  <h1>ReqHQ PDF Cleaner Verification Studio v2 (Tables + Reflow + Precision TOC)</h1>
  <div>
    <button class="btn" onclick="exportVerdicts()">Export Verdicts JSON</button>
  </div>
</header>
<section class="kpis" id="kpiStrip"></section>
<div class="layout">
  <aside class="sidebar" id="docSidebar"></aside>
  <main class="main">
    <div class="toolbar">
      <div class="tabs">
        <button class="tab active" data-tab="diff" onclick="switchTab('diff')">Side-by-Side Diff</button>
        <button class="tab" data-tab="rendered" onclick="switchTab('rendered')">Rendered Clean View</button>
        <button class="tab" data-tab="semantic" onclick="switchTab('semantic')">Smart Sections &amp; Semantic Chunks</button>
        <button class="tab" data-tab="removed" onclick="switchTab('removed')">Removed Lines Audit</button>
        <button class="tab" data-tab="safety" onclick="switchTab('safety')">Normative Safety Check</button>
        <button class="tab" data-tab="stats" onclick="switchTab('stats')">Rule Breakdown</button>
      </div>
      <div class="review-bar">
        <input id="noteInput" type="text" placeholder="Reviewer note for this doc..." oninput="saveNote()" />
        <button class="btn btn-ok" onclick="setVerdict('approved')">Approve</button>
        <button class="btn btn-warn" onclick="setVerdict('tuning')">Needs Tuning</button>
        <button class="btn btn-bad" onclick="setVerdict('flagged')">Flag False Positive</button>
      </div>
    </div>
    <div class="content" id="mainContent"></div>
  </main>
</div>
<script>
const DOCS = {embedded_json};
const STORAGE_KEY = "reqhq_cleaner_verdicts_v2";
let verdicts = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{{}}");
let activeIdx = 0;
let activeTab = "diff";
let activeCategory = "ALL";
let searchTerm = "";

function esc(s) {{
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}}

function persistVerdicts() {{
  localStorage.setItem(STORAGE_KEY, JSON.stringify(verdicts));
}}

function renderMarkdownTablesHtml(lines) {{
  let out = [];
  let i = 0;
  while (i < lines.length) {{
    if (lines[i].startsWith("| ") && lines[i].endsWith(" |")) {{
      let tbl = [];
      while (i < lines.length && lines[i].startsWith("| ") && lines[i].endsWith(" |")) {{
        tbl.push(lines[i]);
        i++;
      }}
      let htmlTbl = "<table>";
      tbl.forEach((row, rIdx) => {{
        const cells = row.slice(2, -2).split(" | ");
        if (rIdx === 1 && cells.every(c => /^-+$/.test(c.trim()))) return;
        const tag = rIdx === 0 ? "th" : "td";
        htmlTbl += "<tr>" + cells.map(c => `<${{tag}}>${{esc(c)}}</${{tag}}>`).join("") + "</tr>";
      }});
      htmlTbl += "</table>";
      out.push(htmlTbl);
    }} else if (lines[i].trim() === "") {{
      out.push("<br/>");
      i++;
    }} else {{
      out.push(`<div>${{esc(lines[i])}}</div>`);
      i++;
    }}
  }}
  return out.join("\\n");
}}

function renderKPIs() {{
  const totalRaw = DOCS.reduce((a, d) => a + d.raw_chars, 0);
  const totalClean = DOCS.reduce((a, d) => a + d.cleaned_chars, 0);
  const totalSaved = totalRaw - totalClean;
  const pct = totalRaw ? ((totalSaved / totalRaw) * 100).toFixed(1) : "0.0";
  const tokens = DOCS.reduce((a, d) => a + d.est_tokens_saved, 0);
  const cBefore = DOCS.reduce((a, d) => a + d.est_chunks_before, 0);
  const cAfter = DOCS.reduce((a, d) => a + d.est_chunks_after, 0);
  const norm = DOCS.reduce((a, d) => a + d.normative_removed_count, 0);
  const reviewed = DOCS.filter(d => verdicts[d.doc_id] && verdicts[d.doc_id].status).length;
  document.getElementById("kpiStrip").innerHTML = `
    <div class="kpi"><div class="lbl">Trial Documents</div><div class="val">${{DOCS.length}}</div></div>
    <div class="kpi"><div class="lbl">Context Saved</div><div class="val">${{pct}}%</div></div>
    <div class="kpi"><div class="lbl">Chars Raw &rarr; Clean</div><div class="val">${{(totalRaw/1000).toFixed(0)}}K &rarr; ${{(totalClean/1000).toFixed(0)}}K</div></div>
    <div class="kpi"><div class="lbl">Est. Tokens Saved</div><div class="val">${{tokens.toLocaleString()}}</div></div>
    <div class="kpi"><div class="lbl">8K Chunks Saved</div><div class="val">${{cBefore}} &rarr; ${{cAfter}} (-${{cBefore - cAfter}})</div></div>
    <div class="kpi ${{norm === 0 ? 'safe' : 'alert'}}"><div class="lbl">Normative Lines Removed</div><div class="val">${{norm}}</div></div>
    <div class="kpi"><div class="lbl">Human Verified</div><div class="val">${{reviewed}} / ${{DOCS.length}}</div></div>
  `;
}}

function renderSidebar() {{
  const el = document.getElementById("docSidebar");
  el.innerHTML = DOCS.map((d, i) => {{
    const v = (verdicts[d.doc_id] && verdicts[d.doc_id].status) || "pending";
    return `<div class="doc-item ${{i === activeIdx ? 'active' : ''}}" onclick="selectDoc(${{i}})">
      <div class="name">${{esc(d.doc_id)}}</div>
      <div class="meta">
        <span>-${{d.reduction_pct}}% (${{d.est_chunks_before}}&rarr;${{d.est_chunks_after}} ch)</span>
        <span class="badge b-${{v}}">${{v.toUpperCase()}}</span>
      </div>
    </div>`;
  }}).join("");
}}

function selectDoc(i) {{
  activeIdx = i;
  activeCategory = "ALL";
  searchTerm = "";
  const doc = DOCS[activeIdx];
  document.getElementById("noteInput").value = (verdicts[doc.doc_id] && verdicts[doc.doc_id].note) || "";
  renderSidebar();
  renderMain();
}}

function switchTab(t) {{
  activeTab = t;
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b.dataset.tab === t));
  renderMain();
}}

function setVerdict(status) {{
  const doc = DOCS[activeIdx];
  verdicts[doc.doc_id] = {{
    status,
    note: document.getElementById("noteInput").value || "",
    updated_at: new Date().toISOString()
  }};
  persistVerdicts();
  renderKPIs();
  renderSidebar();
}}

function saveNote() {{
  const doc = DOCS[activeIdx];
  const cur = verdicts[doc.doc_id] || {{ status: "pending" }};
  cur.note = document.getElementById("noteInput").value || "";
  cur.updated_at = new Date().toISOString();
  verdicts[doc.doc_id] = cur;
  persistVerdicts();
}}

function renderMain() {{
  const doc = DOCS[activeIdx];
  const el = document.getElementById("mainContent");
  if (!doc) {{ el.innerHTML = '<div class="empty">No documents loaded.</div>'; return; }}

  if (activeTab === "diff") {{
    const rawHtml = doc.raw_preview.map((line, idx) => {{
      const ln = idx + 1;
      const cat = doc.removed_line_map[ln];
      const tag = cat ? `<span class="cat-tag">${{esc(cat)}}</span>` : "";
      return `<div class="ln ${{cat ? 'removed' : ''}}"><span class="num">${{ln}}</span>${{tag}}<span>${{esc(line)}}</span></div>`;
    }}).join("");
    const cleanHtml = doc.cleaned_preview.map((line, idx) => {{
      const isTbl = line.startsWith("| ") && line.endsWith(" |");
      return `<div class="ln ${{isTbl ? 'tbl-row' : ''}}"><span class="num">${{idx + 1}}</span><span>${{esc(line)}}</span></div>`;
    }}).join("");
    const truncNote = doc.preview_truncated ? `<p style="color:var(--warn);font-size:12px;">Preview capped at first ${{doc.raw_preview.length}} lines; full raw &amp; cleaned files are saved in raw/ and cleaned/.</p>` : "";
    el.innerHTML = `${{truncNote}}<div class="diff-grid">
      <div class="pane"><h3>RAW (${{doc.raw_chars.toLocaleString()}} chars, ${{doc.raw_lines_count}} lines, ${{doc.page_count}} pages)</h3><div class="code-box">${{rawHtml}}</div></div>
      <div class="pane"><h3>CLEANED (${{doc.cleaned_chars.toLocaleString()}} chars, -${{doc.reduction_pct}}%, ${{doc.cleaned_lines_count}} lines)</h3><div class="code-box">${{cleanHtml}}</div></div>
    </div>`;
  }} else if (activeTab === "rendered") {{
    el.innerHTML = `<div class="rendered-box">${{renderMarkdownTablesHtml(doc.cleaned_preview)}}</div>`;
  }} else if (activeTab === "semantic") {{
    const sp = doc.semantic_plan;
    const secRows = sp.sections.map(s => `<tr>
      <td>${{esc(s.heading_path)}}</td>
      <td><span class="badge ${{s.route_to_llm ? 'b-approved' : 'b-pending'}}">${{esc(s.role)}}</span></td>
      <td>${{s.route_to_llm ? 'Send to LLM' : 'Skip (0 LLM)'}}</td>
      <td>${{s.char_count.toLocaleString()}}</td>
      <td>${{s.normative_count}}</td>
    </tr>`).join("");
    const chunkRows = sp.chunks.map(c => `<tr>
      <td><code>${{esc(c.chunk_id)}}</code></td>
      <td><span class="badge ${{c.route_to_llm ? 'b-approved' : 'b-pending'}}">${{esc(c.role)}}</span></td>
      <td>${{esc(c.heading_path)}}</td>
      <td>${{c.char_count.toLocaleString()}} / 3,400</td>
      <td>${{c.cohesion_score.toFixed(2)}}</td>
      <td><code>${{esc(c.top_terms.join(", "))}}</code></td>
      <td><code>${{esc(c.preview)}}</code></td>
    </tr>`).join("");
    el.innerHTML = `<p style="font-size:13px;color:var(--accent);">
      <strong>Semantic Routing Summary:</strong> ${{sp.total_sections}} sections &rarr;
      <strong>${{sp.llm_routed_chunks}} LLM chunks</strong> (${{sp.llm_routed_chars.toLocaleString()}} chars, 0% clipped at 3.4K budget) |
      <strong>${{sp.skipped_chunks}} zero-LLM chunks skipped</strong> (${{sp.skipped_chars.toLocaleString()}} chars = -${{sp.llm_char_reduction_pct}}% extra context saved after cleaning!)
    </p>
    <h3>1. Section-Role Triage (${{sp.total_sections}} sections)</h3>
    <table><thead><tr><th>Section Path</th><th>Assigned Role</th><th>Routing</th><th>Chars</th><th>Normative Modals</th></tr></thead><tbody>${{secRows}}</tbody></table>
    <h3 style="margin-top:18px;">2. Cohesive Semantic Chunks (${{sp.total_chunks}} chunks, target 3,400 chars)</h3>
    <table><thead><tr><th>Chunk ID</th><th>Role</th><th>Heading Path</th><th>Size</th><th>Cohesion</th><th>Top TF-IDF Terms</th><th>Preview</th></tr></thead><tbody>${{chunkRows}}</tbody></table>`;
  }} else if (activeTab === "removed") {{
    const cats = ["ALL", ...Object.keys(doc.category_counts)];
    const pills = cats.map(c => `<button class="pill ${{activeCategory === c ? 'active' : ''}}" onclick="activeCategory='${{c}}';renderMain();">${{esc(c)}} ${{c==='ALL' ? '('+doc.removed_lines.length+')' : '('+(doc.category_counts[c]||0)+')'}}</button>`).join("");
    const rows = doc.removed_lines.filter(r =>
      (activeCategory === "ALL" || r.category === activeCategory) &&
      (!searchTerm || r.text.toLowerCase().includes(searchTerm.toLowerCase()) || r.rule_detail.toLowerCase().includes(searchTerm.toLowerCase()))
    );
    const tbl = rows.length ? `<table><thead><tr><th>Line</th><th>Page</th><th>Category</th><th>Rule</th><th>Removed Text</th></tr></thead><tbody>` +
      rows.slice(0, 600).map(r => `<tr><td>${{r.line_no}}</td><td>${{r.page_no}}</td><td>${{esc(r.category)}}</td><td>${{esc(r.rule_detail)}}</td><td><code>${{esc(r.text)}}</code></td></tr>`).join("") +
      `</tbody></table>` : `<div class="empty">No removed lines match the filter.</div>`;
    el.innerHTML = `<div class="pills">${{pills}}</div>
      <input class="search-in" type="text" placeholder="Search removed lines (e.g. shall, must, REQ)..." value="${{esc(searchTerm)}}" oninput="searchTerm=this.value;renderMain();" />
      ${{tbl}}`;
  }} else if (activeTab === "safety") {{
    const risky = doc.removed_lines.filter(r => r.contains_normative_modal);
    const tocQuoted = doc.removed_lines.filter(r => !r.contains_normative_modal && /\\b(shall|must|should|req-|fr-|nfr-)\\b/i.test(r.text));
    if (!risky.length) {{
      const note = tocQuoted.length ? `<p style="color:var(--muted);font-size:12px;">Note: ${{tocQuoted.length}} right-aligned page-numbered TOC heading(s) quoting modal verbs were also removed safely (their full body sections remain intact).</p>` : "";
      el.innerHTML = `<div class="empty" style="color:var(--ok);">PASS: Zero non-TOC normative lines (shall/must/REQ-/FR-/NFR-) were removed.</div>${{note}}`;
    }} else {{
      el.innerHTML = `<p style="color:var(--warn);">Inspect these ${{risky.length}} removed line(s) mentioning modal/ID keywords:</p>
        <table><thead><tr><th>Line</th><th>Page</th><th>Category</th><th>Rule</th><th>Text</th></tr></thead><tbody>` +
        risky.map(r => `<tr><td>${{r.line_no}}</td><td>${{r.page_no}}</td><td>${{esc(r.category)}}</td><td>${{esc(r.rule_detail)}}</td><td><code>${{esc(r.text)}}</code></td></tr>`).join("") +
        `</tbody></table>`;
    }}
  }} else {{
    const entries = Object.entries(doc.category_chars_saved);
    const maxVal = Math.max(1, ...entries.map(([, v]) => v));
    const bars = entries.map(([k, v]) => `
      <div class="bar-row">
        <div>${{esc(k)}} (${{doc.category_counts[k] || 0}} lines)</div>
        <div class="bar-track"><div class="bar-fill" style="width:${{Math.min(100, (v/maxVal)*100).toFixed(1)}}%"></div></div>
        <div>${{v.toLocaleString()}} chars</div>
      </div>`).join("");
    el.innerHTML = `<h3>Per-Rule Reduction Breakdown for ${{esc(doc.doc_id)}}</h3>${{bars || '<div class="empty">No noise removed.</div>'}}`;
  }}
}}

function exportVerdicts() {{
  const payload = {{
    exported_at: new Date().toISOString(),
    total_docs: DOCS.length,
    verdicts: verdicts
  }};
  const blob = new Blob([JSON.stringify(payload, null, 2)], {{ type: "application/json" }});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "human_review_verdicts.json";
  a.click();
}}

renderKPIs();
renderSidebar();
if (DOCS.length) selectDoc(0);
</script>
</body>
</html>
"""


__all__ = [
    "BatchCleanSummary",
    "CleanerConfig",
    "DocumentCleanResult",
    "NoiseCategory",
    "PDFTextCleaner",
    "RemovedLineAudit",
    "render_cleaning_review_ui",
    "select_stratified_files",
]
