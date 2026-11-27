"""Five sequential TIER_0-safe specialist critics and inter-critic disagreement panel."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from reqhq.elicitation.models import (
    CriticDisagreement,
    CriticDomain,
    CriticFinding,
    FindingSeverity,
)
from reqhq.ingestion.semantic_chunker import cluster_requirements_into_batches
from reqhq.llm import EscalationReason, LLMRequest, ProviderError, ProviderRouter
from reqhq.schemas.requirements import (
    EdgeKind,
    RequirementGraph,
    RequirementKind,
    RequirementNode,
    RequirementStatus,
)

MAX_CRITIC_PROMPT_CHARS = 5500
_BATCH_SIZE = 5

_CONFLICT_MARKERS = (
    "conflict",
    "contradict",
    "incompatible",
    "disagree",
    "mutually exclusive",
    "opposing",
)
_ANTONYM_PAIRS: tuple[tuple[str, str], ...] = (
    ("strict", "relaxed"),
    ("manual", "automated"),
    ("synchronous", "asynchronous"),
    ("sync", "async"),
    ("require", "skip"),
    ("mandatory", "optional"),
    ("block", "allow"),
    ("deny", "allow"),
    ("centralized", "decentralized"),
)
_MEASURABLE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds|min|minutes|hr|hours|%|rps|qps|mb|gb|kb|users|tx|days)\b"
    r"|\b(?:p95|p99|sla|slo|tls|aes|rbac|oauth|audit log|zero data loss)\b",
    re.IGNORECASE,
)


class _RawCriticFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    req_id: str = Field(..., min_length=1)
    severity: FindingSeverity
    title: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    suggested_question: str = Field(..., min_length=1)
    recommended_default: str = ""
    conflicts_with: CriticDomain | None = None


class _CriticResponsePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[_RawCriticFinding] = Field(default_factory=list)


class _DisagreementArbitrationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution: str = Field(..., min_length=1)
    recommended_default: str = ""


def _make_finding_id(req_id: str, critic: CriticDomain, title: str) -> str:
    digest = hashlib.sha256(f"{req_id}|{critic.value}|{title.strip().lower()}".encode("utf-8")).hexdigest()[:8]
    short_domain = "".join(part[0] for part in critic.value.split("_")).upper()
    return f"FND-{req_id}-{short_domain}-{digest}"


def _node_source_sha(node: RequirementNode) -> str | None:
    if node.trace_links:
        return node.trace_links[0].source_sha256
    return None


def _has_user_clarification(node: RequirementNode) -> bool:
    return any("[user_turn_" in ac for ac in node.acceptance_criteria)


def _format_nodes_compact(
    nodes: Sequence[RequirementNode],
    glossary_header: str,
    *,
    max_chars: int = MAX_CRITIC_PROMPT_CHARS,
) -> str:
    header = glossary_header.strip()[:1200]
    sections: list[str] = []
    if header:
        sections.append(header)
    sections.append("## Target Requirements")
    for node in nodes:
        ac_text = "; ".join(node.acceptance_criteria[:4]) if node.acceptance_criteria else "none"
        sections.append(
            f"- {node.req_id} [{node.kind.value}/{node.priority}] {node.title[:120]}: "
            f"{node.statement[:360]} | AC: {ac_text[:240]}"
        )
    prompt = "\n".join(sections) + (
        '\n\nReturn strict JSON: {"findings": [{"req_id", "severity", "title", '
        '"description", "suggested_question", "recommended_default"}]}. '
        'Return {"findings": []} if no gaps remain.'
    )
    if len(prompt) > max_chars:
        return prompt[: max_chars - 20].rstrip() + "\n[truncated]"
    return prompt


def _try_parse_critic_payload(raw_text: str, parsed: dict[str, Any] | None) -> _CriticResponsePayload | None:
    if parsed is not None:
        try:
            return _CriticResponsePayload.model_validate(parsed)
        except Exception:
            pass
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict) and "findings" in obj:
            return _CriticResponsePayload.model_validate(obj)
    except Exception:
        return None
    return None


class BaseCritic:
    """Base specialist critic executing compact sequential reviews."""

    domain: CriticDomain
    rubric: str

    def evaluate(
        self,
        router: ProviderRouter,
        nodes: Sequence[RequirementNode],
        *,
        graph: RequirementGraph,
        glossary_header: str = "",
    ) -> tuple[list[CriticFinding], dict[str, CriticDomain]]:
        """Evaluate target nodes sequentially in TIER_0-safe batches.

        Returns ``(findings, explicit_conflicts)`` where ``explicit_conflicts`` maps
        ``finding_id -> conflicting CriticDomain`` when declared by the model.
        """
        if not nodes:
            return [], {}

        node_map = {n.req_id: n for n in nodes}
        all_findings: list[CriticFinding] = []
        explicit_conflicts: dict[str, CriticDomain] = {}

        for batch in cluster_requirements_into_batches(nodes, batch_size=_BATCH_SIZE):
            prompt = _format_nodes_compact(batch, glossary_header)
            request = LLMRequest(
                prompt=prompt,
                system_prompt=self.rubric,
                temperature=0.0,
                max_tokens=1024,
                response_schema=_CriticResponsePayload,
                metadata={
                    "stage": "critic",
                    "critic": self.domain.value,
                    "req_ids": [n.req_id for n in batch],
                },
            )
            payload: _CriticResponsePayload | None = None
            try:
                response = router.route(request)
                payload = _try_parse_critic_payload(response.text, response.parsed)
            except ProviderError:
                payload = None

            if payload is not None:
                for raw in payload.findings:
                    if raw.req_id not in node_map:
                        continue
                    fid = _make_finding_id(raw.req_id, self.domain, raw.title)
                    finding = CriticFinding(
                        finding_id=fid,
                        req_id=raw.req_id,
                        critic=self.domain,
                        severity=raw.severity,
                        title=raw.title,
                        description=raw.description,
                        suggested_question=raw.suggested_question,
                        recommended_default=raw.recommended_default,
                        source_sha256=_node_source_sha(node_map[raw.req_id]),
                    )
                    all_findings.append(finding)
                    if raw.conflicts_with is not None and raw.conflicts_with != self.domain:
                        explicit_conflicts[fid] = raw.conflicts_with
            else:
                all_findings.extend(self._deterministic_fallback(batch, graph=graph))

        return all_findings, explicit_conflicts

    def _deterministic_fallback(
        self,
        nodes: Sequence[RequirementNode],
        *,
        graph: RequirementGraph,
    ) -> list[CriticFinding]:
        raise NotImplementedError  # pragma: no cover


class FunctionalCompletenessCritic(BaseCritic):
    """Checks for missing acceptance criteria, edge cases, and failure-mode coverage."""

    domain = CriticDomain.FUNCTIONAL_COMPLETENESS
    rubric = (
        "You are the Functional Completeness Critic. Flag missing inputs/outputs, "
        "boundary conditions, failure handling, and untestable requirements."
    )

    def _deterministic_fallback(
        self,
        nodes: Sequence[RequirementNode],
        *,
        graph: RequirementGraph,
    ) -> list[CriticFinding]:
        findings: list[CriticFinding] = []
        for node in nodes:
            if node.acceptance_criteria or _has_user_clarification(node):
                continue
            title = "Missing testable acceptance criteria and failure-mode bounds"
            findings.append(
                CriticFinding(
                    finding_id=_make_finding_id(node.req_id, self.domain, title),
                    req_id=node.req_id,
                    critic=self.domain,
                    severity=FindingSeverity.BLOCKER if node.priority == "P0" else FindingSeverity.MAJOR,
                    title=title,
                    description=(
                        f"{node.req_id} ({node.title}) lacks explicit acceptance criteria "
                        "and error/boundary handling behavior."
                    ),
                    suggested_question=(
                        f"For {node.req_id} ({node.title}), what exact pass/fail acceptance "
                        "criteria and failure-handling behavior are required?"
                    ),
                    recommended_default=(
                        f"Require deterministic input validation, explicit error codes, and "
                        f"idempotent retry handling for {node.req_id}."
                    ),
                    source_sha256=_node_source_sha(node),
                )
            )
        return findings


class NonFunctionalQualityCritic(BaseCritic):
    """Checks for measurable latency, security, reliability, and audit thresholds."""

    domain = CriticDomain.NON_FUNCTIONAL_QUALITY
    rubric = (
        "You are the Non-Functional Quality Critic. Flag missing quantitative SLA, "
        "latency, security, privacy, availability, and observability targets."
    )

    def _deterministic_fallback(
        self,
        nodes: Sequence[RequirementNode],
        *,
        graph: RequirementGraph,
    ) -> list[CriticFinding]:
        findings: list[CriticFinding] = []
        for node in nodes:
            if node.kind == RequirementKind.PROCESS or _has_user_clarification(node):
                continue
            combined = f"{node.statement} {' '.join(node.acceptance_criteria)}"
            if _MEASURABLE_RE.search(combined):
                continue
            title = "Missing measurable quality and security thresholds"
            findings.append(
                CriticFinding(
                    finding_id=_make_finding_id(node.req_id, self.domain, title),
                    req_id=node.req_id,
                    critic=self.domain,
                    severity=FindingSeverity.MAJOR,
                    title=title,
                    description=(
                        f"{node.req_id} ({node.title}) specifies no quantitative latency, "
                        "availability, or security threshold."
                    ),
                    suggested_question=(
                        f"What measurable SLA, latency, and security/audit controls must "
                        f"{node.req_id} ({node.title}) satisfy?"
                    ),
                    recommended_default=(
                        f"Enforce p95 latency <= 500ms, RBAC authorization, and immutable audit "
                        f"logging for {node.req_id}."
                    ),
                    source_sha256=_node_source_sha(node),
                )
            )
        return findings


class LogicalConsistencyCritic(BaseCritic):
    """Checks for conflicting edges, polarity contradictions, and cross-req tensions."""

    domain = CriticDomain.LOGICAL_CONSISTENCY
    rubric = (
        "You are the Logical Consistency Critic. Flag contradictions, mutually "
        "exclusive constraints, cyclic assumptions, and conflicting state transitions."
    )

    def _deterministic_fallback(
        self,
        nodes: Sequence[RequirementNode],
        *,
        graph: RequirementGraph,
    ) -> list[CriticFinding]:
        findings: list[CriticFinding] = []
        conflict_targets: dict[str, list[str]] = {}
        for edge in graph.edges:
            if edge.relation == EdgeKind.CONFLICTS_WITH:
                conflict_targets.setdefault(edge.source_id, []).append(edge.target_id)
                conflict_targets.setdefault(edge.target_id, []).append(edge.source_id)

        for node in nodes:
            if _has_user_clarification(node):
                continue
            peers = sorted(set(conflict_targets.get(node.req_id, [])))
            if peers:
                title = f"Unresolved requirement conflict with {', '.join(peers)}"
                findings.append(
                    CriticFinding(
                        finding_id=_make_finding_id(node.req_id, self.domain, title),
                        req_id=node.req_id,
                        critic=self.domain,
                        severity=FindingSeverity.BLOCKER,
                        title=title,
                        description=(
                            f"{node.req_id} has explicit CONFLICTS_WITH edges to {', '.join(peers)}."
                        ),
                        suggested_question=(
                            f"How should precedence be resolved between {node.req_id} and "
                            f"{', '.join(peers)}?"
                        ),
                        recommended_default=(
                            f"Prioritize stricter safety/security constraint between {node.req_id} "
                            f"and {', '.join(peers)}."
                        ),
                        source_sha256=_node_source_sha(node),
                    )
                )
        return findings


class ProcessOperationalCritic(BaseCritic):
    """Checks for operational SOPs, ownership, escalation paths, and human sign-off."""

    domain = CriticDomain.PROCESS_OPERATIONAL
    rubric = (
        "You are the Process & Operational Critic. Flag missing human approval SOPs, "
        "incident escalation owners, operational runbooks, and governance boundaries."
    )

    _OPS_TRIGGERS = ("approval", "payment", "invoice", "incident", "manual", "audit", "compliance", "process", "escalat")

    def _deterministic_fallback(
        self,
        nodes: Sequence[RequirementNode],
        *,
        graph: RequirementGraph,
    ) -> list[CriticFinding]:
        findings: list[CriticFinding] = []
        for node in nodes:
            if _has_user_clarification(node):
                continue
            low = f"{node.title} {node.statement}".lower()
            if node.kind == RequirementKind.PROCESS or any(tok in low for tok in self._OPS_TRIGGERS):
                title = "Missing operational SOP and escalation ownership"
                findings.append(
                    CriticFinding(
                        finding_id=_make_finding_id(node.req_id, self.domain, title),
                        req_id=node.req_id,
                        critic=self.domain,
                        severity=FindingSeverity.MAJOR,
                        title=title,
                        description=(
                            f"{node.req_id} ({node.title}) implies operational governance "
                            "without naming owning roles, SOP steps, or exception SLAs."
                        ),
                        suggested_question=(
                            f"Which operational role owns {node.req_id} ({node.title}), and what "
                            "SOP / escalation path applies when exceptions occur?"
                        ),
                        recommended_default=(
                            f"Assign Operations Lead ownership with a documented dual-review SOP "
                            f"and 4-hour exception escalation SLA for {node.req_id}."
                        ),
                        source_sha256=_node_source_sha(node),
                    )
                )
        return findings


class TrainingAdoptionCritic(BaseCritic):
    """Checks for human-role onboarding, enablement prerequisites, and adoption readiness."""

    domain = CriticDomain.TRAINING_ADOPTION
    rubric = (
        "You are the Training & Adoption Critic. Flag user-facing or role-gated "
        "workflows lacking training prerequisites, role certification, or rollout plans."
    )

    _ADOPTION_TRIGGERS = ("training", "staff", "operator", "admin", "approval", "onboarding", "user", "role", "workflow")

    def _deterministic_fallback(
        self,
        nodes: Sequence[RequirementNode],
        *,
        graph: RequirementGraph,
    ) -> list[CriticFinding]:
        findings: list[CriticFinding] = []
        for node in nodes:
            if _has_user_clarification(node):
                continue
            low = f"{node.title} {node.statement}".lower()
            if any(tok in low for tok in self._ADOPTION_TRIGGERS):
                title = "Missing role training and adoption readiness criteria"
                findings.append(
                    CriticFinding(
                        finding_id=_make_finding_id(node.req_id, self.domain, title),
                        req_id=node.req_id,
                        critic=self.domain,
                        severity=FindingSeverity.MINOR,
                        title=title,
                        description=(
                            f"{node.req_id} ({node.title}) impacts human roles without defining "
                            "go-live training or enablement readiness criteria."
                        ),
                        suggested_question=(
                            f"What role training or onboarding verification is required before "
                            f"enabling {node.req_id} ({node.title})?"
                        ),
                        recommended_default=(
                            f"Require role-based quickstart training and sign-off before "
                            f"production rollout of {node.req_id}."
                        ),
                        source_sha256=_node_source_sha(node),
                    )
                )
        return findings


def _findings_conflict(
    a: CriticFinding,
    b: CriticFinding,
    explicit_conflicts: dict[str, CriticDomain],
) -> bool:
    """Return True if two findings from different critics conflict on the same requirement."""
    if a.critic == b.critic or a.req_id != b.req_id:
        return False

    # 1. Explicit conflict annotation from structured critic output.
    if explicit_conflicts.get(a.finding_id) == b.critic or explicit_conflicts.get(b.finding_id) == a.critic:
        return True

    # 2. Explicit conflict language in title/description.
    combined_a = f"{a.title} {a.description} {a.recommended_default}".lower()
    combined_b = f"{b.title} {b.description} {b.recommended_default}".lower()
    if any(marker in combined_a for marker in _CONFLICT_MARKERS) or any(
        marker in combined_b for marker in _CONFLICT_MARKERS
    ):
        return True

    # 3. Direct opposing polarity in findings / recommended defaults.
    for left, right in _ANTONYM_PAIRS:
        if (left in combined_a and right in combined_b) or (right in combined_a and left in combined_b):
            return True

    return False


class CriticPanel:
    """Runs the 5 specialist critics sequentially and escalates inter-critic disagreements."""

    def __init__(
        self,
        router: ProviderRouter,
        critics: Sequence[BaseCritic] | None = None,
    ) -> None:
        self.router = router
        self.critics: tuple[BaseCritic, ...] = (
            tuple(critics)
            if critics is not None
            else (
                FunctionalCompletenessCritic(),
                NonFunctionalQualityCritic(),
                LogicalConsistencyCritic(),
                ProcessOperationalCritic(),
                TrainingAdoptionCritic(),
            )
        )

    def run_on_nodes(
        self,
        graph: RequirementGraph,
        target_req_ids: Iterable[str] | None = None,
        *,
        glossary_header: str = "",
    ) -> tuple[list[CriticFinding], list[CriticDisagreement]]:
        """Run all specialist critics sequentially on target nodes and arbitrate conflicts."""
        if target_req_ids is None:
            target_nodes = [
                n
                for n in graph.nodes
                if n.status not in (RequirementStatus.DEPRECATED, RequirementStatus.SUPERSEDED)
            ]
        else:
            wanted = set(target_req_ids)
            target_nodes = [
                n
                for n in graph.nodes
                if n.req_id in wanted
                and n.status not in (RequirementStatus.DEPRECATED, RequirementStatus.SUPERSEDED)
            ]

        if not target_nodes:
            return [], []

        all_findings: list[CriticFinding] = []
        explicit_conflicts: dict[str, CriticDomain] = {}

        # Strictly sequential single-model execution for TIER_0 memory envelope.
        for critic in self.critics:
            critic_findings, critic_conflicts = critic.evaluate(
                self.router,
                target_nodes,
                graph=graph,
                glossary_header=glossary_header,
            )
            all_findings.extend(critic_findings)
            explicit_conflicts.update(critic_conflicts)

        disagreements = self._detect_and_escalate_disagreements(
            all_findings,
            explicit_conflicts=explicit_conflicts,
            glossary_header=glossary_header,
        )
        return all_findings, disagreements

    def _detect_and_escalate_disagreements(
        self,
        findings: Sequence[CriticFinding],
        *,
        explicit_conflicts: dict[str, CriticDomain],
        glossary_header: str,
    ) -> list[CriticDisagreement]:
        by_req: dict[str, list[CriticFinding]] = {}
        for f in findings:
            by_req.setdefault(f.req_id, []).append(f)

        disagreements: list[CriticDisagreement] = []
        for req_id in sorted(by_req):
            req_findings = by_req[req_id]
            conflicting_ids: set[str] = set()
            conflicting_critics: list[CriticDomain] = []

            for i in range(len(req_findings)):
                for j in range(i + 1, len(req_findings)):
                    fa, fb = req_findings[i], req_findings[j]
                    if _findings_conflict(fa, fb, explicit_conflicts):
                        conflicting_ids.add(fa.finding_id)
                        conflicting_ids.add(fb.finding_id)
                        if fa.critic not in conflicting_critics:
                            conflicting_critics.append(fa.critic)
                        if fb.critic not in conflicting_critics:
                            conflicting_critics.append(fb.critic)

            if len(conflicting_critics) < 2:
                continue

            involved = [f for f in req_findings if f.finding_id in conflicting_ids]
            summary = (
                f"Critics {', '.join(c.value for c in conflicting_critics)} disagree on {req_id}: "
                + "; ".join(f"[{f.critic.value}/{f.severity.value}] {f.title}" for f in involved)
            )
            disagreement = self._arbitrate_disagreement(
                req_id=req_id,
                critics=conflicting_critics,
                involved=involved,
                summary=summary,
                glossary_header=glossary_header,
            )
            disagreements.append(disagreement)

        return disagreements

    def _arbitrate_disagreement(
        self,
        *,
        req_id: str,
        critics: list[CriticDomain],
        involved: Sequence[CriticFinding],
        summary: str,
        glossary_header: str,
    ) -> CriticDisagreement:
        lines = []
        if glossary_header.strip():
            lines.append(glossary_header.strip()[:800])
        lines.append(f"Arbitrate critic disagreement on {req_id}:")
        for f in involved:
            lines.append(
                f"- {f.critic.value} ({f.severity.value}): {f.title} | "
                f"{f.description[:240]} | default={f.recommended_default[:120]}"
            )
        lines.append('Return strict JSON: {"resolution": "...", "recommended_default": "..."}.')
        arb_prompt = "\n".join(lines)[:MAX_CRITIC_PROMPT_CHARS]

        request = LLMRequest(
            prompt=arb_prompt,
            system_prompt=(
                "You are a senior requirements arbitrator. Reconcile the conflicting "
                "critic assessments into a single clear resolution."
            ),
            temperature=0.0,
            max_tokens=512,
            response_schema=_DisagreementArbitrationPayload,
            metadata={
                "stage": "critic_arbitration",
                "req_id": req_id,
                "critics": [c.value for c in critics],
            },
        )

        resolution = ""
        escalated = False
        cloud_verified = False
        try:
            response = self.router.route(
                request,
                escalation_reason=EscalationReason.CRITIC_DISAGREEMENT,
            )
            escalated = bool(response.escalated)
            cloud_verified = bool(response.cloud_verified)
            if response.parsed and isinstance(response.parsed.get("resolution"), str):
                resolution = response.parsed["resolution"].strip()
            else:
                try:
                    parsed_raw = json.loads(response.text.strip())
                    if isinstance(parsed_raw, dict) and isinstance(parsed_raw.get("resolution"), str):
                        resolution = parsed_raw["resolution"].strip()
                except Exception:
                    resolution = response.text.strip()[:300]
        except ProviderError as exc:
            resolution = f"Arbitration deferred (provider unavailable: {exc})"

        if not resolution:
            resolution = (
                f"Requires explicit stakeholder decision on {req_id} between "
                f"{', '.join(c.value for c in critics)}."
            )

        return CriticDisagreement(
            req_id=req_id,
            critics=critics,
            finding_ids=[f.finding_id for f in involved],
            summary=summary,
            resolution=resolution,
            escalated=escalated,
            cloud_verified=cloud_verified,
        )


__all__ = [
    "BaseCritic",
    "CriticPanel",
    "FunctionalCompletenessCritic",
    "LogicalConsistencyCritic",
    "MAX_CRITIC_PROMPT_CHARS",
    "NonFunctionalQualityCritic",
    "ProcessOperationalCritic",
    "TrainingAdoptionCritic",
]
