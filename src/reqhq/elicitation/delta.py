"""Delta dependency tracker for subgraph-scoped invalidation and selective re-analysis."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from reqhq.elicitation.models import (
    CriticFinding,
    ElicitationQuestion,
    ElicitationState,
    UserAnswer,
)
from reqhq.schemas.requirements import EdgeKind, RequirementGraph


class DeltaDependencyTracker:
    """Tracks req <-> finding, question -> reqs, turn -> reqs, and graph edge coupling."""

    def __init__(self) -> None:
        self._req_to_findings: dict[str, set[str]] = {}
        self._finding_to_req: dict[str, str] = {}
        self._question_to_reqs: dict[str, set[str]] = {}
        self._question_to_findings: dict[str, set[str]] = {}
        self._turn_to_reqs: dict[str, set[str]] = {}

    @classmethod
    def from_state(cls, state: ElicitationState) -> DeltaDependencyTracker:
        """Build a populated tracker deterministically from an ElicitationState."""
        tracker = cls()
        for finding in state.findings:
            tracker.register_finding(finding)
        for question in state.pending_questions:
            tracker.register_question(question)
        for answer in state.answers:
            tracker.register_answer(answer)
        return tracker

    def register_finding(self, finding: CriticFinding) -> None:
        """Index a req_id <-> finding_id link."""
        self._req_to_findings.setdefault(finding.req_id, set()).add(finding.finding_id)
        self._finding_to_req[finding.finding_id] = finding.req_id
        if finding.resolved_by_turn:
            self._turn_to_reqs.setdefault(finding.resolved_by_turn, set()).add(finding.req_id)

    def register_question(self, question: ElicitationQuestion) -> None:
        """Index a question_id -> req_ids and question_id -> finding_ids mapping."""
        req_set = self._question_to_reqs.setdefault(question.question_id, set())
        req_set.update(question.req_ids)
        finding_set = self._question_to_findings.setdefault(question.question_id, set())
        finding_set.update(question.finding_ids)
        for fid in question.finding_ids:
            req_id = self._finding_to_req.get(fid)
            if req_id:
                req_set.add(req_id)

    def register_answer(
        self,
        answer: UserAnswer,
        question: ElicitationQuestion | None = None,
    ) -> set[str]:
        """Record which req_ids a UserAnswer touched and return them."""
        if question is not None:
            self.register_question(question)
        touched = set(self._question_to_reqs.get(answer.question_id, set()))
        if touched:
            self._turn_to_reqs.setdefault(answer.turn_id, set()).update(touched)
        return touched

    def findings_for_req(self, req_id: str) -> set[str]:
        """Return all tracked finding IDs associated with req_id."""
        return set(self._req_to_findings.get(req_id, set()))

    def req_for_finding(self, finding_id: str) -> str | None:
        """Return the req_id owning finding_id, or None if untracked."""
        return self._finding_to_req.get(finding_id)

    def reqs_for_question(self, question_id: str) -> set[str]:
        """Return all req_ids linked to question_id."""
        return set(self._question_to_reqs.get(question_id, set()))

    def findings_for_question(self, question_id: str) -> set[str]:
        """Return all finding_ids linked to question_id."""
        return set(self._question_to_findings.get(question_id, set()))

    def reqs_for_turn(self, turn_id: str) -> set[str]:
        """Return all req_ids directly modified during turn_id."""
        return set(self._turn_to_reqs.get(turn_id, set()))

    def compute_impacted_req_ids(
        self,
        graph: RequirementGraph,
        changed_req_ids: Iterable[str],
        *,
        directed: bool = False,
    ) -> set[str]:
        """Compute the minimal impacted subgraph for changed_req_ids.

        Untouched disconnected requirements are never included in the returned set.
        When ``directed=False`` (default), traverses connected RequirementEdge
        subgraphs so mutually dependent/refining/conflicting nodes are re-checked.
        When ``directed=True``, follows downstream dependents (incoming DEPENDS_ON,
        REFINES, DERIVES_FROM, SATISFIES) plus bidirectional CONFLICTS_WITH.
        """
        valid_ids = {node.req_id for node in graph.nodes}
        seeds = sorted(req_id for req_id in set(changed_req_ids) if req_id in valid_ids)
        if not seeds:
            return set()

        adjacency: dict[str, set[str]] = {req_id: set() for req_id in valid_ids}
        for edge in graph.edges:
            if not directed or edge.relation == EdgeKind.CONFLICTS_WITH:
                adjacency[edge.source_id].add(edge.target_id)
                adjacency[edge.target_id].add(edge.source_id)
            else:
                # If source DEPENDS_ON/REFINES/DERIVES_FROM/SATISFIES target, changing target impacts source.
                adjacency[edge.target_id].add(edge.source_id)

        visited: set[str] = set(seeds)
        queue: deque[str] = deque(seeds)
        while queue:
            current = queue.popleft()
            for neighbor in sorted(adjacency[current]):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)

        return visited


__all__ = ["DeltaDependencyTracker"]
