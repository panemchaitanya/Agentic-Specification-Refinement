"""Strict Pydantic v2 schemas for requirements graphs and traceability."""

from __future__ import annotations

from collections import deque
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256_LEN = 64


class RequirementKind(str, Enum):
    """Classification of a requirement node."""

    FUNCTIONAL = "functional"
    NON_FUNCTIONAL = "non_functional"
    CONSTRAINT = "constraint"
    INTERFACE = "interface"
    SECURITY = "security"
    DATA = "data"
    PROCESS = "process"


class RequirementStatus(str, Enum):
    """Lifecycle status of a requirement node."""

    DRAFT = "draft"
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    VERIFIED = "verified"
    DEPRECATED = "deprecated"
    SUPERSEDED = "superseded"


class EdgeKind(str, Enum):
    """Directed relationship between two requirement nodes."""

    DEPENDS_ON = "depends_on"
    REFINES = "refines"
    CONFLICTS_WITH = "conflicts_with"
    SATISFIES = "satisfies"
    DERIVES_FROM = "derives_from"


_ACYCLIC_RELATIONS: frozenset[EdgeKind] = frozenset(
    {
        EdgeKind.DEPENDS_ON,
        EdgeKind.REFINES,
        EdgeKind.SATISFIES,
        EdgeKind.DERIVES_FROM,
    }
)


class TraceLink(BaseModel):
    """Immutable cryptographic link back to a source artifact and anchor."""

    model_config = ConfigDict(extra="forbid")

    source_sha256: str = Field(..., min_length=_SHA256_LEN, max_length=_SHA256_LEN)
    source_uri: str = Field(..., min_length=1)
    anchor: str = ""
    excerpt: str = ""
    chunk_id: str | None = None

    @field_validator("source_sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != _SHA256_LEN or any(c not in "0123456789abcdef" for c in normalized):
            raise ValueError("source_sha256 must be a 64-character hex SHA-256 digest")
        return normalized

    @field_validator("source_uri")
    @classmethod
    def _validate_source_uri(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("source_uri must not be blank or whitespace-only")
        return cleaned


class RequirementNode(BaseModel):
    """Single atomic requirement in a RequirementGraph."""

    model_config = ConfigDict(extra="forbid")

    req_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    statement: str = Field(..., min_length=1)
    kind: RequirementKind = RequirementKind.FUNCTIONAL
    status: RequirementStatus = RequirementStatus.DRAFT
    priority: Literal["P0", "P1", "P2", "P3"] = "P1"
    acceptance_criteria: list[str] = Field(default_factory=list)
    trace_links: list[TraceLink] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    rationale: str = ""

    @field_validator("req_id", "title", "statement")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be blank or whitespace-only")
        return cleaned


class RequirementEdge(BaseModel):
    """Directed edge connecting two RequirementNodes."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(..., min_length=1)
    target_id: str = Field(..., min_length=1)
    relation: EdgeKind = EdgeKind.DEPENDS_ON
    rationale: str = ""

    @field_validator("source_id", "target_id")
    @classmethod
    def _strip_edge_ids(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Edge endpoint ID must not be blank or whitespace-only")
        return cleaned

    @model_validator(mode="after")
    def _reject_self_loop(self) -> RequirementEdge:
        if self.source_id == self.target_id:
            raise ValueError(f"Self-loop edge is forbidden on requirement {self.source_id!r}")
        return self


class RequirementGraph(BaseModel):
    """Directed graph of requirements with reference, DAG, and traceability checks."""

    model_config = ConfigDict(extra="forbid")

    graph_id: str = Field(..., min_length=1)
    version: str = "1.0.0"
    nodes: list[RequirementNode] = Field(default_factory=list)
    edges: list[RequirementEdge] = Field(default_factory=list)

    @field_validator("graph_id", "version")
    @classmethod
    def _strip_graph_meta(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be blank or whitespace-only")
        return cleaned

    @model_validator(mode="after")
    def _validate_structure(self) -> RequirementGraph:
        seen: set[str] = set()
        for node in self.nodes:
            if node.req_id in seen:
                raise ValueError(f"Duplicate requirement ID in graph: {node.req_id!r}")
            seen.add(node.req_id)

        ref_errors = self.validate_reference_integrity()
        if ref_errors:
            raise ValueError("; ".join(ref_errors))
        return self

    def get_node(self, req_id: str) -> RequirementNode | None:
        """Return the RequirementNode for req_id, or None if absent."""
        for node in self.nodes:
            if node.req_id == req_id:
                return node
        return None

    def validate_reference_integrity(self) -> list[str]:
        """Return errors for any edges referencing non-existent requirement IDs."""
        node_ids = {node.req_id for node in self.nodes}
        errors: list[str] = []
        for idx, edge in enumerate(self.edges):
            if edge.source_id not in node_ids:
                errors.append(
                    f"Edge[{idx}] source_id {edge.source_id!r} not found in graph nodes"
                )
            if edge.target_id not in node_ids:
                errors.append(
                    f"Edge[{idx}] target_id {edge.target_id!r} not found in graph nodes"
                )
        return errors

    def _filtered_adjacency(
        self, relations: set[EdgeKind] | frozenset[EdgeKind] | None = None
    ) -> dict[str, list[str]]:
        allowed = _ACYCLIC_RELATIONS if relations is None else relations
        adjacency: dict[str, list[str]] = {node.req_id: [] for node in self.nodes}
        for edge in self.edges:
            if edge.relation in allowed and edge.source_id in adjacency and edge.target_id in adjacency:
                adjacency[edge.source_id].append(edge.target_id)
        return adjacency

    def topological_order(
        self, relations: set[EdgeKind] | frozenset[EdgeKind] | None = None
    ) -> list[str]:
        """Return a deterministic topological ordering of node IDs, or raise ValueError on cycles."""
        adjacency = self._filtered_adjacency(relations)
        in_degree: dict[str, int] = {node_id: 0 for node_id in adjacency}
        for targets in adjacency.values():
            for target in targets:
                in_degree[target] += 1

        ready = deque(sorted(node_id for node_id, deg in in_degree.items() if deg == 0))
        order: list[str] = []

        while ready:
            current = ready.popleft()
            order.append(current)
            for neighbor in sorted(adjacency[current]):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    ready.append(neighbor)

        if len(order) != len(adjacency):
            cycles = self.find_cycles(relations)
            raise ValueError(f"RequirementGraph contains cycle(s): {cycles}")
        return order

    def find_cycles(
        self, relations: set[EdgeKind] | frozenset[EdgeKind] | None = None
    ) -> list[list[str]]:
        """Find directed cycles among the selected edge relations."""
        adjacency = self._filtered_adjacency(relations)
        state: dict[str, int] = {node_id: 0 for node_id in adjacency}  # 0=unvisited,1=visiting,2=done
        stack: list[str] = []
        cycles: list[list[str]] = []

        def _dfs(node_id: str) -> None:
            state[node_id] = 1
            stack.append(node_id)
            for neighbor in sorted(adjacency[node_id]):
                if state[neighbor] == 1:
                    start = stack.index(neighbor)
                    cycles.append(stack[start:] + [neighbor])
                elif state[neighbor] == 0:
                    _dfs(neighbor)
            stack.pop()
            state[node_id] = 2

        for node_id in sorted(adjacency):
            if state[node_id] == 0:
                _dfs(node_id)
        return cycles

    def is_dag(
        self, relations: set[EdgeKind] | frozenset[EdgeKind] | None = None
    ) -> bool:
        """Return True iff the selected dependency edges form a DAG."""
        return len(self.find_cycles(relations)) == 0

    def validate_dag(
        self, relations: set[EdgeKind] | frozenset[EdgeKind] | None = None
    ) -> list[str]:
        """Return cycle error messages (empty list when graph is acyclic)."""
        cycles = self.find_cycles(relations)
        return [f"Cycle detected: {' -> '.join(cycle)}" for cycle in cycles]

    def validate_traceability(self, *, require_all: bool = True) -> list[str]:
        """Check that requirement nodes carry source TraceLinks.

        When require_all=True, every non-deprecated node must have >= 1 TraceLink.
        When require_all=False, only ACCEPTED and VERIFIED nodes require TraceLinks.
        """
        errors: list[str] = []
        for node in self.nodes:
            if node.status in (RequirementStatus.DEPRECATED, RequirementStatus.SUPERSEDED):
                continue
            needs_trace = require_all or node.status in (
                RequirementStatus.ACCEPTED,
                RequirementStatus.VERIFIED,
            )
            if needs_trace and not node.trace_links:
                errors.append(
                    f"Requirement {node.req_id!r} ({node.status.value}) has no trace_links"
                )
        return errors

    def validate_all(self, *, require_all_traceability: bool = True) -> dict[str, list[str]]:
        """Run reference integrity, DAG, and traceability checks together."""
        return {
            "reference_integrity": self.validate_reference_integrity(),
            "dag": self.validate_dag(),
            "traceability": self.validate_traceability(require_all=require_all_traceability),
        }


__all__ = [
    "EdgeKind",
    "RequirementEdge",
    "RequirementGraph",
    "RequirementKind",
    "RequirementNode",
    "RequirementStatus",
    "TraceLink",
]
