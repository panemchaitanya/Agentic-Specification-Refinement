"""Sequential requirement decomposition for 1-liner prompts and chunked PRDs."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from reqhq.ingestion.chunker import ChunkingResult, DocumentChunk
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
_MAX_GLOSSARY_HEADER_CHARS = 1400
_MAX_CHUNK_EXCERPT_CHARS = 3600

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_HEADING_RE = re.compile(r"^#{1,6}\s+")
_ANCHOR_ONLY_RE = re.compile(r"^\[(?:page|section):[^\]]+\]$")

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
        for chunk in ordered_chunks:
            chunk_nodes, chunk_edges, chunk_id_map, next_index = self._decompose_single_chunk(
                chunk=chunk,
                glossary_header=glossary_header,
                session_id=session_id,
                source_uri=source_uri,
                start_index=next_index,
                seen_statements=seen_statements,
            )
            nodes.extend(chunk_nodes)
            for edge_item in chunk_edges:
                raw_edges.append((edge_item, chunk_id_map))

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
    ) -> tuple[list[RequirementNode], list[_RawEdgeItem], dict[str, str], int]:
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
        else:
            for clause, start, end in _split_clauses_deterministic(chunk.text):
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

        return nodes, edges, id_map, idx

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
    "RequirementDecomposer",
]
