"""Intelligent pre-LLM SRS heading promotion, section-role triage, and TF-IDF semantic chunking."""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum
import math
import re
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from reqhq.schemas.requirements import RequirementNode


class SectionRole(str, Enum):
    """Semantic role of an SRS section for pre-LLM routing decisions."""

    GLOSSARY_ONLY = "GLOSSARY_ONLY"
    BACKGROUND_REFERENCE = "BACKGROUND_REFERENCE"
    CONSTRAINTS_ASSUMPTIONS = "CONSTRAINTS_ASSUMPTIONS"
    REQUIREMENT_DENSE = "REQUIREMENT_DENSE"


class SemanticChunkConfig(BaseModel):
    """Configuration for deterministic heading promotion, section triage, and semantic chunking."""

    model_config = ConfigDict(extra="forbid")

    target_chunk_chars: int = Field(default=3400, ge=400, le=8000)
    min_chunk_chars: int = Field(default=600, ge=100)
    skip_glossary_llm: bool = True
    skip_background_llm: bool = True
    dedup_similarity_threshold: float = Field(default=0.85, ge=0.5, le=1.0)


class TriagedSection(BaseModel):
    """Single structural SRS section classified for pre-LLM routing."""

    model_config = ConfigDict(extra="forbid")

    section_index: int = Field(..., ge=0)
    heading_path: list[str] = Field(default_factory=list)
    title: str = Field(..., min_length=1)
    level: int = Field(..., ge=1, le=6)
    role: SectionRole
    route_to_llm: bool
    char_count: int = Field(..., ge=0)
    normative_sentence_count: int = Field(..., ge=0)
    text: str


class SemanticChunk(BaseModel):
    """Cohesive, prompt-sized chunk produced by heading-aware TF-IDF semantic splitting."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(..., min_length=1)
    chunk_index: int = Field(..., ge=0)
    role: SectionRole
    route_to_llm: bool
    heading_path: list[str] = Field(default_factory=list)
    char_count: int = Field(..., ge=0)
    paragraph_count: int = Field(..., ge=1)
    cohesion_score: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    top_terms: list[str] = Field(default_factory=list)
    text: str


class SemanticChunkPlan(BaseModel):
    """Complete pre-LLM section triage and semantic chunking plan for one cleaned document."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(..., min_length=1)
    promoted_markdown: str
    total_chars: int = Field(..., ge=0)
    total_sections: int = Field(..., ge=0)
    role_counts: dict[str, int] = Field(default_factory=dict)
    role_chars: dict[str, int] = Field(default_factory=dict)
    total_chunks: int = Field(..., ge=0)
    llm_routed_chunks: int = Field(..., ge=0)
    skipped_chunks: int = Field(..., ge=0)
    llm_routed_chars: int = Field(..., ge=0)
    skipped_chars: int = Field(..., ge=0)
    baseline_8k_chunks: int = Field(..., ge=0)
    llm_char_reduction_pct: float = Field(..., ge=0.0, le=100.0, allow_inf_nan=False)
    sections: list[TriagedSection] = Field(default_factory=list)
    chunks: list[SemanticChunk] = Field(default_factory=list)


@runtime_checkable
class TextEmbedder(Protocol):
    """Pluggable vectorization interface (zero-dep TF-IDF default; optional local neural embedder)."""

    def vectorize(self, texts: Sequence[str]) -> list[dict[str, float]]:
        """Return L2-normalized sparse feature vectors for each input text."""
        ...


# --- Compiled patterns & curated Requirements Engineering concept synonym map ---

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_NUM_HEADING_RE = re.compile(
    r"^(\d+(?:\.\d+)*)\.?\s+([A-Z][A-Za-z0-9 /&(),:'\"–-]{1,80})$"
)
_ALPHA_HEADING_RE = re.compile(r"^([A-Z]\d+)\s+([A-Z][A-Za-z0-9 /&(),:'\"–-]{1,75})$")
_CHAPTER_HEADING_RE = re.compile(
    r"^(?:Chapter|Section|Appendix)\s+[A-Z0-9]+(?:\s*[:.-]\s*.+)?$",
    re.IGNORECASE,
)
_NORMATIVE_MODAL_RE = re.compile(
    r"\b(?:shall(?:\s+not)?|must(?:\s+not)?|(?:REQ|FR|NFR|SRS|UC)-\d+)\b",
    re.IGNORECASE,
)
_CLAUSE_VERB_RE = re.compile(
    r"\b(?:should|will|allow|provide|ensure|enables?|supports?|requires?|displays?|stores?)\b",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{1,}")

_STOPWORDS = frozenset(
    {
        "the", "and", "for", "that", "this", "with", "from", "are", "was", "were",
        "will", "shall", "must", "should", "can", "may", "not", "all", "any", "into",
        "have", "has", "had", "been", "being", "their", "there", "these", "those",
        "such", "also", "when", "where", "which", "who", "what", "how", "more", "most",
        "other", "each", "only", "than", "then", "them", "they", "its", "our", "your",
        "use", "used", "using", "system", "software", "application", "document",
    }
)

# Curated RE domain synonym expansion so TF-IDF links synonyms without neural model swaps
_RE_CONCEPT_SYNONYMS: dict[str, str] = {
    "auth": "_re_security",
    "authenticate": "_re_security",
    "authentication": "_re_security",
    "authorize": "_re_security",
    "authorization": "_re_security",
    "login": "_re_security",
    "password": "_re_security",
    "credential": "_re_security",
    "credentials": "_re_security",
    "rbac": "_re_security",
    "mfa": "_re_security",
    "encrypt": "_re_security",
    "encryption": "_re_security",
    "permission": "_re_security",
    "permissions": "_re_security",
    "privilege": "_re_security",
    "privileges": "_re_security",
    "security": "_re_security",
    "latency": "_re_performance",
    "throughput": "_re_performance",
    "response": "_re_performance",
    "concurrent": "_re_performance",
    "scalability": "_re_performance",
    "performance": "_re_performance",
    "sla": "_re_performance",
    "p95": "_re_performance",
    "p99": "_re_performance",
    "backup": "_re_reliability",
    "failover": "_re_reliability",
    "recovery": "_re_reliability",
    "availability": "_re_reliability",
    "uptime": "_re_reliability",
    "reliability": "_re_reliability",
    "fault": "_re_reliability",
    "audit": "_re_audit",
    "trail": "_re_audit",
    "logging": "_re_audit",
    "log": "_re_audit",
    "logs": "_re_audit",
    "compliance": "_re_audit",
    "traceability": "_re_audit",
    "approval": "_re_process",
    "workflow": "_re_process",
    "sop": "_re_process",
    "escalation": "_re_process",
    "governance": "_re_process",
    "policy": "_re_process",
    "training": "_re_training",
    "onboarding": "_re_training",
    "tutorial": "_re_training",
    "documentation": "_re_training",
    "manual": "_re_training",
    "usability": "_re_usability",
    "accessibility": "_re_usability",
    "wcag": "_re_usability",
    "interface": "_re_usability",
    "gui": "_re_usability",
    "navigation": "_re_usability",
}

_GLOSSARY_TITLE_RE = re.compile(
    r"\b(?:glossary|definitions?|acronyms?|abbreviations?|terminology)\b",
    re.IGNORECASE,
)
_BACKGROUND_TITLE_RE = re.compile(
    r"\b(?:purpose|scope|references?|bibliography|overview|introduction|stakeholders?|"
    r"product\s+perspective|product\s+position|user\s+characteristics|user\s+personas?|"
    r"problem\s+statement|document\s+conventions|intended\s+audience|supporting\s+information)\b",
    re.IGNORECASE,
)
_CONSTRAINT_TITLE_RE = re.compile(
    r"\b(?:assumptions?|dependencies|constraints?|operating\s+environment|"
    r"design\s+constraints?|applicable\s+standards|licensing|policies|limitations?)\b",
    re.IGNORECASE,
)
_REQ_DENSE_TITLE_RE = re.compile(
    r"\b(?:requirements?|functional|non[- ]?functional|specific\s+requirements?|use\s+cases?|"
    r"features?|security|access|audit|performance|reliability|usability|availability|"
    r"interfaces?|safety|data\s+model|workflows?|modules?|registration|investigation)\b",
    re.IGNORECASE,
)
_TITLE_CONNECTORS = frozenset(
    {"of", "and", "the", "for", "in", "to", "on", "with", "by", "a", "an", "or", "at", "from", "as", "&", "-", "–"}
)


def _is_standalone_title_heading(line: str, prev_blank: bool, next_blank: bool) -> bool:
    s = line.strip()
    if not s or len(s) > 55 or s.startswith(("|", "- ", "* ", "#")):
        return False
    if s.endswith((".", ",", ";", "?", "!", ":")):
        return False
    if _NORMATIVE_MODAL_RE.search(s) or _CLAUSE_VERB_RE.search(s):
        return False
    words = s.split()
    if not (1 <= len(words) <= 6):
        return False
    content = [w for w in words if w.lower() not in _TITLE_CONNECTORS]
    if not content:
        return False
    cap_ratio = sum(1 for w in content if w[0].isupper()) / len(content)
    return cap_ratio >= 0.8 and (prev_blank or next_blank)


def promote_srs_headings(text: str) -> str:
    """Promote IEEE-830 numbered and standalone SRS headings to Markdown `#` hierarchy.

    Preserves code fences, pipe tables, bullet lists, and normative requirement lines untouched.
    """
    if not text:
        return ""

    lines = text.split("\n")
    out: list[str] = []
    n = len(lines)

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "|", "- ", "* ")):
            out.append(line)
            continue

        prev_blank = idx == 0 or not lines[idx - 1].strip()
        next_blank = idx + 1 >= n or not lines[idx + 1].strip()

        # Never promote normative requirement sentences (e.g., "4. The System must allow...")
        if _NORMATIVE_MODAL_RE.search(stripped) or len(stripped.split()) > 10:
            out.append(line)
            continue

        m_num = _NUM_HEADING_RE.match(stripped)
        if m_num and not stripped.endswith((".", ",", ";")) and not _CLAUSE_VERB_RE.search(stripped):
            num_part = m_num.group(1)
            parts = [p for p in num_part.split(".") if p and p != "0"]
            level = max(1, min(6, len(parts) if parts else 1))
            out.append(f"{'#' * level} {stripped}")
            continue

        if _ALPHA_HEADING_RE.match(stripped) and not stripped.endswith((".", ",", ";")):
            out.append(f"## {stripped}")
            continue

        if _CHAPTER_HEADING_RE.match(stripped) and len(stripped) <= 80:
            out.append(f"# {stripped}")
            continue

        if _is_standalone_title_heading(stripped, prev_blank=prev_blank, next_blank=next_blank):
            out.append(f"## {stripped}")
            continue

        out.append(line)

    return "\n".join(out)


def classify_section_role(title: str, body: str) -> tuple[SectionRole, int]:
    """Classify an SRS section into a routing role and count its normative requirement sentences.

    Safety guarantee: any section containing >= 2 normative modal matches (`shall`/`must`/`REQ-`)
    is NEVER downgraded to GLOSSARY_ONLY or BACKGROUND_REFERENCE.
    """
    normative_count = len(_NORMATIVE_MODAL_RE.findall(body))
    clean_title = title.strip()

    if _GLOSSARY_TITLE_RE.search(clean_title) and normative_count == 0:
        return SectionRole.GLOSSARY_ONLY, normative_count

    if _CONSTRAINT_TITLE_RE.search(clean_title):
        return SectionRole.CONSTRAINTS_ASSUMPTIONS, normative_count

    if _REQ_DENSE_TITLE_RE.search(clean_title) or normative_count >= 2:
        return SectionRole.REQUIREMENT_DENSE, normative_count

    if _BACKGROUND_TITLE_RE.search(clean_title) and normative_count == 0:
        return SectionRole.BACKGROUND_REFERENCE, normative_count

    if normative_count == 1:
        return SectionRole.REQUIREMENT_DENSE, normative_count

    return SectionRole.CONSTRAINTS_ASSUMPTIONS, normative_count


class TFIDFConceptVectorizer:
    """Zero-dependency pure-Python TF-IDF + RE concept synonym vectorizer (<15ms per SRS)."""

    @staticmethod
    def tokenize(text: str) -> list[str]:
        tokens: list[str] = []
        for raw in _TOKEN_RE.findall(text.lower()):
            tok = raw.strip("_-")
            if len(tok) < 2 or tok in _STOPWORDS:
                continue
            tokens.append(tok)
            concept = _RE_CONCEPT_SYNONYMS.get(tok)
            if concept is not None:
                tokens.append(concept)
        return tokens

    def vectorize(self, texts: Sequence[str]) -> list[dict[str, float]]:
        if not texts:
            return []
        tokenized = [self.tokenize(t) for t in texts]
        n_docs = len(tokenized)
        df: dict[str, int] = {}
        for toks in tokenized:
            for term in set(toks):
                df[term] = df.get(term, 0) + 1

        vectors: list[dict[str, float]] = []
        for toks in tokenized:
            if not toks:
                vectors.append({})
                continue
            tf: dict[str, int] = {}
            for term in toks:
                tf[term] = tf.get(term, 0) + 1
            raw_vec: dict[str, float] = {}
            for term, count in tf.items():
                idf = math.log((n_docs + 1.0) / (df[term] + 1.0)) + 1.0
                raw_vec[term] = (1.0 + math.log(count)) * idf
            norm = math.sqrt(sum(v * v for v in raw_vec.values()))
            vectors.append({k: v / norm for k, v in raw_vec.items()})
        return vectors


def cosine_similarity(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
    """Compute cosine similarity in [0.0, 1.0] between two L2-normalized sparse vectors."""
    if not vec_a or not vec_b:
        return 0.0
    if len(vec_a) > len(vec_b):
        vec_a, vec_b = vec_b, vec_a
    dot = sum(val * vec_b[k] for k, val in vec_a.items() if k in vec_b)
    return max(0.0, min(1.0, dot))


def _top_terms_from_vector(vec: dict[str, float], limit: int = 5) -> list[str]:
    ranked = sorted(
        ((k, v) for k, v in vec.items() if not k.startswith("_re_")),
        key=lambda kv: (-kv[1], kv[0]),
    )
    return [k for k, _ in ranked[:limit]]


def _split_into_paragraphs(text: str) -> list[str]:
    """Split section text into atomic paragraph/table units without losing characters."""
    units: list[str] = []
    buf: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            if buf:
                units.append("\n".join(buf))
                buf = []
        else:
            buf.append(line)
    if buf:
        units.append("\n".join(buf))
    return units


def _mean_adjacent_cohesion(vectors: Sequence[dict[str, float]]) -> float:
    if len(vectors) <= 1:
        return 1.0
    sims = [cosine_similarity(vectors[i], vectors[i + 1]) for i in range(len(vectors) - 1)]
    return round(sum(sims) / len(sims), 4)


class SemanticSRSChunker:
    """Heading-aware, section-triaging, TF-IDF cosine-valley semantic chunker."""

    def __init__(
        self,
        config: SemanticChunkConfig | None = None,
        *,
        embedder: TextEmbedder | None = None,
    ) -> None:
        self.config = config if config is not None else SemanticChunkConfig()
        self.embedder: TextEmbedder = embedder if embedder is not None else TFIDFConceptVectorizer()

    def _partition_promoted_sections(self, promoted_md: str) -> list[TriagedSection]:
        if not promoted_md.strip():
            return []

        lines = promoted_md.split("\n")
        sections_raw: list[tuple[list[str], str, int, list[str]]] = []
        heading_stack: list[tuple[int, str]] = []
        cur_title = "Document Front"
        cur_level = 1
        cur_path: list[str] = ["Document Front"]
        buf: list[str] = []

        for line in lines:
            m = _MD_HEADING_RE.match(line.strip())
            if m:
                if buf and "\n".join(buf).strip():
                    sections_raw.append((list(cur_path), cur_title, cur_level, list(buf)))
                level = len(m.group(1))
                title = m.group(2).strip()
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, title))
                cur_path = [h[1] for h in heading_stack]
                cur_title = title
                cur_level = level
                buf = [line]
            else:
                buf.append(line)

        if buf and "\n".join(buf).strip():
            sections_raw.append((list(cur_path), cur_title, cur_level, list(buf)))

        triaged: list[TriagedSection] = []
        cfg = self.config
        for idx, (hpath, title, level, sec_lines) in enumerate(sections_raw):
            sec_text = "\n".join(sec_lines).strip()
            role, norm_cnt = classify_section_role(title, sec_text)
            route_to_llm = True
            if role == SectionRole.GLOSSARY_ONLY and cfg.skip_glossary_llm:
                route_to_llm = False
            elif role == SectionRole.BACKGROUND_REFERENCE and cfg.skip_background_llm:
                route_to_llm = False
            triaged.append(
                TriagedSection(
                    section_index=idx,
                    heading_path=hpath,
                    title=title,
                    level=level,
                    role=role,
                    route_to_llm=route_to_llm,
                    char_count=len(sec_text),
                    normative_sentence_count=norm_cnt,
                    text=sec_text,
                )
            )
        return triaged

    def _split_section_by_semantic_valleys(
        self,
        section: TriagedSection,
    ) -> list[tuple[list[str], float]]:
        """Split a large section at TF-IDF cosine-similarity valleys within target_chunk_chars."""
        target = self.config.target_chunk_chars
        paras = _split_into_paragraphs(section.text)
        if not paras:
            return []

        # Ensure no single giant paragraph exceeds target_chunk_chars (split at sentence/hard bounds)
        bounded_paras: list[str] = []
        for para in paras:
            if len(para) <= target:
                bounded_paras.append(para)
            else:
                pos = 0
                while pos < len(para):
                    end = min(len(para), pos + target)
                    if end < len(para):
                        cut = para.rfind(". ", pos + target // 2, end)
                        if cut > pos:
                            end = cut + 2
                    bounded_paras.append(para[pos:end].strip())
                    pos = end

        vectors = self.embedder.vectorize(bounded_paras)
        groups: list[tuple[list[str], float]] = []
        start_idx = 0
        n = len(bounded_paras)

        while start_idx < n:
            cur_len = len(bounded_paras[start_idx])
            end_idx = start_idx + 1
            while end_idx < n and (cur_len + 2 + len(bounded_paras[end_idx])) <= target:
                cur_len += 2 + len(bounded_paras[end_idx])
                end_idx += 1

            # If more paragraphs remain, look for the deepest cosine-similarity valley in [min_fill, end_idx]
            if end_idx < n and (end_idx - start_idx) >= 3:
                min_fill_chars = max(self.config.min_chunk_chars, int(target * 0.45))
                running = 0
                best_split = end_idx
                lowest_sim = 2.0
                for split_at in range(start_idx + 1, end_idx):
                    running += len(bounded_paras[split_at - 1]) + (2 if split_at - 1 > start_idx else 0)
                    if running < min_fill_chars:
                        continue
                    sim = cosine_similarity(vectors[split_at - 1], vectors[split_at])
                    if sim < lowest_sim:
                        lowest_sim = sim
                        best_split = split_at
                end_idx = best_split

            slice_paras = bounded_paras[start_idx:end_idx]
            cohesion = _mean_adjacent_cohesion(vectors[start_idx:end_idx])
            groups.append((slice_paras, cohesion))
            start_idx = end_idx

        return groups

    def plan_document(
        self,
        cleaned_text: str,
        *,
        doc_id: str = "document",
    ) -> SemanticChunkPlan:
        """Promote headings, triage section roles, and emit cohesive prompt-sized semantic chunks."""
        promoted = promote_srs_headings(cleaned_text)
        total_chars = len(promoted)
        sections = self._partition_promoted_sections(promoted)

        role_counts: dict[str, int] = {}
        role_chars: dict[str, int] = {}
        for sec in sections:
            rk = sec.role.value
            role_counts[rk] = role_counts.get(rk, 0) + 1
            role_chars[rk] = role_chars.get(rk, 0) + sec.char_count

        # Pack adjacent small sections of the SAME role + route_to_llm up to target_chunk_chars
        raw_chunk_specs: list[tuple[SectionRole, bool, list[str], list[str], float]] = []
        target = self.config.target_chunk_chars

        for sec in sections:
            if sec.char_count <= target:
                paras = _split_into_paragraphs(sec.text)
                can_pack = (
                    bool(raw_chunk_specs)
                    and raw_chunk_specs[-1][1] == sec.route_to_llm
                    and (
                        sec.route_to_llm
                        or raw_chunk_specs[-1][0] == sec.role
                    )
                    and (len("\n\n".join(raw_chunk_specs[-1][3])) + 2 + sec.char_count) <= target
                )
                if can_pack:
                    prev_role, prev_route, prev_path, prev_paras, _ = raw_chunk_specs[-1]
                    merged_role = (
                        SectionRole.REQUIREMENT_DENSE
                        if (prev_role == SectionRole.REQUIREMENT_DENSE or sec.role == SectionRole.REQUIREMENT_DENSE)
                        else prev_role
                    )
                    merged_paras = [*prev_paras, *paras]
                    merged_path = list(prev_path)
                    if sec.title not in merged_path:
                        merged_path.append(sec.title)
                    vecs = self.embedder.vectorize(merged_paras)
                    raw_chunk_specs[-1] = (
                        merged_role,
                        prev_route,
                        merged_path[:4],
                        merged_paras,
                        _mean_adjacent_cohesion(vecs),
                    )
                else:
                    vecs = self.embedder.vectorize(paras)
                    raw_chunk_specs.append(
                        (sec.role, sec.route_to_llm, list(sec.heading_path), paras, _mean_adjacent_cohesion(vecs))
                    )
            else:
                for slice_paras, cohesion in self._split_section_by_semantic_valleys(sec):
                    raw_chunk_specs.append(
                        (sec.role, sec.route_to_llm, list(sec.heading_path), slice_paras, cohesion)
                    )

        chunk_texts = ["\n\n".join(spec[3]) for spec in raw_chunk_specs]
        chunk_vecs = self.embedder.vectorize(chunk_texts)

        chunks: list[SemanticChunk] = []
        for idx, ((role, route_llm, hpath, paras, cohesion), text, vec) in enumerate(
            zip(raw_chunk_specs, chunk_texts, chunk_vecs, strict=True)
        ):
            chunks.append(
                SemanticChunk(
                    chunk_id=f"{doc_id}-sem-{idx + 1:03d}",
                    chunk_index=idx,
                    role=role,
                    route_to_llm=route_llm,
                    heading_path=hpath,
                    char_count=len(text),
                    paragraph_count=max(1, len(paras)),
                    cohesion_score=cohesion,
                    top_terms=_top_terms_from_vector(vec, limit=5),
                    text=text,
                )
            )

        llm_chunks = [c for c in chunks if c.route_to_llm]
        skipped = [c for c in chunks if not c.route_to_llm]
        llm_chars = sum(c.char_count for c in llm_chunks)
        skipped_chars = sum(c.char_count for c in skipped)
        denom = llm_chars + skipped_chars
        llm_reduction_pct = round((skipped_chars / denom) * 100.0, 2) if denom > 0 else 0.0

        return SemanticChunkPlan(
            doc_id=doc_id,
            promoted_markdown=promoted,
            total_chars=total_chars,
            total_sections=len(sections),
            role_counts=dict(sorted(role_counts.items())),
            role_chars=dict(sorted(role_chars.items())),
            total_chunks=len(chunks),
            llm_routed_chunks=len(llm_chunks),
            skipped_chunks=len(skipped),
            llm_routed_chars=llm_chars,
            skipped_chars=skipped_chars,
            baseline_8k_chunks=math.ceil(total_chars / 8000) if total_chars > 0 else 0,
            llm_char_reduction_pct=llm_reduction_pct,
            sections=sections,
            chunks=chunks,
        )


def cluster_requirements_into_batches(
    nodes: Sequence[RequirementNode],
    *,
    batch_size: int = 5,
    embedder: TextEmbedder | None = None,
) -> list[list[RequirementNode]]:
    """Co-locate semantically closest RequirementNodes into fixed-size critic batches.

    Guarantees an exact lossless partition of ``nodes`` with every batch <= ``batch_size``,
    so cross-section contradictions land in the same 5-node critic prompt at zero extra LLM cost.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    if not nodes:
        return []
    if len(nodes) <= batch_size:
        return [list(nodes)]

    active_embedder = embedder if embedder is not None else TFIDFConceptVectorizer()
    texts = [f"{n.title} {n.statement} {n.kind.value} {' '.join(n.tags)}" for n in nodes]
    vectors = active_embedder.vectorize(texts)

    remaining = list(range(len(nodes)))
    batches: list[list[RequirementNode]] = []

    while remaining:
        seed_idx = remaining.pop(0)
        batch_indices = [seed_idx]
        if remaining and batch_size > 1:
            ranked = sorted(
                remaining,
                key=lambda idx: (-cosine_similarity(vectors[seed_idx], vectors[idx]), idx),
            )
            chosen = ranked[: batch_size - 1]
            chosen_set = set(chosen)
            batch_indices.extend(chosen)
            remaining = [idx for idx in remaining if idx not in chosen_set]
        batches.append([nodes[i] for i in batch_indices])

    return batches


def deduplicate_requirements(
    nodes: Sequence[RequirementNode],
    *,
    similarity_threshold: float = 0.85,
    embedder: TextEmbedder | None = None,
) -> tuple[list[RequirementNode], int]:
    """Merge near-duplicate RequirementNodes while preserving every TraceLink and tag."""
    if not (0.5 <= similarity_threshold <= 1.0):
        raise ValueError("similarity_threshold must be in [0.5, 1.0]")
    if len(nodes) <= 1:
        return list(nodes), 0

    active_embedder = embedder if embedder is not None else TFIDFConceptVectorizer()
    texts = [f"{n.title} {n.statement}" for n in nodes]
    vectors = active_embedder.vectorize(texts)

    kept_nodes: list[RequirementNode] = []
    kept_vectors: list[dict[str, float]] = []
    merged_count = 0

    for node, vec in zip(nodes, vectors, strict=True):
        matched_idx: int | None = None
        for k_idx, k_vec in enumerate(kept_vectors):
            if cosine_similarity(vec, k_vec) >= similarity_threshold:
                matched_idx = k_idx
                break

        if matched_idx is None:
            kept_nodes.append(node)
            kept_vectors.append(vec)
        else:
            merged_count += 1
            existing = kept_nodes[matched_idx]
            existing_anchors = {(tl.source_sha256, tl.anchor) for tl in existing.trace_links}
            new_links = list(existing.trace_links)
            for tl in node.trace_links:
                key = (tl.source_sha256, tl.anchor)
                if key not in existing_anchors:
                    existing_anchors.add(key)
                    new_links.append(tl)
            merged_tags = list(dict.fromkeys([*existing.tags, *node.tags]))
            kept_nodes[matched_idx] = existing.model_copy(
                update={"trace_links": new_links, "tags": merged_tags}
            )

    return kept_nodes, merged_count


__all__ = [
    "SectionRole",
    "SemanticChunk",
    "SemanticChunkConfig",
    "SemanticChunkPlan",
    "SemanticSRSChunker",
    "TFIDFConceptVectorizer",
    "TextEmbedder",
    "TriagedSection",
    "classify_section_role",
    "cluster_requirements_into_batches",
    "cosine_similarity",
    "deduplicate_requirements",
    "promote_srs_headings",
]
