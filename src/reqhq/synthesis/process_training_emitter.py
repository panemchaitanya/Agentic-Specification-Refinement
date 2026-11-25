"""Stage 5a Process SOP Workflows and Role-Based Training Outlines emitter."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from reqhq.schemas.requirements import RequirementGraph, RequirementNode
from reqhq.schemas.routing_adr import RoutingDecisionKind
from reqhq.synthesis.models import EmittedADRFile, TriageItem, TriagePlan, TriageTrack


def _adr_ids_for_req(adrs: Sequence[EmittedADRFile], req_id: str) -> list[str]:
    return [
        adr.adr_id
        for adr in adrs
        if adr.kind == "routing" and req_id in (adr.frontmatter.get("requirement_ids") or [])
    ]


def _has_process_scope(item: TriageItem) -> bool:
    if item.track == TriageTrack.PROCESS:
        return True
    if item.track == TriageTrack.HYBRID:
        if not item.component_breakdown:
            return True
        return RoutingDecisionKind.PROCESS in item.component_breakdown
    return False


def _has_training_scope(item: TriageItem) -> bool:
    if item.track == TriageTrack.TRAINING:
        return True
    if item.track == TriageTrack.HYBRID:
        return RoutingDecisionKind.TRAINING in item.component_breakdown
    return False


def _derive_sop_steps(item: TriageItem, node: RequirementNode | None) -> list[str]:
    steps: list[str] = [
        f"Verify trigger context and inspect audit payload for `{item.req_id}`."
    ]
    if node is not None and node.acceptance_criteria:
        for criterion in node.acceptance_criteria:
            steps.append(f"Validate operational check: {criterion}")
    elif node is not None:
        steps.append(f"Execute documented operational procedure satisfying: {node.statement}")
    else:
        steps.append(f"Execute documented operational procedure for `{item.req_id}`.")
    steps.append(
        "Record timestamped sign-off, rationale, and disposition in the operational audit log."
    )
    return steps


def _derive_training_modules(item: TriageItem, node: RequirementNode | None) -> list[str]:
    title = node.title if node is not None else item.req_id
    modules: list[str] = [
        f"Module 1 — Foundations & Policy Context: Purpose, scope, and failure modes of `{item.req_id}` ({title}).",
        f"Module 2 — Hands-On Workflow Walkthrough: Guided lab executing standard and exception paths for `{item.req_id}`.",
    ]
    if node is not None and node.acceptance_criteria:
        for idx, criterion in enumerate(node.acceptance_criteria, start=3):
            modules.append(f"Module {idx} — Competency Verification: {criterion}")
    else:
        modules.append(
            "Module 3 — Practical Assessment & Escalation Drill: Simulated edge-case assessment and escalation handoff."
        )
    return modules


def emit_process_and_training_md(
    graph: RequirementGraph,
    plan: TriagePlan,
    adrs: Sequence[EmittedADRFile],
) -> str:
    """Emit Process_and_Training.md with operational SOPs and role-based training outlines."""
    process_items = [item for item in plan.items if _has_process_scope(item)]
    training_items = [item for item in plan.items if _has_training_scope(item)]
    assumption_adrs = [adr for adr in adrs if adr.kind == "assumption"]

    lines: list[str] = [
        f"# Operational SOPs & End-User Training Plan (`{plan.session_id}`)",
        "",
        "## Executive Summary & Go-Live Readiness Gate",
        "",
        f"- **Operational SOP Workflows**: {len(process_items)}",
        f"- **Role-Based Training Outlines**: {len(training_items)}",
        f"- **Unconfirmed Assumptions (`user_confirmed: false`)**: {len(assumption_adrs)}",
        "",
        "Production launch MUST NOT proceed until all non-software SOP sign-offs and role-based training readiness criteria below are verified complete.",
        "",
        "## Operational SOP Workflows",
        "",
    ]

    if process_items:
        for idx, item in enumerate(process_items, start=1):
            node = graph.get_node(item.req_id)
            title = node.title if node is not None else item.req_id
            r_adrs = _adr_ids_for_req(adrs, item.req_id)
            adr_str = ", ".join(f"`{a}`" for a in r_adrs) or "*(none)*"
            roles = ", ".join(item.human_roles) or "Operations Lead"
            handoff = (
                f"Receives structured event from Functional Core module `{item.software_module}` "
                "(via `shell.handoff_notifier` in `Design.md`) and submits signed decision back to the Imperative Shell."
                if item.software_module
                else "Manual operational trigger outside automated software; outcomes recorded in operational governance log."
            )
            sla = (
                "Initial triage within 1 business hour; resolution or escalation within 4 business hours "
                "(P0/P1 critical: 15-minute acknowledgement, escalate to Incident Commander / Duty Manager)."
                if node is not None and node.priority in ("P0", "P1")
                else "Initial triage within 1 business day; escalate unresolved exceptions to Operations Lead within 2 business days."
            )
            trigger = (
                f"Operational event or exception queue entry linked to `{item.req_id}` ({title})."
            )
            process_scope = (
                item.component_breakdown.get(RoutingDecisionKind.PROCESS)
                or item.rationale
            )

            lines.extend(
                [
                    f"### SOP-{idx:03d}: {title} (`{item.req_id}`)",
                    "",
                    f"- **Linked Requirement & Routing ADR**: `{item.req_id}` | {adr_str} | Track: `{item.track.value}`",
                    f"- **Trigger**: {trigger}",
                    f"- **Responsible Human Role**: {roles}",
                    f"- **Operational Scope**: {process_scope}",
                    f"- **SLA / Escalation Path**: {sla}",
                    f"- **Software Handoff Point**: {handoff}",
                    "",
                    "#### Checklist Steps",
                    "",
                ]
            )
            for step_num, step in enumerate(_derive_sop_steps(item, node), start=1):
                lines.append(f"{step_num}. {step}")
            lines.append("")

            if item.prerequisites:
                lines.extend(["#### Go-Live Prerequisites", ""])
                for prereq in item.prerequisites:
                    lines.append(f"- [ ] {prereq}")
                lines.append("")
    else:
        lines.extend(
            [
                "No pure Process or Hybrid-Process SOP workflows were routed in this session. Standard software release and incident-response handover checklists apply.",
                "",
            ]
        )

    lines.extend(["## Role-Based End-User Training Outlines", ""])

    if training_items:
        roles_to_items: dict[str, list[TriageItem]] = defaultdict(list)
        for item in training_items:
            target_roles = item.human_roles or ["End User / Trainee"]
            for role in target_roles:
                roles_to_items[role].append(item)

        lines.extend(
            [
                "### Target Role Matrix",
                "",
                "| Target Role | Linked Requirements |",
                "| --- | --- |",
            ]
        )
        for role in sorted(roles_to_items.keys()):
            req_list = ", ".join(f"`{it.req_id}`" for it in roles_to_items[role])
            lines.append(f"| {role} | {req_list} |")
        lines.append("")

        for idx, item in enumerate(training_items, start=1):
            node = graph.get_node(item.req_id)
            title = node.title if node is not None else item.req_id
            r_adrs = _adr_ids_for_req(adrs, item.req_id)
            adr_str = ", ".join(f"`{a}`" for a in r_adrs) or "*(none)*"
            target_role = ", ".join(item.human_roles) or "End User / Trainee"
            prereqs = item.prerequisites or [
                f"Role assignment confirmed and sandbox environment provisioned for `{item.req_id}`."
            ]
            training_scope = (
                item.component_breakdown.get(RoutingDecisionKind.TRAINING)
                or item.rationale
            )

            lines.extend(
                [
                    f"### TRN-{idx:03d}: {title} (`{item.req_id}`)",
                    "",
                    f"- **Target Role**: {target_role}",
                    f"- **Linked Requirement & Routing ADR**: `{item.req_id}` | {adr_str} | Track: `{item.track.value}`",
                    f"- **Training Scope**: {training_scope}",
                    "",
                    "#### Prerequisites",
                    "",
                ]
            )
            for prereq in prereqs:
                lines.append(f"- {prereq}")
            lines.extend(["", "#### Training Modules", ""])
            for mod in _derive_training_modules(item, node):
                lines.append(f"- {mod}")
            lines.extend(
                [
                    "",
                    "#### Go-Live Sign-Off Criteria",
                    "",
                    f"- [ ] 100% of assigned `{target_role}` personnel complete all modules for `{item.req_id}`.",
                    "- [ ] Practical scenario evaluation passed with >= 85% score and zero critical safety/compliance errors.",
                    "- [ ] Training Coordinator and Operations Lead record signed go-live readiness attestation.",
                    "",
                ]
            )
    else:
        lines.extend(
            [
                "No pure Training or Hybrid-Training outlines were routed in this session. Standard release notes distribution applies.",
                "",
            ]
        )

    if assumption_adrs:
        lines.extend(
            [
                "## Unconfirmed Assumptions Impacting Operations & Training",
                "",
            ]
        )
        for adr in assumption_adrs:
            lines.append(
                f"- **`{adr.adr_id}`** (`user_confirmed: false`): {adr.frontmatter.get('title', adr.adr_id)} — confirm with operational stakeholders before training delivery."
            )
        lines.append("")

    return "\n".join(lines)


__all__ = ["emit_process_and_training_md"]
