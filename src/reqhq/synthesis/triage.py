"""Stage 4a Socio-Technical Triager routing requirements across Software/Process/Training/Hybrid."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from reqhq.llm.providers.base import LLMRequest
from reqhq.llm.router import ProviderRouter
from reqhq.schemas.requirements import (
    RequirementGraph,
    RequirementKind,
    RequirementNode,
    RequirementStatus,
)
from reqhq.schemas.routing_adr import RoutingDecisionKind
from reqhq.synthesis.models import TriageItem, TriagePlan, TriageTrack

_INACTIVE_STATUSES: frozenset[RequirementStatus] = frozenset(
    {RequirementStatus.DEPRECATED, RequirementStatus.SUPERSEDED}
)

_TRAINING_KEYWORDS: tuple[str, ...] = (
    "training",
    "train ",
    "onboarding",
    "curriculum",
    "workshop",
    "certification",
    "certify",
    "coaching",
    "enablement",
    "user guide",
    "refresher",
    "competency",
    "skill",
)

_PROCESS_KEYWORDS: tuple[str, ...] = (
    "sop",
    "runbook",
    "manual review",
    "manual approval",
    "human approval",
    "sign-off",
    "signoff",
    "escalation",
    "incident response",
    "governance",
    "committee",
    "policy",
    "audit review",
    "handoff",
    "shift handover",
    "human operator",
    "compliance officer",
    "operational procedure",
)

_SOFTWARE_KEYWORDS: tuple[str, ...] = (
    "api",
    "endpoint",
    "service",
    "database",
    "state machine",
    "encrypt",
    "authenticate",
    "validate",
    "automated",
    "webhook",
    "queue",
    "persist",
    "schema",
    "system shall",
    "latency",
    "throughput",
    "ledger",
    "engine",
)

_SYSTEM_PROMPT = (
    "You are a socio-technical systems architect performing Stage 4a triage. "
    "Classify each requirement into exactly one track: 'software', 'process', 'training', or 'hybrid'. "
    "Never force human-governance, SOP, or end-user skill problems into pure software. "
    "For every requirement, provide a concise rationale, rejected alternatives, non-software go-live "
    "prerequisites (when applicable), human roles (when process/training/hybrid), and a snake_case "
    "software_module name (when software/hybrid). "
    "For 'hybrid', populate component_breakdown with at least 2 concrete tracks among "
    "Software, Process, and Training. Respond ONLY with valid JSON matching the schema."
)


class _LLMTriageEntry(BaseModel):
    """Single item in the compact structured JSON batch response from the SLM."""

    model_config = ConfigDict(extra="forbid")

    req_id: str = Field(..., min_length=1)
    track: TriageTrack
    rationale: str = Field(..., min_length=1)
    rejected_alternatives: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    component_breakdown: dict[str, str] = Field(default_factory=dict)
    human_roles: list[str] = Field(default_factory=list)
    software_module: str = ""


class _BatchTriageResponse(BaseModel):
    """Strict batch schema returned by ProviderRouter for TIER_0 SLM triage."""

    model_config = ConfigDict(extra="forbid")

    items: list[_LLMTriageEntry] = Field(default_factory=list)


def _slugify_module_name(node: RequirementNode) -> str:
    """Derive a clean Python snake_case Functional Core module name for a requirement."""
    source = node.title.lower()
    tokens = re.findall(r"[a-z0-9]+", source)
    filtered = [t for t in tokens if t not in {"the", "a", "an", "for", "of", "to", "and", "or", "in"}]
    slug_tokens = filtered[:4] if filtered else re.findall(r"[a-z0-9]+", node.req_id.lower())
    slug = "_".join(slug_tokens) or "core_domain"
    if slug[0].isdigit():
        slug = f"mod_{slug}"
    return slug


def _active_nodes_ordered(graph: RequirementGraph) -> list[RequirementNode]:
    """Return active (non-deprecated, non-superseded) nodes in deterministic order."""
    node_map = {
        node.req_id: node
        for node in graph.nodes
        if node.status not in _INACTIVE_STATUSES
    }
    if not node_map:
        return []
    if graph.is_dag():
        ordered_ids = [rid for rid in graph.topological_order() if rid in node_map]
    else:
        ordered_ids = sorted(node_map.keys())
    return [node_map[rid] for rid in ordered_ids]


def _heuristic_triage_item(node: RequirementNode, *, cloud_verified: bool = False) -> TriageItem:
    """Deterministic socio-technical triage fallback when an LLM omits or degrades a batch."""
    text = f"{node.title} {node.statement} {' '.join(node.acceptance_criteria)} {' '.join(node.tags)}".lower()
    has_training = any(kw in text for kw in _TRAINING_KEYWORDS)
    has_process = node.kind == RequirementKind.PROCESS or any(
        kw in text for kw in _PROCESS_KEYWORDS
    )
    has_software = node.kind in (
        RequirementKind.INTERFACE,
        RequirementKind.DATA,
        RequirementKind.SECURITY,
    ) or any(kw in text for kw in _SOFTWARE_KEYWORDS)

    module_name = _slugify_module_name(node)

    if has_software and (has_process or has_training):
        breakdown: dict[RoutingDecisionKind, str] = {
            RoutingDecisionKind.SOFTWARE: (
                f"Pure Functional Core state validation and audit trail in `{module_name}`."
            ),
        }
        roles: list[str] = []
        prereqs: list[str] = []
        if has_process:
            breakdown[RoutingDecisionKind.PROCESS] = (
                f"Operational SOP review and human approval gate for {node.req_id}."
            )
            roles.append("Operations Lead")
            prereqs.append(f"Approved operational SOP sign-off for {node.req_id}")
        if has_training:
            breakdown[RoutingDecisionKind.TRAINING] = (
                f"Role-based operator enablement and readiness check for {node.req_id}."
            )
            roles.append("End User / Operator")
            prereqs.append(f"Completed role training sign-off for {node.req_id}")
        return TriageItem(
            req_id=node.req_id,
            track=TriageTrack.HYBRID,
            rationale=(
                f"{node.req_id} spans both automated software enforcement and human operational/training "
                "accountability, requiring a Hybrid split."
            ),
            rejected_alternatives=[
                "Pure software automation rejected because human accountability/readiness cannot be fully automated.",
                "Pure manual process rejected because deterministic state validation requires software guardrails.",
            ],
            prerequisites=prereqs,
            component_breakdown=breakdown,
            human_roles=roles,
            software_module=module_name,
            cloud_verified=cloud_verified,
        )

    if has_training and not has_process:
        return TriageItem(
            req_id=node.req_id,
            track=TriageTrack.TRAINING,
            rationale=(
                f"{node.req_id} addresses human competency, onboarding, or behavioral adoption "
                "best solved via structured role-based training."
            ),
            rejected_alternatives=[
                "Software-only tooltips rejected as insufficient for role competency verification.",
            ],
            prerequisites=[
                f"Curriculum approved and role-based training completed before go-live for {node.req_id}"
            ],
            human_roles=["End User / Trainee", "Training Coordinator"],
            software_module="",
            cloud_verified=cloud_verified,
        )

    if has_process and not has_training:
        return TriageItem(
            req_id=node.req_id,
            track=TriageTrack.PROCESS,
            rationale=(
                f"{node.req_id} defines human governance, approval, or operational workflow "
                "accountability best enforced as a documented SOP."
            ),
            rejected_alternatives=[
                "Unattended software automation rejected due to human accountability and exception judgment needs.",
            ],
            prerequisites=[
                f"Documented SOP and escalation matrix signed off by operations before go-live for {node.req_id}"
            ],
            human_roles=["Operations Lead"],
            software_module="",
            cloud_verified=cloud_verified,
        )

    if has_process and has_training:
        return TriageItem(
            req_id=node.req_id,
            track=TriageTrack.HYBRID,
            rationale=(
                f"{node.req_id} combines operational SOP execution with role-based training readiness."
            ),
            rejected_alternatives=[
                "Software automation rejected because the requirement governs human procedures and training.",
            ],
            prerequisites=[
                f"SOP signed off and operators certified prior to go-live for {node.req_id}"
            ],
            component_breakdown={
                RoutingDecisionKind.PROCESS: f"Standard operating procedure and escalation rules for {node.req_id}.",
                RoutingDecisionKind.TRAINING: f"Hands-on operator training and readiness check for {node.req_id}.",
            },
            human_roles=["Operations Lead", "End User / Operator"],
            software_module="",
            cloud_verified=cloud_verified,
        )

    return TriageItem(
        req_id=node.req_id,
        track=TriageTrack.SOFTWARE,
        rationale=(
            f"{node.req_id} specifies deterministic system behavior suitable for a pure "
            f"Functional Core module (`{module_name}`) behind an Imperative Shell."
        ),
        rejected_alternatives=[
            "Manual SOP rejected due to latency, repeatability, and deterministic enforcement needs.",
        ],
        prerequisites=[],
        component_breakdown={},
        human_roles=[],
        software_module=module_name,
        cloud_verified=cloud_verified,
    )


def _normalize_llm_entry(
    entry: _LLMTriageEntry,
    node: RequirementNode,
    *,
    cloud_verified: bool,
) -> TriageItem:
    """Convert an LLM batch entry into a validated TriageItem with safe defaults."""
    raw_breakdown: dict[str, str] = {}
    for key, desc in entry.component_breakdown.items():
        if key.strip().lower() == "hybrid":
            continue
        cleaned = str(desc).strip()
        if cleaned:
            raw_breakdown[key] = cleaned

    software_module = entry.software_module.strip()
    human_roles = [r.strip() for r in entry.human_roles if r.strip()]
    prerequisites = [p.strip() for p in entry.prerequisites if p.strip()]
    rejected = [a.strip() for a in entry.rejected_alternatives if a.strip()]

    if entry.track == TriageTrack.SOFTWARE:
        if not software_module:
            software_module = _slugify_module_name(node)
        raw_breakdown = {}
    elif entry.track == TriageTrack.PROCESS:
        if not human_roles:
            human_roles = ["Operations Lead"]
        if not prerequisites:
            prerequisites = [f"Signed-off operational SOP for {node.req_id} prior to go-live"]
        raw_breakdown = {}
    elif entry.track == TriageTrack.TRAINING:
        if not human_roles:
            human_roles = ["End User / Trainee", "Training Coordinator"]
        if not prerequisites:
            prerequisites = [f"Role-based training sign-off completed for {node.req_id} prior to go-live"]
        raw_breakdown = {}
    elif entry.track == TriageTrack.HYBRID:
        if len(raw_breakdown) < 2:
            if not software_module:
                software_module = _slugify_module_name(node)
            raw_breakdown.setdefault(
                "Software",
                f"Automated domain state enforcement in `{software_module}`.",
            )
            if len(raw_breakdown) < 2:
                raw_breakdown.setdefault(
                    "Process",
                    f"Operational SOP oversight and exception handling for {node.req_id}.",
                )
        has_software_slice = any(k.strip().lower() == "software" for k in raw_breakdown)
        if has_software_slice and not software_module:
            software_module = _slugify_module_name(node)
        if not human_roles:
            human_roles = ["Operations Lead"]
        if not prerequisites:
            prerequisites = [f"Non-software SOP/training go-live gate completed for {node.req_id}"]

    if not rejected:
        rejected = [f"Alternative single-track routing rejected for {node.req_id}."]

    return TriageItem(
        req_id=node.req_id,
        track=entry.track,
        rationale=entry.rationale,
        rejected_alternatives=rejected,
        prerequisites=prerequisites,
        component_breakdown=raw_breakdown,  # type: ignore[arg-type]
        human_roles=human_roles,
        software_module=software_module,
        cloud_verified=cloud_verified,
    )


class SocioTechnicalTriager:
    """Sequential TIER_0-safe triager routing active requirements into socio-technical tracks."""

    def __init__(
        self,
        router: ProviderRouter,
        *,
        batch_size: int = 4,
        max_prompt_chars: int = 5500,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if max_prompt_chars < 500:
            raise ValueError("max_prompt_chars must be >= 500")
        self.router = router
        self.batch_size = batch_size
        self.max_prompt_chars = max_prompt_chars

    def _format_node_block(self, node: RequirementNode) -> str:
        criteria = "; ".join(node.acceptance_criteria[:3]) if node.acceptance_criteria else "none"
        tags = ", ".join(node.tags[:5]) if node.tags else "none"
        return (
            f"- {node.req_id} [{node.kind.value} | {node.priority}]: {node.title}\n"
            f"  Statement: {node.statement}\n"
            f"  Acceptance: {criteria}\n"
            f"  Tags: {tags}"
        )

    def _build_batches(self, nodes: list[RequirementNode]) -> list[list[RequirementNode]]:
        batches: list[list[RequirementNode]] = []
        current_batch: list[RequirementNode] = []
        current_chars = 250  # header overhead

        for node in nodes:
            block_len = len(self._format_node_block(node)) + 2
            would_exceed_size = len(current_batch) >= self.batch_size
            would_exceed_chars = bool(current_batch) and (
                current_chars + block_len > self.max_prompt_chars
            )
            if would_exceed_size or would_exceed_chars:
                batches.append(current_batch)
                current_batch = []
                current_chars = 250
            current_batch.append(node)
            current_chars += block_len

        if current_batch:
            batches.append(current_batch)
        return batches

    def _build_batch_prompt(
        self,
        session_id: str,
        batch: list[RequirementNode],
        batch_index: int,
        total_batches: int,
    ) -> str:
        lines = [
            f"Session: {session_id} | Batch {batch_index + 1}/{total_batches}",
            "Classify every listed requirement into software, process, training, or hybrid.",
            "Requirements:",
        ]
        for node in batch:
            lines.append(self._format_node_block(node))
        return "\n".join(lines)

    def triage(self, graph: RequirementGraph, *, session_id: str) -> TriagePlan:
        """Route every active requirement in graph sequentially via ProviderRouter."""
        active_nodes = _active_nodes_ordered(graph)
        if not active_nodes:
            return TriagePlan(session_id=session_id, items=[])

        batches = self._build_batches(active_nodes)
        triaged_items: list[TriageItem] = []

        for idx, batch in enumerate(batches):
            prompt = self._build_batch_prompt(session_id, batch, idx, len(batches))
            request = LLMRequest(
                prompt=prompt,
                system_prompt=_SYSTEM_PROMPT,
                temperature=0.0,
                max_tokens=1536,
                response_schema=_BatchTriageResponse,
                metadata={
                    "stage": "socio_technical_triage",
                    "session_id": session_id,
                    "batch_index": idx,
                    "req_ids": [n.req_id for n in batch],
                },
            )
            response = self.router.route(request)
            parsed_by_id: dict[str, _LLMTriageEntry] = {}
            if response.parsed is not None:
                parsed_model = _BatchTriageResponse.model_validate(response.parsed)
                for entry in parsed_model.items:
                    parsed_by_id[entry.req_id.strip()] = entry

            for node in batch:
                entry = parsed_by_id.get(node.req_id)
                if entry is not None:
                    item = _normalize_llm_entry(
                        entry,
                        node,
                        cloud_verified=response.cloud_verified,
                    )
                else:
                    item = _heuristic_triage_item(
                        node,
                        cloud_verified=response.cloud_verified,
                    )
                triaged_items.append(item)

        return TriagePlan(session_id=session_id, items=triaged_items)


__all__ = ["SocioTechnicalTriager"]
