"""Sequential requirement decomposition for 1-liner prompts and chunked PRDs."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from reqhq.ingestion.chunker import ChunkingResult, DocumentChunk
from reqhq.ingestion.semantic_chunker import deduplicate_requirements
from reqhq.llm import LLMRequest, ProviderError, ProviderRouter
from reqhq.schemas.requirements import (
    EdgeKind,
    RequirementEdge,
    RequirementGraph,
    RequirementKind,
    RequirementNode,
    RequirementStatus,
    TraceLink,
)

MAX_DECOMPOSE_PROMPT_CHARS = 5500
MAX_FALLBACK_NODES_PER_CHUNK = 6
MAX_FALLBACK_NODES_TOTAL = 24
_MAX_GLOSSARY_HEADER_CHARS = 1400
_MAX_CHUNK_EXCERPT_CHARS = 3600

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
_PARA_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+")
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*+•▪◦]|\d+[.)])\s+")
_LIST_START_RE = re.compile(
    r"^\s*(?:[-*+•▪◦]\s+|(?:\d+(?:\.\d+)*[.)]|[A-Z]{1,4}-\d+(?:\.\d+)?[:.]|R\d+[:.])\s+)"
)
_INLINE_ROW_NUM_CONT_RE = re.compile(r"^\d+[.)]\s{2,}([a-z].*)$")
_PURE_NUMBER_LINE_RE = re.compile(r"^\d+[.)]?$")
_HEADING_RE = re.compile(r"^#{1,6}\s+")
_ANCHOR_ONLY_RE = re.compile(r"^\[(?:page|section):[^\]]+\]$")
_MD_TABLE_ROW_RE = re.compile(r"^\|.*\|$")
_TOC_LEADER_RE = re.compile(r"\.{4,}|(?:\.\s){4,}")
_PAGE_NUMBER_FOOTER_RE = re.compile(r"\bpage\s+\d+(?:\s+of\s+\d+)?\b", re.IGNORECASE)
_WIDE_TRAILING_PAGE_RE = re.compile(r"\s{3,}\d+\s*$")
_FRONTMATTER_LABEL_RE = re.compile(
    r"^(?:table\s+of\s+contents|s\.?\s*no\.?\s+contents|appendix\s+[a-z]\b|team\s+members\b)",
    re.IGNORECASE,
)
_REQ_MODAL_RE = re.compile(
    r"\b(?:shall|must|should|cannot|will\s+(?:not\s+)?(?:be|have|provide|support|allow|enable|"
    r"enforce|require|store|maintain|validate|verify|send|display|log|handle|process|generate|"
    r"restrict|ensure|operate|use|contain|confirm|manage|track|record|capture|protect|prevent|"
    r"guarantee))\b",
    re.IGNORECASE,
)
_REQ_ACTION_RE = re.compile(
    r"\b(?:requires?|enforces?|supports?|allows?|enables?|provides?|ensures?|validates?|verifies?|"
    r"maintains?|guarantees?|prevents?|restricts?|persists?|logs?|tracks?|records?|monitors?|"
    r"authenticates?|authorizes?|encrypts?|exports?|imports?|notifies?|approves?|assigns?|"
    r"manages?|configures?|audits?|handles?|processes?|publishes?|applies?|apply|"
    r"controls?|facilitates?|automates?)\b",
    re.IGNORECASE,
)

_SECURITY_KW = ("auth", "audit", "encrypt", "rbac", "permission", "approval", "security", "pii", "token")
_NFR_KW = ("latency", "uptime", "sla", "throughput", "p95", "p99", "availability", "performance", "scalab", "ms", "rps")
_PROCESS_KW = ("sop", "runbook", "workflow", "manual", "escalation", "training", "onboarding", "staff", "operator", "role")
_DATA_KW = ("schema", "database", "retention", "backup", "archive", "ledger", "storage", "snapshot")
_INTERFACE_KW = ("api", "webhook", "cli", "endpoint", "dashboard", "ui", "export", "import")
_CONSTRAINT_KW = ("must not", "never", "limit", "budget", "compliance", "gdpr", "hipaa", "offline", "local-first")


class _RawRequirementItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    req_id: str | None = None
    title: str = Field(..., min_length=1)
    statement: str = Field(..., min_length=1)
    kind: RequirementKind = RequirementKind.FUNCTIONAL
    priority: Literal["P0", "P1", "P2", "P3"] = "P1"
    acceptance_criteria: list[str] = Field(default_factory=list)
    excerpt: str = ""
    tags: list[str] = Field(default_factory=list)
    rationale: str = ""


class _RawEdgeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(..., min_length=1)
    target_id: str = Field(..., min_length=1)
    relation: EdgeKind = EdgeKind.DEPENDS_ON
    rationale: str = ""


class _DecompositionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirements: list[_RawRequirementItem] = Field(default_factory=list)
    edges: list[_RawEdgeItem] = Field(default_factory=list)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _infer_kind(text: str) -> RequirementKind:
    low = text.lower()
    if any(kw in low for kw in _SECURITY_KW):
        return RequirementKind.SECURITY
    if any(kw in low for kw in _NFR_KW):
        return RequirementKind.NON_FUNCTIONAL
    if any(kw in low for kw in _PROCESS_KW):
        return RequirementKind.PROCESS
    if any(kw in low for kw in _CONSTRAINT_KW):
        return RequirementKind.CONSTRAINT
    if any(kw in low for kw in _DATA_KW):
        return RequirementKind.DATA
    if any(kw in low for kw in _INTERFACE_KW):
        return RequirementKind.INTERFACE
    return RequirementKind.FUNCTIONAL


def _make_title(clause: str, fallback_idx: int) -> str:
    words = [
        cleaned
        for token in re.sub(r"[^A-Za-z0-9\s-]", " ", clause).split()
        if (cleaned := token.strip("-"))
    ]
    if not words:
        return f"Requirement {fallback_idx}"
    return " ".join(words[:7]).strip().capitalize()[:80]


def _clamp_prompt(text: str, limit: int = MAX_DECOMPOSE_PROMPT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n[truncated-for-budget]"


def _verbatim_span(source: str, candidate: str) -> tuple[str, str]:
    """Return a verbatim excerpt from source and its char-span anchor."""
    cleaned = candidate.strip()
    if cleaned:
        idx = source.find(cleaned)
        if idx >= 0:
            excerpt = source[idx : idx + min(len(cleaned), 240)]
            return excerpt, f"char:{idx}-{idx + len(excerpt)}"
    excerpt = source[: min(len(source), 240)].strip() or source
    return excerpt, f"char:0-{len(excerpt)}"


def _split_clauses_deterministic(text: str) -> list[tuple[str, int, int]]:
    """Extract atomic requirement clauses and their exact character spans from text."""
    results: list[tuple[str, int, int]] = []
    cursor = 0
    for raw_part in _SENTENCE_SPLIT_RE.split(text):
        stripped = raw_part.strip()
        if not stripped:
            continue
        pos = text.find(stripped, cursor)
        cursor = pos + len(stripped)

        if _HEADING_RE.match(stripped) or _ANCHOR_ONLY_RE.match(stripped):
            continue
        cleaned = _BULLET_PREFIX_RE.sub("", stripped).strip()
        if len(cleaned) < 8:
            continue
        results.append((cleaned, pos, pos + len(stripped)))

    # If a single 1-liner sentence mentions multiple clauses joined by "with"/"and", split cleanly.
    if len(results) == 1 and len(results[0][0]) > 40:
        clause, base_start, _ = results[0]
        subparts = [
            p.strip(" ,.")
            for p in re.split(r"\b(?:with|plus|as well as)\b|,\s*(?:and\s+)?", clause, flags=re.IGNORECASE)
            if len(p.strip(" ,.")) >= 12
        ]
        if len(subparts) >= 2:
            expanded: list[tuple[str, int, int]] = []
            search_from = base_start
            for idx, part in enumerate(subparts):
                p_idx = text.find(part, search_from)
                search_from = p_idx + len(part)
                stmt = part if idx == 0 else f"The system shall support {part}."
                expanded.append((stmt, p_idx, p_idx + len(part)))
            return expanded

    if not results and text.strip():
        stripped = text.strip()
        return [(stripped, 0, len(stripped))]
    return results


def _is_noise_line(line: str, *, after_page_break: bool = False) -> bool:
    """Return True for structural/front-matter lines that are not requirements."""
    if _HEADING_RE.match(line) or _ANCHOR_ONLY_RE.match(line) or _MD_TABLE_ROW_RE.match(line):
        return True
    if (
        _TOC_LEADER_RE.search(line)
        or _PAGE_NUMBER_FOOTER_RE.search(line)
        or _WIDE_TRAILING_PAGE_RE.search(line)
    ):
        return True
    has_upper = any(ch.isupper() for ch in line)
    has_lower = any(ch.islower() for ch in line)
    if has_upper and not has_lower:
        return True
    if _FRONTMATTER_LABEL_RE.match(line) and not _REQ_MODAL_RE.search(line):
        return True
    if (
        after_page_break
        and not line.endswith((".", "!", "?", ";"))
        and not _REQ_MODAL_RE.search(line)
    ):
        return True
    return False


def _score_requirement_clause(clause: str) -> int:
    """Score requirement likelihood: 2=normative modal, 1=action/capability, 0=non-requirement."""
    if _REQ_MODAL_RE.search(clause):
        return 2
    if _REQ_ACTION_RE.search(clause):
        return 1
    return 0


def _extract_chunk_fallback_clauses(
    text: str,
    *,
    max_clauses: int = MAX_FALLBACK_NODES_PER_CHUNK,
) -> list[tuple[str, int, int]]:
    """Extract compact, noise-filtered requirement clauses and verbatim spans from a chunk."""
    if max_clauses <= 0:
        return []

    paragraphs: list[tuple[str, int, int]] = []
    para_parts: list[str] = []
    para_start: int | None = None
    para_end = 0
    in_code_fence = False
    after_page_break = False
    offset = 0

    def _flush() -> None:
        nonlocal para_parts, para_start, para_end
        if para_parts and para_start is not None:
            joined = re.sub(r"\s+", " ", " ".join(para_parts)).strip()
            if joined:
                paragraphs.append((joined, para_start, para_end))
        para_parts = []
        para_start = None

    for raw_line in text.splitlines(keepends=True):
        line_start = offset
        offset += len(raw_line)
        if "\x0c" in raw_line:
            _flush()
            after_page_break = True
        body = raw_line.replace("\x0c", "")
        stripped = body.strip()

        if stripped.startswith("```"):
            _flush()
            in_code_fence = not in_code_fence
            after_page_break = False
            continue
        if in_code_fence or not stripped:
            if not in_code_fence:
                _flush()
            continue
        if _PURE_NUMBER_LINE_RE.match(stripped):
            continue
        is_page_header = after_page_break
        after_page_break = False
        if _is_noise_line(stripped, after_page_break=is_page_header):
            _flush()
            continue

        cont_match = (
            _INLINE_ROW_NUM_CONT_RE.match(stripped)
            if para_parts and not para_parts[-1].endswith((".", "!", "?", ";", ":"))
            else None
        )
        if cont_match is not None:
            stripped = cont_match.group(1).strip()
        elif _LIST_START_RE.match(stripped):
            _flush()

        if para_start is None:
            para_start = line_start + (len(body) - len(body.lstrip()))
        para_parts.append(stripped)
        para_end = line_start + len(body.rstrip())
    _flush()

    scored: list[tuple[int, int, str, int, int]] = []
    for para_text, p_start, p_end in paragraphs:
        for sent in _PARA_SENTENCE_SPLIT_RE.split(para_text):
            cleaned = _BULLET_PREFIX_RE.sub("", sent.strip()).strip()
            if not cleaned or not cleaned[0].isalnum():
                continue
            alpha_words = re.findall(r"[A-Za-z]{2,}", cleaned)
            if len(cleaned) < 20 or len(alpha_words) < 4:
                continue
            score = _score_requirement_clause(cleaned)
            if score <= 0:
                continue
            probe = cleaned[: min(len(cleaned), 40)]
            rel = text.find(probe, p_start, p_end)
            if rel >= 0:
                s_start = rel
                s_end = min(p_end, rel + min(len(cleaned), 240))
            else:
                s_start = p_start
                s_end = min(p_end, p_start + 240)
            scored.append((score, len(scored), cleaned, s_start, s_end))

    chosen = [item for item in scored if item[0] == 2][:max_clauses]
    if len(chosen) < max_clauses:
        chosen.extend([item for item in scored if item[0] == 1][: max_clauses - len(chosen)])
    chosen.sort(key=lambda item: item[1])
    return [(stmt, start, end) for _, _, stmt, start, end in chosen]


def _try_parse_payload(raw_text: str, parsed: dict | None) -> _DecompositionPayload | None:
    if parsed is not None:
        try:
            payload = _DecompositionPayload.model_validate(parsed)
            if payload.requirements:
                return payload
        except Exception:
            pass
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict) and "requirements" in obj:
            payload = _DecompositionPayload.model_validate(obj)
            if payload.requirements:
                return payload
    except Exception:
        return None
    return None


class RequirementDecomposer:
    """Decompose 1-liner prompts or chunked PRDs into traceable RequirementGraphs."""

    _SYSTEM_PROMPT = (
        "You are a concise requirements engineer. Decompose the input into atomic, "
        "testable requirements as strict JSON with keys 'requirements' and 'edges'. "
        "Each requirement needs title, statement, kind, priority, acceptance_criteria, "
        "and a verbatim excerpt from the source text."
    )

    def __init__(self, router: ProviderRouter) -> None:
        self.router = router

    def _call_decomposer(
        self,
        prompt_text: str,
        *,
        metadata: dict[str, str],
    ) -> _DecompositionPayload | None:
        request = LLMRequest(
            prompt=_clamp_prompt(prompt_text),
            system_prompt=self._SYSTEM_PROMPT,
            temperature=0.0,
            max_tokens=1536,
            response_schema=_DecompositionPayload,
            metadata=metadata,
        )
        try:
            response = self.router.route(request)
        except ProviderError:
            return None
        return _try_parse_payload(response.text, response.parsed)

    def decompose_prompt(
        self,
        prompt: str,
        *,
        session_id: str,
        source_uri: str = "cli://prompt",
    ) -> RequirementGraph:
        """Decompose a 1-liner or short prompt into a traceable RequirementGraph."""
        cleaned_prompt = prompt.strip()
        if not cleaned_prompt:
            raise ValueError("prompt must not be blank")

        source_sha256 = _sha256_text(cleaned_prompt)
        llm_prompt = (
            f"Session: {session_id}\n"
            f"Source URI: {source_uri}\n"
            f"Input Prompt:\n{cleaned_prompt[:_MAX_CHUNK_EXCERPT_CHARS]}\n\n"
            "Return JSON: {\"requirements\": [...], \"edges\": [...]}."
        )
        payload = self._call_decomposer(
            llm_prompt,
            metadata={"stage": "decompose_prompt", "session_id": session_id},
        )

        nodes: list[RequirementNode] = []
        edges: list[RequirementEdge] = []
        id_map: dict[str, str] = {}

        if payload is not None:
            for idx, item in enumerate(payload.requirements, start=1):
                req_id = f"REQ-{idx:03d}"
                if item.req_id:
                    id_map[item.req_id.strip()] = req_id
                id_map[req_id] = req_id
                excerpt, anchor = _verbatim_span(cleaned_prompt, item.excerpt or item.statement)
                nodes.append(
                    RequirementNode(
                        req_id=req_id,
                        title=item.title,
                        statement=item.statement,
                        kind=item.kind,
                        status=RequirementStatus.DRAFT,
                        priority=item.priority,
                        acceptance_criteria=list(item.acceptance_criteria),
                        trace_links=[
                            TraceLink(
                                source_sha256=source_sha256,
                                source_uri=source_uri,
                                anchor=anchor,
                                excerpt=excerpt,
                                chunk_id=None,
                            )
                        ],
                        tags=list(item.tags),
                        rationale=item.rationale,
                    )
                )
            edges = self._safe_edges(nodes, payload.edges, id_map)
        else:
            clauses = _split_clauses_deterministic(cleaned_prompt)
            for idx, (clause, start, end) in enumerate(clauses, start=1):
                req_id = f"REQ-{idx:03d}"
                excerpt = cleaned_prompt[start:end].strip() or cleaned_prompt[:240]
                nodes.append(
                    RequirementNode(
                        req_id=req_id,
                        title=_make_title(clause, idx),
                        statement=clause,
                        kind=_infer_kind(clause),
                        status=RequirementStatus.DRAFT,
                        priority="P0" if idx == 1 else "P1",
                        acceptance_criteria=[],
                        trace_links=[
                            TraceLink(
                                source_sha256=source_sha256,
                                source_uri=source_uri,
                                anchor=f"prompt:char:{start}-{end}",
                                excerpt=excerpt[:240],
                                chunk_id=None,
                            )
                        ],
                        tags=["decomposed_prompt"],
                        rationale="Deterministic decomposition from user prompt.",
                    )
                )
            if len(nodes) > 1:
                for node in nodes[1:]:
                    edges.append(
                        RequirementEdge(
                            source_id=node.req_id,
                            target_id=nodes[0].req_id,
                            relation=EdgeKind.DEPENDS_ON,
                            rationale="Derived from the same primary prompt context.",
                        )
                    )

        return RequirementGraph(
            graph_id=f"{session_id}-graph",
            nodes=nodes,
            edges=edges,
        )

    def decompose_chunks(
        self,
        chunking_result: ChunkingResult,
        *,
        session_id: str,
        source_uri: str = "file://prd",
        deduplicate_near_duplicates: bool = False,
    ) -> RequirementGraph:
        """Sequentially decompose chunks prepending GlobalGlossary with verbatim TraceLinks."""
        if not chunking_result.chunks:
            return RequirementGraph(graph_id=f"{session_id}-graph", nodes=[], edges=[])

        glossary_header = chunking_result.glossary.to_prompt_header()[:_MAX_GLOSSARY_HEADER_CHARS]
        nodes: list[RequirementNode] = []
        raw_edges: list[tuple[_RawEdgeItem, dict[str, str]]] = []
        seen_statements: set[str] = set()
        next_index = 1

        ordered_chunks = sorted(chunking_result.chunks, key=lambda c: c.chunk_index)
        per_chunk_fallback_limit = max(
            1,
            min(
                MAX_FALLBACK_NODES_PER_CHUNK,
                MAX_FALLBACK_NODES_TOTAL // len(ordered_chunks),
            ),
        )
        fallback_nodes_emitted = 0

        for chunk in ordered_chunks:
            chunk_fallback_budget = min(
                per_chunk_fallback_limit,
                max(0, MAX_FALLBACK_NODES_TOTAL - fallback_nodes_emitted),
            )
            chunk_nodes, chunk_edges, chunk_id_map, next_index, used_fallback = (
                self._decompose_single_chunk(
                    chunk=chunk,
                    glossary_header=glossary_header,
                    session_id=session_id,
                    source_uri=source_uri,
                    start_index=next_index,
                    seen_statements=seen_statements,
                    max_fallback_nodes=chunk_fallback_budget,
                )
            )
            if used_fallback:
                fallback_nodes_emitted += len(chunk_nodes)
            nodes.extend(chunk_nodes)
            for edge_item in chunk_edges:
                raw_edges.append((edge_item, chunk_id_map))

        # Safety net: if every chunk fell back and noise filtering left zero nodes for a
        # non-empty document, extract one verbatim fallback clause from the first chunk.
        if not nodes:
            for chunk in ordered_chunks:
                raw_clauses = _split_clauses_deterministic(chunk.text)
                if raw_clauses:
                    clause, start, end = raw_clauses[0]
                    heading_label = " > ".join(chunk.heading_path) if chunk.heading_path else chunk.chunk_id
                    anchor = chunk.anchors[0] if chunk.anchors else heading_label
                    excerpt = chunk.text[start:end].strip() or chunk.text[:240]
                    nodes.append(
                        RequirementNode(
                            req_id=f"REQ-{next_index:03d}",
                            title=_make_title(clause, next_index),
                            statement=clause,
                            kind=_infer_kind(clause),
                            status=RequirementStatus.DRAFT,
                            priority="P1",
                            acceptance_criteria=[],
                            trace_links=[
                                TraceLink(
                                    source_sha256=chunk.source_sha256,
                                    source_uri=source_uri,
                                    anchor=anchor,
                                    excerpt=excerpt[:240],
                                    chunk_id=chunk.chunk_id,
                                )
                            ],
                            tags=[chunk.chunk_id],
                            rationale=f"Deterministic extraction from {heading_label}.",
                        )
                    )
                    break

        if deduplicate_near_duplicates and len(nodes) > 1:
            nodes, _merged = deduplicate_requirements(nodes, similarity_threshold=0.85)

        global_id_map: dict[str, str] = {n.req_id: n.req_id for n in nodes}
        collected_edges: list[RequirementEdge] = []
        seen_edge_keys: set[tuple[str, str, EdgeKind]] = set()
        for edge_item, local_map in raw_edges:
            merged_map = {**global_id_map, **local_map}
            src = merged_map.get(edge_item.source_id.strip())
            dst = merged_map.get(edge_item.target_id.strip())
            if not src or not dst or src == dst:
                continue
            key = (src, dst, edge_item.relation)
            if key in seen_edge_keys:
                continue
            candidate = RequirementEdge(
                source_id=src,
                target_id=dst,
                relation=edge_item.relation,
                rationale=edge_item.rationale,
            )
            tentative = RequirementGraph(
                graph_id=f"{session_id}-check",
                nodes=nodes,
                edges=[*collected_edges, candidate],
            )
            if tentative.is_dag():
                collected_edges.append(candidate)
                seen_edge_keys.add(key)

        return RequirementGraph(
            graph_id=f"{session_id}-graph",
            nodes=nodes,
            edges=collected_edges,
        )

    def _decompose_single_chunk(
        self,
        *,
        chunk: DocumentChunk,
        glossary_header: str,
        session_id: str,
        source_uri: str,
        start_index: int,
        seen_statements: set[str],
        max_fallback_nodes: int = MAX_FALLBACK_NODES_PER_CHUNK,
    ) -> tuple[list[RequirementNode], list[_RawEdgeItem], dict[str, str], int, bool]:
        heading_label = " > ".join(chunk.heading_path) if chunk.heading_path else chunk.chunk_id
        anchor = chunk.anchors[0] if chunk.anchors else heading_label
        llm_prompt = (
            f"{glossary_header}\n\n"
            f"## Chunk {chunk.chunk_id} ({heading_label})\n"
            f"Anchors: {', '.join(chunk.anchors) if chunk.anchors else anchor}\n\n"
            f"{chunk.text[:_MAX_CHUNK_EXCERPT_CHARS]}\n\n"
            "Extract atomic requirements as JSON: {\"requirements\": [...], \"edges\": [...]}."
        )
        payload = self._call_decomposer(
            llm_prompt,
            metadata={
                "stage": "decompose_chunk",
                "session_id": session_id,
                "chunk_id": chunk.chunk_id,
            },
        )

        nodes: list[RequirementNode] = []
        edges: list[_RawEdgeItem] = []
        id_map: dict[str, str] = {}
        idx = start_index

        if payload is not None:
            for item in payload.requirements:
                norm_stmt = re.sub(r"\s+", " ", item.statement.strip().lower())
                if norm_stmt in seen_statements:
                    continue
                seen_statements.add(norm_stmt)
                req_id = f"REQ-{idx:03d}"
                idx += 1
                if item.req_id:
                    id_map[item.req_id.strip()] = req_id
                id_map[req_id] = req_id
                excerpt, _ = _verbatim_span(chunk.text, item.excerpt or item.statement)
                nodes.append(
                    RequirementNode(
                        req_id=req_id,
                        title=item.title,
                        statement=item.statement,
                        kind=item.kind,
                        status=RequirementStatus.DRAFT,
                        priority=item.priority,
                        acceptance_criteria=list(item.acceptance_criteria),
                        trace_links=[
                            TraceLink(
                                source_sha256=chunk.source_sha256,
                                source_uri=source_uri,
                                anchor=anchor,
                                excerpt=excerpt[:240],
                                chunk_id=chunk.chunk_id,
                            )
                        ],
                        tags=list(item.tags),
                        rationale=item.rationale,
                    )
                )
            edges.extend(payload.edges)
            return nodes, edges, id_map, idx, False

        for clause, start, end in _extract_chunk_fallback_clauses(
            chunk.text,
            max_clauses=max_fallback_nodes,
        ):
            norm_stmt = re.sub(r"\s+", " ", clause.strip().lower())
            if norm_stmt in seen_statements:
                continue
            seen_statements.add(norm_stmt)
            req_id = f"REQ-{idx:03d}"
            idx += 1
            excerpt = chunk.text[start:end].strip() or chunk.text[:240]
            nodes.append(
                RequirementNode(
                    req_id=req_id,
                    title=_make_title(clause, idx - 1),
                    statement=clause,
                    kind=_infer_kind(clause),
                    status=RequirementStatus.DRAFT,
                    priority="P1",
                    acceptance_criteria=[],
                    trace_links=[
                        TraceLink(
                            source_sha256=chunk.source_sha256,
                            source_uri=source_uri,
                            anchor=anchor,
                            excerpt=excerpt[:240],
                            chunk_id=chunk.chunk_id,
                        )
                    ],
                    tags=[chunk.chunk_id],
                    rationale=f"Deterministic extraction from {heading_label}.",
                )
            )

        return nodes, edges, id_map, idx, True

    @staticmethod
    def _safe_edges(
        nodes: list[RequirementNode],
        raw_edges: list[_RawEdgeItem],
        id_map: dict[str, str],
    ) -> list[RequirementEdge]:
        valid_ids = {n.req_id for n in nodes}
        edges: list[RequirementEdge] = []
        seen: set[tuple[str, str, EdgeKind]] = set()
        for item in raw_edges:
            src = id_map.get(item.source_id.strip(), item.source_id.strip())
            dst = id_map.get(item.target_id.strip(), item.target_id.strip())
            if src not in valid_ids or dst not in valid_ids or src == dst:
                continue
            key = (src, dst, item.relation)
            if key in seen:
                continue
            candidate = RequirementEdge(
                source_id=src,
                target_id=dst,
                relation=item.relation,
                rationale=item.rationale,
            )
            check_graph = RequirementGraph(
                graph_id="edge-dag-check",
                nodes=nodes,
                edges=[*edges, candidate],
            )
            if check_graph.is_dag():
                edges.append(candidate)
                seen.add(key)
        return edges


__all__ = [
    "MAX_DECOMPOSE_PROMPT_CHARS",
    "MAX_FALLBACK_NODES_PER_CHUNK",
    "MAX_FALLBACK_NODES_TOTAL",
    "RequirementDecomposer",
]
