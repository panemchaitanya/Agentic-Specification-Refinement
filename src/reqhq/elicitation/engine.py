"""Stage 3 ElicitationEngine: decomposition, delta critic loops, revision, and finalization."""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
from pathlib import Path
import re
from typing import Any

from reqhq.elicitation.convergence import (
    cluster_and_rank_questions,
    deduplicate_findings,
    is_converged,
)
from reqhq.elicitation.critics import CriticPanel
from reqhq.elicitation.decomposer import RequirementDecomposer
from reqhq.elicitation.delta import DeltaDependencyTracker
from reqhq.elicitation.models import (
    AssumptionRecord,
    ConvergenceGateError,
    CriticDisagreement,
    CriticFinding,
    ElicitationState,
    FindingSeverity,
    UserAnswer,
)
from reqhq.ingestion.chunker import ChunkingResult
from reqhq.llm import EscalationReason, ProviderRouter
from reqhq.schemas.requirements import RequirementGraph, RequirementStatus
from reqhq.store.event_log import EventKind, SessionEventLog

_TURN_NUM_RE = re.compile(r"(\d+)$")


def _extract_turn_number(turn_id: str) -> int:
    match = _TURN_NUM_RE.search(turn_id)
    return int(match.group(1)) if match else 1


def _fallback_sha256(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _node_sha256(graph: RequirementGraph, req_ids: Sequence[str], fallback_seed: str) -> str:
    for req_id in req_ids:
        node = graph.get_node(req_id)
        if node is not None and node.trace_links:
            return node.trace_links[0].source_sha256
    return _fallback_sha256(fallback_seed)


class ElicitationEngine:
    """Orchestrates Stage 3 iterative elicitation, 5 local critics, and delta convergence."""

    def __init__(
        self,
        router: ProviderRouter,
        event_log: SessionEventLog,
        *,
        decomposer: RequirementDecomposer | None = None,
        critic_panel: CriticPanel | None = None,
    ) -> None:
        self.router = router
        self.event_log = event_log
        self.decomposer = decomposer or RequirementDecomposer(router)
        self.critic_panel = critic_panel or CriticPanel(router)
        self._glossary_headers: dict[str, str] = {}

    def _state_path(self, session_id: str) -> Path:
        return self.event_log.sessions_dir / session_id / "elicitation_state.json"

    def _glossary_path(self, session_id: str) -> Path:
        return self.event_log.sessions_dir / session_id / "glossary_header.txt"

    def _ensure_session(self, session_id: str) -> None:
        db_path = self.event_log.sessions_dir / session_id / "events.db"
        if not db_path.exists():
            self.event_log.create_session(session_id=session_id)

    def _get_glossary_header(self, session_id: str) -> str:
        if session_id in self._glossary_headers:
            return self._glossary_headers[session_id]
        gpath = self._glossary_path(session_id)
        if gpath.is_file():
            header = gpath.read_text(encoding="utf-8")
            self._glossary_headers[session_id] = header
            return header
        return ""

    def _set_glossary_header(self, session_id: str, header: str) -> None:
        self._glossary_headers[session_id] = header
        if header.strip():
            gpath = self._glossary_path(session_id)
            gpath.parent.mkdir(parents=True, exist_ok=True)
            gpath.write_text(header, encoding="utf-8")

    def save_state(self, session_id: str, state: ElicitationState) -> Path:
        """Persist ElicitationState as strict JSON under <sessions_dir>/<session_id>/."""
        target = self._state_path(session_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        return target

    def load_state(self, session_id: str) -> ElicitationState:
        """Load and strictly validate a persisted ElicitationState from disk."""
        target = self._state_path(session_id)
        if not target.is_file():
            raise FileNotFoundError(f"Elicitation state not found at {target}")
        return ElicitationState.model_validate_json(target.read_text(encoding="utf-8"))

    def start_from_prompt(
        self,
        session_id: str,
        prompt: str,
        *,
        source_uri: str = "cli://prompt",
    ) -> ElicitationState:
        """Decompose a 1-liner/short prompt and initialize Stage 3 ElicitationState."""
        self._ensure_session(session_id)
        graph = self.decomposer.decompose_prompt(
            prompt,
            session_id=session_id,
            source_uri=source_uri,
        )
        stale_ids = [n.req_id for n in graph.nodes]
        state = ElicitationState(
            session_id=session_id,
            graph=graph,
            stale_req_ids=stale_ids,
            converged=len(stale_ids) == 0,
        )
        self.save_state(session_id, state)
        return state

    def start_from_chunks(
        self,
        session_id: str,
        chunking_result: ChunkingResult,
        *,
        source_uri: str = "file://prd",
    ) -> ElicitationState:
        """Decompose a multi-chunk PRD preserving GlobalGlossary and trace links."""
        self._ensure_session(session_id)
        glossary_header = chunking_result.glossary.to_prompt_header()
        self._set_glossary_header(session_id, glossary_header)

        graph = self.decomposer.decompose_chunks(
            chunking_result,
            session_id=session_id,
            source_uri=source_uri,
        )
        stale_ids = [n.req_id for n in graph.nodes]
        state = ElicitationState(
            session_id=session_id,
            graph=graph,
            stale_req_ids=stale_ids,
            converged=len(stale_ids) == 0,
        )
        self.save_state(session_id, state)
        return state

    def run_turn(
        self,
        state: ElicitationState,
        answers: Sequence[UserAnswer | dict[str, Any]] | None = None,
    ) -> ElicitationState:
        """Apply optional UserAnswers, run delta-only critics on stale reqs, and log turn."""
        self._ensure_session(state.session_id)
        working = state.model_copy(deep=True)
        tracker = DeltaDependencyTracker.from_state(working)

        next_turn = working.turn_count + 1
        turn_id = f"turn-{next_turn:03d}"
        answer_source = f"user_turn_{next_turn}"

        question_by_id = {q.question_id: q for q in working.pending_questions}
        finding_by_id = {f.finding_id: f for f in working.findings}

        applied_answers: list[UserAnswer] = []
        changed_req_ids: set[str] = set()

        for raw_ans in answers or []:
            ans = (
                raw_ans
                if isinstance(raw_ans, UserAnswer)
                else UserAnswer.model_validate(
                    {
                        "turn_id": turn_id,
                        "source": answer_source,
                        **raw_ans,
                    }
                )
            )
            normalized_ans = ans.model_copy(
                update={"turn_id": turn_id, "source": answer_source}
            )
            question = question_by_id.get(normalized_ans.question_id)
            target_reqs = self._apply_answer_to_state(
                working,
                normalized_ans,
                question=question,
                finding_by_id=finding_by_id,
            )
            tracker.register_answer(normalized_ans, question=question)
            changed_req_ids.update(target_reqs)
            working.answers.append(normalized_ans)
            applied_answers.append(normalized_ans)

        # Determine minimal delta set to analyze.
        if changed_req_ids:
            impacted = tracker.compute_impacted_req_ids(working.graph, changed_req_ids)
            target_req_ids = sorted(impacted | set(working.stale_req_ids))
        elif working.stale_req_ids:
            target_req_ids = sorted(set(working.stale_req_ids))
        elif working.turn_count == 0:
            target_req_ids = [
                n.req_id
                for n in working.graph.nodes
                if n.status not in (RequirementStatus.DEPRECATED, RequirementStatus.SUPERSEDED)
            ]
        else:
            target_req_ids = []

        disagreements_added = self._evaluate_delta_critics(
            working,
            target_req_ids=target_req_ids,
            turn_id=turn_id,
        )

        working.stale_req_ids = []
        working.pending_questions = cluster_and_rank_questions(working.findings)
        working.converged = is_converged(working.findings)
        working.turn_count = next_turn

        unresolved_blockers = sum(
            1 for f in working.findings if not f.resolved and f.severity == FindingSeverity.BLOCKER
        )
        unresolved_majors = sum(
            1 for f in working.findings if not f.resolved and f.severity == FindingSeverity.MAJOR
        )
        self.event_log.append_event(
            working.session_id,
            EventKind.TURN_COMPLETED,
            turn_id=turn_id,
            payload={
                "stage": "elicit",
                "turn_count": next_turn,
                "evaluated_req_ids": target_req_ids,
                "answers_applied": [a.model_dump(mode="json") for a in applied_answers],
                "disagreements_count": len(disagreements_added),
                "unresolved_blockers": unresolved_blockers,
                "unresolved_majors": unresolved_majors,
                "pending_question_count": len(working.pending_questions),
                "converged": working.converged,
            },
        )
        self.save_state(working.session_id, working)
        return working

    def apply_revision(
        self,
        state: ElicitationState,
        turn_id: str,
        updated_answers: Sequence[UserAnswer | dict[str, Any]],
        *,
        note: str = "",
    ) -> ElicitationState:
        """Revise a previous turn, invalidate downstream turns, and re-run delta critics only."""
        self._ensure_session(state.session_id)
        working = state.model_copy(deep=True)
        tracker = DeltaDependencyTracker.from_state(working)
        turn_num = _extract_turn_number(turn_id)
        revised_source = f"user_turn_{turn_num}"

        normalized_updates: list[UserAnswer] = []
        for raw_ans in updated_answers:
            ans = (
                raw_ans
                if isinstance(raw_ans, UserAnswer)
                else UserAnswer.model_validate(
                    {
                        "turn_id": turn_id,
                        "source": revised_source,
                        **raw_ans,
                    }
                )
            )
            normalized_updates.append(
                ans.model_copy(update={"turn_id": turn_id, "source": revised_source})
            )

        rev_result = self.event_log.revise(
            working.session_id,
            turn_id,
            revised_payload={
                "stage": "elicit",
                "note": note,
                "updated_answers": [a.model_dump(mode="json") for a in normalized_updates],
            },
        )
        invalidated_turns: list[str] = list(rev_result.get("invalidated_turn_ids", []))
        affected_turns: set[str] = {turn_id, *invalidated_turns}

        # Collect all reqs directly touched by revised + invalidated turns and new answers.
        seed_reqs: set[str] = set()
        for tid in affected_turns:
            seed_reqs.update(tracker.reqs_for_turn(tid))

        question_by_id = {q.question_id: q for q in working.pending_questions}
        for ans in working.answers:
            if ans.turn_id in affected_turns:
                seed_reqs.update(tracker.reqs_for_question(ans.question_id))
                if working.graph.get_node(ans.question_id) is not None:
                    seed_reqs.add(ans.question_id)

        for ans in normalized_updates:
            seed_reqs.update(tracker.reqs_for_question(ans.question_id))
            if working.graph.get_node(ans.question_id) is not None:
                seed_reqs.add(ans.question_id)

        # Also check findings resolved by affected turns.
        for finding in working.findings:
            if finding.resolved_by_turn in affected_turns:
                seed_reqs.add(finding.req_id)

        # Also inspect requirement acceptance_criteria tags for affected turn numbers.
        affected_sources = {f"[user_turn_{_extract_turn_number(tid)}]" for tid in affected_turns}
        for node in working.graph.nodes:
            if any(any(tag in ac for tag in affected_sources) for ac in node.acceptance_criteria):
                seed_reqs.add(node.req_id)

        impacted_req_ids = sorted(tracker.compute_impacted_req_ids(working.graph, seed_reqs))
        impacted_set = set(impacted_req_ids)

        # Track reqs directly touched by the revised turn itself before rollback.
        revised_turn_tag = f"[user_turn_{turn_num}]"
        direct_revised_reqs = sorted(
            {f.req_id for f in working.findings if f.resolved_by_turn == turn_id}
            | {
                node.req_id
                for node in working.graph.nodes
                if any(revised_turn_tag in ac for ac in node.acceptance_criteria)
            }
            | tracker.reqs_for_turn(turn_id)
        )

        # Roll back stale answers and acceptance criteria from revised + invalidated turns.
        working.answers = [a for a in working.answers if a.turn_id not in affected_turns]
        for node in working.graph.nodes:
            if node.req_id in impacted_set:
                node.acceptance_criteria = [
                    ac
                    for ac in node.acceptance_criteria
                    if not any(tag in ac for tag in affected_sources)
                ]

        # Reopen findings on impacted reqs resolved by affected turns.
        for finding in working.findings:
            if finding.req_id in impacted_set and finding.resolved_by_turn in affected_turns:
                finding.resolved = False
                finding.resolved_by_turn = None

        finding_by_id = {f.finding_id: f for f in working.findings}
        for ans in normalized_updates:
            question = question_by_id.get(ans.question_id)
            fallback_reqs = direct_revised_reqs or impacted_req_ids
            touched = self._apply_answer_to_state(
                working,
                ans,
                question=question,
                finding_by_id=finding_by_id,
                fallback_req_ids=fallback_reqs if not question else None,
            )
            impacted_set.update(tracker.compute_impacted_req_ids(working.graph, touched))
            working.answers.append(ans)

        final_targets = sorted(impacted_set)
        working.stale_req_ids = list(final_targets)
        self._evaluate_delta_critics(
            working,
            target_req_ids=final_targets,
            turn_id=turn_id,
        )

        working.stale_req_ids = []
        working.pending_questions = cluster_and_rank_questions(working.findings)
        working.converged = is_converged(working.findings)
        working.finalized = False
        working.accepted_defaults = False
        self.save_state(working.session_id, working)
        return working

    def finalize(
        self,
        state: ElicitationState,
        *,
        accept_defaults: bool = False,
    ) -> ElicitationState:
        """Finalize elicitation once converged, or convert open gaps into unconfirmed assumptions."""
        self._ensure_session(state.session_id)
        working = state.model_copy(deep=True)
        converged = is_converged(working.findings)
        working.converged = converged

        unresolved_blocking = [
            f
            for f in working.findings
            if not f.resolved
            and f.severity in (FindingSeverity.BLOCKER, FindingSeverity.MAJOR)
        ]

        if not converged and not accept_defaults:
            blockers = sum(1 for f in unresolved_blocking if f.severity == FindingSeverity.BLOCKER)
            majors = sum(1 for f in unresolved_blocking if f.severity == FindingSeverity.MAJOR)
            raise ConvergenceGateError(
                f"Cannot finalize session '{working.session_id}': "
                f"{blockers} unresolved BLOCKER and {majors} unresolved MAJOR finding(s) remain. "
                "Resolve them or call finalize(accept_defaults=True)."
            )

        if not converged and accept_defaults:
            working.accepted_defaults = True
            assumptions = list(working.assumptions)
            next_idx = len(assumptions) + 1
            for finding in unresolved_blocking:
                req_ids = [finding.req_id]
                source_sha = finding.source_sha256 or _node_sha256(
                    working.graph,
                    req_ids,
                    fallback_seed=f"{working.session_id}:{finding.finding_id}",
                )
                statement = (
                    finding.recommended_default.strip()
                    or f"Assume default resolution for {finding.req_id}: {finding.title}."
                )
                assumptions.append(
                    AssumptionRecord(
                        assumption_id=f"ADR-A{next_idx:03d}",
                        title=f"Unconfirmed Assumption: {finding.title}",
                        statement=statement,
                        req_ids=req_ids,
                        finding_ids=[finding.finding_id],
                        source_sha256=source_sha,
                        rationale=(
                            f"Accepted default during finalize(--accept-defaults) for "
                            f"{finding.critic.value} {finding.severity.value} finding "
                            f"{finding.finding_id}: {finding.description}"
                        ),
                        type="assumption",
                        user_confirmed=False,
                    )
                )
                next_idx += 1
            working.assumptions = assumptions
            working.pending_questions = []

        for node in working.graph.nodes:
            if node.status in (RequirementStatus.DRAFT, RequirementStatus.PROPOSED):
                node.status = RequirementStatus.ACCEPTED

        working.finalized = True
        self.event_log.append_event(
            working.session_id,
            EventKind.TURN_COMPLETED,
            turn_id=None,
            payload={
                "stage": "finalize",
                "converged": working.converged,
                "accepted_defaults": working.accepted_defaults,
                "assumption_count": len(working.assumptions),
                "assumption_ids": [a.assumption_id for a in working.assumptions],
            },
        )
        self.save_state(working.session_id, working)
        return working

    def _apply_answer_to_state(
        self,
        state: ElicitationState,
        answer: UserAnswer,
        *,
        question: Any,
        finding_by_id: dict[str, CriticFinding],
        fallback_req_ids: Sequence[str] | None = None,
    ) -> set[str]:
        target_reqs: set[str] = set()
        target_finding_ids: list[str] = []

        if question is not None:
            target_reqs.update(question.req_ids)
            target_finding_ids.extend(question.finding_ids)
        elif state.graph.get_node(answer.question_id) is not None:
            target_reqs.add(answer.question_id)
        elif fallback_req_ids:
            target_reqs.update(fallback_req_ids)

        for fid in target_finding_ids:
            finding = finding_by_id.get(fid)
            if finding is not None:
                finding.resolved = True
                finding.resolved_by_turn = answer.turn_id
                target_reqs.add(finding.req_id)

        tag = f"[{answer.source}] {answer.answer}"
        for req_id in sorted(target_reqs):
            node = state.graph.get_node(req_id)
            if node is None:
                continue
            if tag not in node.acceptance_criteria:
                node.acceptance_criteria.append(tag)
            provenance_note = f"Clarified via {answer.source} ({answer.turn_id}): {answer.answer}"
            if provenance_note not in node.rationale:
                node.rationale = (
                    f"{node.rationale}\n{provenance_note}".strip()
                    if node.rationale
                    else provenance_note
                )
            if node.status == RequirementStatus.DRAFT:
                node.status = RequirementStatus.PROPOSED

        return target_reqs

    def _evaluate_delta_critics(
        self,
        state: ElicitationState,
        *,
        target_req_ids: Sequence[str],
        turn_id: str,
    ) -> list[CriticDisagreement]:
        if not target_req_ids:
            return []

        glossary_header = self._get_glossary_header(state.session_id)
        new_findings, new_disagreements = self.critic_panel.run_on_nodes(
            state.graph,
            target_req_ids=target_req_ids,
            glossary_header=glossary_header,
        )

        target_set = set(target_req_ids)
        preserved_findings = [
            f
            for f in state.findings
            if (f.req_id not in target_set) or f.resolved
        ]
        state.findings = deduplicate_findings([*preserved_findings, *new_findings])

        preserved_disagreements = [
            d for d in state.disagreements if d.req_id not in target_set
        ]
        state.disagreements = [*preserved_disagreements, *new_disagreements]

        for disagreement in new_disagreements:
            self.event_log.append_event(
                state.session_id,
                EventKind.ESCALATION_TRIGGERED,
                turn_id=turn_id,
                payload={
                    "reason": EscalationReason.CRITIC_DISAGREEMENT.value,
                    "req_id": disagreement.req_id,
                    "critics": [c.value for c in disagreement.critics],
                    "finding_ids": list(disagreement.finding_ids),
                    "escalated": disagreement.escalated,
                    "cloud_verified": disagreement.cloud_verified,
                    "resolution": disagreement.resolution,
                },
            )

        return new_disagreements


__all__ = ["ElicitationEngine"]
