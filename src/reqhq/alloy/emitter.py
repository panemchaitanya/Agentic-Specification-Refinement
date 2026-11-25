"""Two-Layer Literate Alloy 6 / Legacy Spec.md Emitter and static version-agnostic linter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

from reqhq.alloy.models import AlloyCompatMode
from reqhq.llm.providers.base import LLMRequest
from reqhq.llm.router import ProviderRouter
from reqhq.schemas.requirements import (
    RequirementGraph,
    RequirementKind,
    RequirementNode,
    RequirementStatus,
)

ALLOY6_TEMPORAL_KEYWORDS: tuple[str, ...] = (
    "var",
    "always",
    "eventually",
    "after",
    "until",
    "once",
    "historically",
    "since",
    "triggered",
    "releases",
    "before",
)

_TEMPORAL_WORD_RE = re.compile(
    r"\b(" + "|".join(ALLOY6_TEMPORAL_KEYWORDS) + r")\b"
)

_ALLOY_RESERVED = frozenset(
    set(ALLOY6_TEMPORAL_KEYWORDS)
    | {
        "abstract",
        "all",
        "and",
        "as",
        "assert",
        "but",
        "check",
        "disj",
        "else",
        "enum",
        "exactly",
        "extends",
        "fact",
        "for",
        "fun",
        "iden",
        "iff",
        "implies",
        "in",
        "Int",
        "let",
        "lone",
        "module",
        "no",
        "none",
        "not",
        "one",
        "open",
        "or",
        "pred",
        "run",
        "set",
        "sig",
        "some",
        "sum",
        "this",
        "univ",
    }
)


def _strip_comments_and_strings_preserving_lines(source: str) -> str:
    """Replace Alloy comments (`//`, `--`, `/* ... */`) and string literals with spaces.

    Newlines are preserved so line numbers remain exact for lint diagnostics.
    """
    out: list[str] = []
    i = 0
    n = len(source)
    in_block_comment = False
    in_string = False

    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""

        if in_block_comment:
            if ch == "*" and nxt == "/":
                out.append("  ")
                i += 2
                in_block_comment = False
            elif ch == "\n":
                out.append("\n")
                i += 1
            else:
                out.append(" ")
                i += 1
            continue

        if in_string:
            if ch == "\\" and i + 1 < n:
                out.append("  ")
                i += 2
            elif ch == '"':
                out.append(" ")
                i += 1
                in_string = False
            elif ch == "\n":
                out.append("\n")
                i += 1
            else:
                out.append(" ")
                i += 1
            continue

        if ch == "/" and nxt == "/":
            out.append("  ")
            i += 2
            while i < n and source[i] != "\n":
                out.append(" ")
                i += 1
            continue

        if ch == "-" and nxt == "-":
            out.append("  ")
            i += 2
            while i < n and source[i] != "\n":
                out.append(" ")
                i += 1
            continue

        if ch == "/" and nxt == "*":
            out.append("  ")
            i += 2
            in_block_comment = True
            continue

        if ch == '"':
            out.append(" ")
            i += 1
            in_string = True
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def lint_core_domain_version_agnostic(core_als: str) -> list[str]:
    """Reject Alloy 6 temporal keywords and prime (`'`) outside comments/strings.

    Returns a list of diagnostic error strings (empty list means valid version-agnostic Alloy).
    """
    sanitized = _strip_comments_and_strings_preserving_lines(core_als)
    errors: list[str] = []

    for line_no, line in enumerate(sanitized.splitlines(), start=1):
        for match in _TEMPORAL_WORD_RE.finditer(line):
            kw = match.group(1)
            errors.append(
                f"Line {line_no}: forbidden Alloy 6 temporal keyword '{kw}' in version-agnostic layer"
            )
        if "'" in line:
            errors.append(
                f"Line {line_no}: forbidden Alloy 6 prime operator \"'\" in version-agnostic layer"
            )

    return errors


def _sanitize_alloy_ident(raw: str, *, prefix: str = "Item") -> str:
    """Convert an arbitrary REQ-ID or title into a safe, version-agnostic Alloy identifier."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", raw.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"{prefix}_{cleaned}" if cleaned else prefix
    if cleaned in _ALLOY_RESERVED or cleaned.lower() in {k.lower() for k in ALLOY6_TEMPORAL_KEYWORDS}:
        cleaned = f"{prefix}_{cleaned}"
    return cleaned


def _extract_triage_map(triage_plan: Any) -> dict[str, dict[str, Any]]:
    """Duck-typed extraction of per-REQ-ID triage metadata from a TriagePlan or dict."""
    if triage_plan is None:
        return {}
    if isinstance(triage_plan, Mapping):
        items = triage_plan.get("items", [])
    else:
        items = getattr(triage_plan, "items", None)
    if not items:
        return {}

    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if isinstance(item, Mapping):
            req_id = str(item.get("req_id", "")).strip()
            raw_track = item.get("track", "software")
            rationale = str(item.get("rationale", ""))
            human_roles = list(item.get("human_roles", []))
            prerequisites = list(item.get("prerequisites", []))
            software_module = str(item.get("software_module", ""))
        else:
            req_id = str(getattr(item, "req_id", "")).strip()
            raw_track = getattr(item, "track", "software")
            rationale = str(getattr(item, "rationale", ""))
            human_roles = list(getattr(item, "human_roles", []) or [])
            prerequisites = list(getattr(item, "prerequisites", []) or [])
            software_module = str(getattr(item, "software_module", "") or "")
        if not req_id:
            continue
        track_val = getattr(raw_track, "value", raw_track)
        result[req_id] = {
            "track": str(track_val).strip().lower(),
            "rationale": rationale,
            "human_roles": [str(r) for r in human_roles],
            "prerequisites": [str(p) for p in prerequisites],
            "software_module": software_module,
        }
    return result


def _extract_adr_links(adrs: Sequence[Any] | None) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
    """Duck-typed extraction of REQ-ID -> ADR IDs and ADR summary entries."""
    req_to_adrs: dict[str, list[str]] = {}
    adr_entries: list[dict[str, str]] = []
    if not adrs:
        return req_to_adrs, adr_entries

    for adr in adrs:
        adr_id = ""
        filename = ""
        decision = ""
        title = ""
        req_ids: list[str] = []

        if isinstance(adr, Mapping):
            adr_id = str(adr.get("adr_id", "")).strip()
            filename = str(adr.get("filename", "")).strip()
            decision = str(adr.get("decision", adr.get("kind", ""))).strip()
            title = str(adr.get("title", "")).strip()
            req_ids = [str(r) for r in adr.get("req_ids", [])]
            fm = adr.get("frontmatter")
            if isinstance(fm, Mapping):
                if not req_ids and isinstance(fm.get("requirement_ids"), list):
                    req_ids = [str(r) for r in fm["requirement_ids"]]
                if not decision and fm.get("decision"):
                    decision = str(fm["decision"])
        else:
            adr_id = str(getattr(adr, "adr_id", "")).strip()
            filename = str(getattr(adr, "filename", "") or "").strip()
            routing_adr = getattr(adr, "routing_adr", None)
            if routing_adr is not None:
                raw_dec = getattr(routing_adr, "decision", "")
                decision = str(getattr(raw_dec, "value", raw_dec))
                title = str(getattr(routing_adr, "title", ""))
                req_ids = [str(r) for r in getattr(routing_adr, "req_ids", [])]
            else:
                raw_dec = getattr(adr, "decision", getattr(adr, "kind", ""))
                decision = str(getattr(raw_dec, "value", raw_dec))
                title = str(getattr(adr, "title", ""))
                req_ids = [str(r) for r in getattr(adr, "req_ids", []) or []]
                fm = getattr(adr, "frontmatter", None)
                if isinstance(fm, Mapping):
                    if not req_ids and isinstance(fm.get("requirement_ids"), list):
                        req_ids = [str(r) for r in fm["requirement_ids"]]
                    if not title and fm.get("title"):
                        title = str(fm["title"])

        if not adr_id:
            continue
        ref_path = f"ADRs/{filename}" if filename else f"ADRs/{adr_id}.md"
        adr_entries.append(
            {
                "adr_id": adr_id,
                "path": ref_path,
                "decision": decision or "Routing",
                "title": title or adr_id,
                "req_ids": ", ".join(req_ids),
            }
        )
        for rid in req_ids:
            bucket = req_to_adrs.setdefault(rid, [])
            if adr_id not in bucket:
                bucket.append(adr_id)

    return req_to_adrs, adr_entries


def _active_nodes(graph: RequirementGraph) -> list[RequirementNode]:
    return [
        node
        for node in graph.nodes
        if node.status not in (RequirementStatus.DEPRECATED, RequirementStatus.SUPERSEDED)
    ]


class AlloySpecEmitter:
    """Emit Two-Layer Literate Spec.md (Layer 1 core_domain + Layer 2 behavior)."""

    def __init__(self, router: ProviderRouter | None = None) -> None:
        self.router = router

    def _emit_core_domain(
        self,
        active_nodes: list[RequirementNode],
        triage_map: dict[str, dict[str, Any]],
    ) -> str:
        lines: list[str] = [
            "module core_domain",
            "",
            "// Layer 1: Version-agnostic relational domain ontology (Alloy 5 & Alloy 6 compatible).",
            "sig HumanActor {}",
            "sig TrainedRole {",
            "  assignedTo: some HumanActor",
            "}",
            "sig SystemEntity {",
            "  owner: one HumanActor,",
            "  governingRole: lone TrainedRole",
            "}",
        ]

        # Collect distinct human roles from triage_plan if present.
        extra_roles: list[str] = []
        seen_roles: set[str] = set()
        for meta in triage_map.values():
            for role in meta.get("human_roles", []):
                role_ident = _sanitize_alloy_ident(role, prefix="Role")
                if role_ident not in seen_roles and role_ident not in {
                    "HumanActor",
                    "TrainedRole",
                    "SystemEntity",
                }:
                    seen_roles.add(role_ident)
                    extra_roles.append(role_ident)

        for role_ident in extra_roles:
            lines.append(f"sig {role_ident} extends TrainedRole {{}}")

        lines.append("")
        lines.append("// Timeless requirement invariants with @trace markers.")

        if not active_nodes:
            lines.extend(
                [
                    "fact BaselineDomainIntegrity {",
                    "  all e: SystemEntity | some e.owner",
                    "}",
                ]
            )
        else:
            for node in active_nodes:
                safe_id = _sanitize_alloy_ident(node.req_id, prefix="Req")
                track = triage_map.get(node.req_id, {}).get(
                    "track",
                    "process" if node.kind == RequirementKind.PROCESS else "software",
                )
                comment_title = node.title.replace("\n", " ").strip()
                lines.append(f"// @trace {node.req_id}")
                lines.append(f"// {comment_title} [{node.kind.value} | track={track}]")
                if track in ("process", "training") or node.kind == RequirementKind.PROCESS:
                    lines.extend(
                        [
                            f"fact Inv_{safe_id} {{",
                            "  all r: TrainedRole | some r.assignedTo",
                            "}",
                            "",
                        ]
                    )
                elif node.kind == RequirementKind.SECURITY:
                    lines.extend(
                        [
                            f"fact Inv_{safe_id} {{",
                            "  all e: SystemEntity | some e.owner and (some e.governingRole implies some e.governingRole.assignedTo)",
                            "}",
                            "",
                        ]
                    )
                else:
                    lines.extend(
                        [
                            f"fact Inv_{safe_id} {{",
                            "  all e: SystemEntity | some e.owner",
                            "}",
                            "",
                        ]
                    )

        return "\n".join(lines).rstrip() + "\n"

    def _emit_behavior_alloy6(
        self,
        active_nodes: list[RequirementNode],
        max_scope: int,
        bitwidth: int,
    ) -> str:
        trace_list = ", ".join(node.req_id for node in active_nodes) or "REQ-BASE"
        lines: list[str] = [
            "module behavior",
            "open core_domain",
            "",
            "// Layer 2 (Alloy 6): Mutable state & LTL safety/liveness assertions.",
            "one sig SystemState {",
            "  var activeEntities: set SystemEntity,",
            "  var authorizedActors: set HumanActor",
            "}",
            "",
            "pred Init {",
            "  some SystemState.authorizedActors",
            "  SystemState.activeEntities.owner in SystemState.authorizedActors",
            "}",
            "",
            "pred ActivateEntity[e: SystemEntity] {",
            "  e.owner in SystemState.authorizedActors",
            "  SystemState.activeEntities' = SystemState.activeEntities + e",
            "  SystemState.authorizedActors' = SystemState.authorizedActors",
            "}",
            "",
            "pred Stutter {",
            "  SystemState.activeEntities' = SystemState.activeEntities",
            "  SystemState.authorizedActors' = SystemState.authorizedActors",
            "}",
            "",
            "fact Traces {",
            "  Init",
            "  always ((some e: SystemEntity | ActivateEntity[e]) or Stutter)",
            "}",
            "",
            f"// @trace {trace_list}",
            "assert SafetyAuthorizedOwnership {",
            "  always (SystemState.activeEntities.owner in SystemState.authorizedActors)",
            "}",
            f"check SafetyAuthorizedOwnership for {max_scope} but {bitwidth} int",
            "",
            f"// @trace {trace_list}",
            "assert LivenessReachableActivity {",
            "  always (some SystemState.activeEntities implies eventually (some SystemState.authorizedActors))",
            "}",
            f"check LivenessReachableActivity for {max_scope} but {bitwidth} int",
        ]
        return "\n".join(lines) + "\n"

    def _emit_behavior_legacy(
        self,
        active_nodes: list[RequirementNode],
        max_scope: int,
        bitwidth: int,
    ) -> str:
        trace_list = ", ".join(node.req_id for node in active_nodes) or "REQ-BASE"
        lines: list[str] = [
            "module behavior",
            "open core_domain",
            "open util/ordering[Time]",
            "",
            "// Layer 2 (Legacy Alloy 5): Explicit state-passing over ordered Time.",
            "sig Time {}",
            "",
            "sig State {",
            "  activeEntities: set SystemEntity,",
            "  authorizedActors: set HumanActor",
            "}",
            "",
            "one sig Trace {",
            "  state: Time -> one State",
            "}",
            "",
            "pred Init[s: State] {",
            "  some s.authorizedActors",
            "  s.activeEntities.owner in s.authorizedActors",
            "}",
            "",
            "pred Step[s, sNext: State] {",
            "  (some e: SystemEntity |",
            "    e.owner in s.authorizedActors and",
            "    sNext.activeEntities = s.activeEntities + e and",
            "    sNext.authorizedActors = s.authorizedActors",
            "  ) or (",
            "    sNext.activeEntities = s.activeEntities and",
            "    sNext.authorizedActors = s.authorizedActors",
            "  )",
            "}",
            "",
            "fact ValidTrace {",
            "  Init[Trace.state[first]]",
            "  all t: Time - last | Step[Trace.state[t], Trace.state[t.next]]",
            "}",
            "",
            f"// @trace {trace_list}",
            "assert SafetyAuthorizedOwnership {",
            "  all t: Time | Trace.state[t].activeEntities.owner in Trace.state[t].authorizedActors",
            "}",
            f"check SafetyAuthorizedOwnership for {max_scope} but {bitwidth} int",
        ]
        return "\n".join(lines) + "\n"

    def _optional_llm_notes(self, graph: RequirementGraph) -> str:
        """Optionally request a brief domain note via ProviderRouter when explicitly enabled."""
        if self.router is None:
            return ""
        summary_lines = [f"- {n.req_id}: {n.title}" for n in _active_nodes(graph)[:8]]
        req = LLMRequest(
            prompt=(
                f"Summarize the formal verification boundary for graph '{graph.graph_id}' "
                "in 2 concise bullet points:\n" + "\n".join(summary_lines)
            ),
            system_prompt="You are a concise formal methods engineer.",
            max_tokens=160,
            temperature=0.0,
        )
        try:
            resp = self.router.route(req)
        except Exception:
            return ""
        text = resp.text.strip()
        if not text or text.startswith("{"):
            return ""
        return text

    def emit_spec_md(
        self,
        graph: RequirementGraph,
        *,
        triage_plan: Any = None,
        adrs: Sequence[Any] | None = None,
        compat: AlloyCompatMode = AlloyCompatMode.ALLOY6,
        max_scope: int = 4,
        include_llm_notes: bool = False,
    ) -> str:
        """Emit a complete Two-Layer Literate Spec.md for `graph`."""
        if max_scope < 1:
            raise ValueError("max_scope must be >= 1")
        compat_mode = AlloyCompatMode(compat)
        bitwidth = min(4, max(2, max_scope))

        active = _active_nodes(graph)
        deprecated = [
            n
            for n in graph.nodes
            if n.status in (RequirementStatus.DEPRECATED, RequirementStatus.SUPERSEDED)
        ]
        triage_map = _extract_triage_map(triage_plan)
        req_to_adrs, adr_entries = _extract_adr_links(adrs)

        software_nodes: list[RequirementNode] = []
        non_software_nodes: list[RequirementNode] = []
        for node in active:
            track = triage_map.get(node.req_id, {}).get(
                "track",
                "process" if node.kind == RequirementKind.PROCESS else "software",
            )
            if track in ("process", "training"):
                non_software_nodes.append(node)
            else:
                software_nodes.append(node)

        nfr_nodes = [
            n
            for n in active
            if n.kind in (RequirementKind.NON_FUNCTIONAL, RequirementKind.SECURITY, RequirementKind.CONSTRAINT)
        ]
        api_nodes = [
            n
            for n in active
            if n.kind in (RequirementKind.INTERFACE, RequirementKind.FUNCTIONAL, RequirementKind.DATA)
        ]

        core_als = self._emit_core_domain(active, triage_map)
        if compat_mode == AlloyCompatMode.ALLOY6:
            behavior_als = self._emit_behavior_alloy6(active, max_scope, bitwidth)
        else:
            behavior_als = self._emit_behavior_legacy(active, max_scope, bitwidth)

        sections: list[str] = [
            f"# Specification: {graph.graph_id}",
            "",
            f"- **Graph Version:** `{graph.version}`",
            f"- **Alloy Compatibility Mode:** `{compat_mode.value}`",
            f"- **Bounded Verification Scope:** `scope <= {max_scope}`, `bitwidth <= {bitwidth}`",
            "",
            "## System Boundary",
            "",
            "In-scope software and hybrid requirements enforced by the formal model:",
            "",
        ]

        if software_nodes:
            for node in software_nodes:
                sections.append(f"- **{node.req_id}** ({node.priority}, `{node.kind.value}`): {node.title} — {node.statement}")
        else:
            sections.append("- No active software-routed requirements in this graph.")

        if include_llm_notes:
            llm_notes = self._optional_llm_notes(graph)
            if llm_notes:
                sections.extend(["", "### Verification Boundary Notes", "", llm_notes])

        sections.extend(["", "## Quantified NFR Budgets", ""])
        if nfr_nodes:
            sections.append("| REQ-ID | Category | Priority | Budget / Constraint |")
            sections.append("|---|---|---|---|")
            for node in nfr_nodes:
                stmt = node.statement.replace("|", "\\|")
                sections.append(f"| `{node.req_id}` | `{node.kind.value}` | `{node.priority}` | {stmt} |")
        else:
            sections.append("- Bounded formal verification budget: `max_scope <= 4`, `bitwidth <= 4`.")

        sections.extend(["", "## Traceability IDs", ""])
        sections.append("| REQ-ID | Title | Kind | Track | Source SHA-256 | ADR Links | Alloy Ref |")
        sections.append("|---|---|---|---|---|---|---|")
        for node in active:
            track = triage_map.get(node.req_id, {}).get(
                "track",
                "process" if node.kind == RequirementKind.PROCESS else "software",
            )
            sha_short = node.trace_links[0].source_sha256[:12] if node.trace_links else "unlinked"
            adr_list = ", ".join(req_to_adrs.get(node.req_id, [])) or "—"
            safe_id = _sanitize_alloy_ident(node.req_id, prefix="Req")
            title_cell = node.title.replace("|", "\\|")
            sections.append(
                f"| `{node.req_id}` | {title_cell} | `{node.kind.value}` | `{track}` "
                f"| `{sha_short}` | {adr_list} | `core_domain.als::Inv_{safe_id}` |"
            )

        sections.extend(["", "## API Contracts", ""])
        if api_nodes:
            for node in api_nodes:
                criteria = "; ".join(node.acceptance_criteria) if node.acceptance_criteria else "Satisfies formal domain invariants."
                sections.append(f"- **{node.req_id} — {node.title}**: {node.statement}")
                sections.append(f"  - Acceptance Criteria: {criteria}")
        else:
            sections.append("- No external API contracts declared.")

        sections.extend(["", "## Out-of-Scope / Non-Goals", ""])
        sections.append(
            "Requirements routed to organizational Process or Training tracks are governed outside "
            "the software runtime boundary and tracked via Process & Training Routing ADRs:"
        )
        sections.append("")
        if non_software_nodes:
            for node in non_software_nodes:
                track = triage_map.get(node.req_id, {}).get("track", "process")
                linked_adrs = req_to_adrs.get(node.req_id, [])
                adr_ref = ", ".join(f"[{a}](ADRs/{a}.md)" for a in linked_adrs) if linked_adrs else "Process & Training Routing ADR"
                sections.append(f"- **{node.req_id}** (`{track}`): {node.title} — routed via {adr_ref}")
        else:
            sections.append("- No active requirements routed exclusively to non-software Process/Training tracks.")

        if adr_entries:
            sections.extend(["", "### Linked Process & Training Routing ADRs", ""])
            for entry in adr_entries:
                sections.append(
                    f"- [{entry['adr_id']}]({entry['path']}) (`{entry['decision']}`): {entry['title']}"
                )

        if deprecated:
            sections.extend(["", "### Deprecated / Superseded Requirements", ""])
            for node in deprecated:
                sections.append(f"- `{node.req_id}` ({node.status.value}): {node.title}")

        sections.extend(
            [
                "",
                "## Formal Specification (Two-Layer Literate Alloy)",
                "",
                "### Layer 1: Version-Agnostic Core Domain (`core_domain`)",
                "",
                "```alloy module=core_domain",
                core_als.rstrip("\n"),
                "```",
                "",
                f"### Layer 2: Behavioral Model (`behavior`, mode=`{compat_mode.value}`)",
                "",
                "```alloy module=behavior",
                behavior_als.rstrip("\n"),
                "```",
                "",
            ]
        )

        return "\n".join(sections)


__all__ = [
    "ALLOY6_TEMPORAL_KEYWORDS",
    "AlloySpecEmitter",
    "lint_core_domain_version_agnostic",
]
