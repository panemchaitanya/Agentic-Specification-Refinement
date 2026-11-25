"""Stage 5a Functional Core / Imperative Shell Design.md emitter with Mermaid diagrams."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
import re

from reqhq.schemas.requirements import RequirementGraph, RequirementNode
from reqhq.schemas.routing_adr import RoutingDecisionKind
from reqhq.synthesis.models import EmittedADRFile, TriageItem, TriagePlan, TriageTrack


def _safe_mermaid_id(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", label).strip("_")
    if not cleaned:
        return "node"
    if cleaned[0].isdigit():
        cleaned = f"n_{cleaned}"
    return cleaned


def _safe_mermaid_label(text: str, *, max_len: int = 48) -> str:
    collapsed = " ".join(text.replace('"', "'").replace("\n", " ").split())
    if len(collapsed) > max_len:
        collapsed = collapsed[: max_len - 3].rstrip() + "..."
    return collapsed


def _is_software_bearing(item: TriageItem) -> bool:
    if item.track == TriageTrack.SOFTWARE:
        return True
    if item.track == TriageTrack.HYBRID:
        if not item.component_breakdown:
            return bool(item.software_module)
        return RoutingDecisionKind.SOFTWARE in item.component_breakdown or bool(
            item.software_module
        )
    return False


def _pascal_case(snake: str) -> str:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", snake) if p]
    return "".join(p.capitalize() for p in parts) or "DomainModule"


def _adr_index_by_req(adrs: Sequence[EmittedADRFile]) -> dict[str, list[EmittedADRFile]]:
    index: dict[str, list[EmittedADRFile]] = defaultdict(list)
    for adr in adrs:
        for req_id in adr.frontmatter.get("requirement_ids", []) or []:
            index[str(req_id)].append(adr)
    return index


def _render_flowchart_mermaid(
    modules: dict[str, list[tuple[TriageItem, RequirementNode | None]]],
    non_software_items: list[TriageItem],
) -> list[str]:
    lines: list[str] = [
        "```mermaid",
        "flowchart TD",
        '    Client["Inbound Client / CLI / API"] --> ShellIn["Imperative Shell (Inbound Adapters)"]',
        '    ShellIn --> CoreRouter["Functional Core Dispatcher (Pure State Machine)"]',
    ]

    if modules:
        for mod_name in sorted(modules.keys()):
            mod_id = _safe_mermaid_id(f"core_{mod_name}")
            req_list = ", ".join(item.req_id for item, _ in modules[mod_name])
            label = _safe_mermaid_label(f"{mod_name} ({req_list})")
            lines.append(f'    CoreRouter --> {mod_id}["Pure Core: {label}"]')
            lines.append(f"    {mod_id} --> ShellOut")
    else:
        lines.append('    CoreRouter --> CoreNoop["Pure Core: Minimal Audit Stub"]')
        lines.append("    CoreNoop --> ShellOut")

    lines.append('    ShellOut["Imperative Shell (Persistence & I/O Adapters)"]')

    if non_software_items:
        lines.append(
            '    Boundary["Socio-Technical Boundary (Process SOP & Training)"]'
        )
        lines.append("    ShellOut -. Handoff .-> Boundary")
        for item in non_software_items:
            b_id = _safe_mermaid_id(f"st_{item.req_id}")
            roles = ", ".join(item.human_roles[:2]) or "Human Operator"
            b_label = _safe_mermaid_label(f"{item.req_id} [{item.track.value}]: {roles}")
            lines.append(f'    Boundary -.-> {b_id}["{b_label}"]')

    lines.append("```")
    return lines


def _render_sequence_mermaid(
    modules: dict[str, list[tuple[TriageItem, RequirementNode | None]]],
    non_software_items: list[TriageItem],
) -> list[str]:
    first_module = sorted(modules.keys())[0] if modules else "domain_core"
    has_process = any(
        item.track in (TriageTrack.PROCESS, TriageTrack.HYBRID)
        for item in non_software_items
    )
    has_training = any(
        item.track in (TriageTrack.TRAINING, TriageTrack.HYBRID)
        for item in non_software_items
    )

    lines: list[str] = [
        "```mermaid",
        "sequenceDiagram",
        "    actor Requester as Client / Operator",
        "    participant Shell as Imperative Shell",
        f"    participant Core as Functional Core ({first_module})",
        "    participant Store as Persistence Adapter",
    ]
    if has_process or has_training:
        lines.append("    actor HumanActor as Operations / Trained Role")

    lines.extend(
        [
            "    Requester->>Shell: Submit command payload",
            "    Shell->>Store: Load immutable aggregate state",
            "    Store-->>Shell: Current DomainState snapshot",
            "    Shell->>Core: apply_command(state, command) [pure, zero I/O]",
            "    Core-->>Shell: TransitionResult(next_state, domain_events)",
            "    Shell->>Store: Persist next_state and domain_events",
            "    Store-->>Shell: Commit acknowledgement",
        ]
    )

    if has_process or has_training:
        lines.extend(
            [
                "    Shell-->>HumanActor: Emit SOP handoff / review notification",
                "    HumanActor-->>Shell: Record human sign-off per Process_and_Training.md",
            ]
        )

    lines.extend(
        [
            "    Shell-->>Requester: Return validated response",
            "```",
        ]
    )
    return lines


def emit_design_md(
    graph: RequirementGraph,
    plan: TriagePlan,
    adrs: Sequence[EmittedADRFile],
) -> str:
    """Emit Design.md enforcing Functional Core, Imperative Shell, Mermaid diagrams, and boundaries."""
    adr_by_req = _adr_index_by_req(adrs)
    assumption_adrs = [adr for adr in adrs if adr.kind == "assumption"]

    software_modules: dict[str, list[tuple[TriageItem, RequirementNode | None]]] = defaultdict(list)
    non_software_items: list[TriageItem] = []

    for item in plan.items:
        node = graph.get_node(item.req_id)
        if _is_software_bearing(item):
            mod_name = item.software_module or f"core_{item.req_id.lower().replace('-', '_')}"
            software_modules[mod_name].append((item, node))
        if item.track in (TriageTrack.PROCESS, TriageTrack.TRAINING, TriageTrack.HYBRID):
            non_software_items.append(item)

    lines: list[str] = [
        f"# System Architecture & Design (`{plan.session_id}`)",
        "",
        "## Architecture Pattern: Functional Core, Imperative Shell",
        "",
        "All software-routed requirements in this system strictly follow the **Functional Core, Imperative Shell** pattern:",
        "",
        "- **Functional Core (Pure Python Domain State Machines)**: Pure, deterministic functions and immutable Pydantic v2 state models (`ConfigDict(extra=\"forbid\")`) with **zero I/O** (no filesystem, network, clock, randomness, or database calls). Every transition takes `(state, command) -> TransitionResult(next_state, events)` so Phase 3 Hypothesis stateful Property-Based Testing (`RuleBasedStateMachine` / `PBTSpec`) can exhaustively exercise domain invariants without mocks.",
        "- **Imperative Shell (I/O Adapters)**: Thin adapters handling CLI/HTTP transport, persistence repositories, authentication middleware, and human workflow webhooks. Adapters decode untrusted inputs into strict Pydantic commands, invoke the Functional Core, and commit resulting state/events.",
        "- **Socio-Technical Boundary**: Requirements routed to `Process` or `Training` (and the non-software slices of `Hybrid` requirements) are intentionally excluded from automated software enforcement and governed via `Process_and_Training.md`.",
        "",
        "## Component Topology",
        "",
        *_render_flowchart_mermaid(software_modules, non_software_items),
        "",
        "## Runtime Interaction Sequence",
        "",
        *_render_sequence_mermaid(software_modules, non_software_items),
        "",
        "## Functional Core Modules (Phase 3 PBT Targets)",
        "",
    ]

    if software_modules:
        lines.extend(
            [
                "| Module (`functional_core`) | Requirements | Routing ADRs | Pure State Machine Responsibility |",
                "| --- | --- | --- | --- |",
            ]
        )
        for mod_name in sorted(software_modules.keys()):
            entries = software_modules[mod_name]
            req_ids_str = ", ".join(f"`{item.req_id}`" for item, _ in entries)
            routing_ids = sorted(
                {
                    adr.adr_id
                    for item, _ in entries
                    for adr in adr_by_req.get(item.req_id, [])
                    if adr.kind == "routing"
                }
            )
            adr_str = ", ".join(f"`{aid}`" for aid in routing_ids) or "*(none)*"
            summaries = []
            for item, node in entries:
                if (
                    item.track == TriageTrack.HYBRID
                    and RoutingDecisionKind.SOFTWARE in item.component_breakdown
                ):
                    summaries.append(item.component_breakdown[RoutingDecisionKind.SOFTWARE])
                elif node is not None:
                    summaries.append(node.title)
                else:
                    summaries.append(item.req_id)
            resp_summary = "; ".join(summaries).replace("|", "\\|")
            lines.append(f"| `{mod_name}` | {req_ids_str} | {adr_str} | {resp_summary} |")
        lines.append("")
    else:
        lines.extend(
            [
                "No pure software modules were routed in this session; see **Socio-Technical Boundaries** below.",
                "",
            ]
        )

    lines.extend(
        [
            "## Imperative Shell Adapters",
            "",
            "- **Inbound Adapter (`shell.api_cli`)**: Parses external requests, enforces schema validation (`extra=\"forbid\"`), and maps requests into pure domain commands.",
            "- **Persistence Adapter (`shell.repository`)**: Loads and atomically saves domain snapshots and append-only audit events.",
            "- **Human Handoff Adapter (`shell.handoff_notifier`)**: Emits structured notifications and records human sign-offs at Process/Training boundaries without embedding business rules.",
            "",
            "## Module Interface Contracts",
            "",
        ]
    )

    if software_modules:
        for mod_name in sorted(software_modules.keys()):
            entries = software_modules[mod_name]
            cls_prefix = _pascal_case(mod_name)
            req_ids_str = ", ".join(item.req_id for item, _ in entries)
            lines.extend(
                [
                    f"### `{mod_name}` (`{req_ids_str}`)",
                    "",
                    "```python",
                    f"class {cls_prefix}State(BaseModel):",
                    '    model_config = ConfigDict(extra="forbid", frozen=True)',
                    "    version: int = 0",
                    "",
                    f"class {cls_prefix}Command(BaseModel):",
                    '    model_config = ConfigDict(extra="forbid", frozen=True)',
                    "    actor_id: str",
                    "",
                    f"def apply_{mod_name}_command(",
                    f"    state: {cls_prefix}State,",
                    f"    command: {cls_prefix}Command,",
                    f") -> tuple[{cls_prefix}State, list[dict[str, str]]]:",
                    f'    """Pure Functional Core transition for {req_ids_str} (zero I/O; Phase 3 PBT target)."""',
                    "    ...",
                    "```",
                    "",
                ]
            )
    else:
        lines.extend(["*(No software module interface contracts required for this session.)*", ""])

    lines.extend(
        [
            "## Socio-Technical Boundaries (Process & Training Exclusions)",
            "",
        ]
    )

    if non_software_items:
        lines.extend(
            [
                "The following requirements (or Hybrid slices) are **explicitly NOT implemented in automated software** and must be fulfilled via `Process_and_Training.md` prior to go-live:",
                "",
                "| Req ID | Track | Routing ADR | Responsible Roles | Boundary / Non-Software Scope |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for item in non_software_items:
            r_adrs = [
                adr.adr_id
                for adr in adr_by_req.get(item.req_id, [])
                if adr.kind == "routing"
            ]
            adr_str = ", ".join(f"`{a}`" for a in r_adrs) or "*(none)*"
            roles_str = ", ".join(item.human_roles) or "Operations Lead"
            if item.track == TriageTrack.HYBRID and item.component_breakdown:
                non_sw_parts = [
                    f"{k.value}: {v}"
                    for k, v in item.component_breakdown.items()
                    if k != RoutingDecisionKind.SOFTWARE
                ]
                boundary = "; ".join(non_sw_parts) or item.rationale
            else:
                boundary = item.rationale
            boundary = boundary.replace("|", "\\|")
            lines.append(
                f"| `{item.req_id}` | `{item.track.value}` | {adr_str} | {roles_str} | {boundary} |"
            )
        lines.append("")
    else:
        lines.extend(
            [
                "All active requirements were routed purely to `software`; no Process or Training exclusions apply.",
                "",
            ]
        )

    if assumption_adrs:
        lines.extend(
            [
                "## Unconfirmed Assumptions Requiring Sign-Off",
                "",
                "The following Assumption ADRs (`user_confirmed: false`) constrain this design until confirmed by stakeholders:",
                "",
            ]
        )
        for adr in assumption_adrs:
            req_list = ", ".join(
                f"`{r}`" for r in (adr.frontmatter.get("requirement_ids") or [])
            ) or "*(general)*"
            lines.append(
                f"- **`{adr.adr_id}`** (`user_confirmed: false`): {adr.frontmatter.get('title', adr.adr_id)} — affects {req_list}"
            )
        lines.append("")

    return "\n".join(lines)


__all__ = ["emit_design_md"]
