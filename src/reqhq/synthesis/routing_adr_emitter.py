"""Pure-Python YAML frontmatter & Routing/Assumption ADR emitters for Stage 4a/5a."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import re
from typing import Any

from reqhq.schemas.requirements import RequirementGraph, RequirementNode, RequirementStatus
from reqhq.schemas.routing_adr import RoutingADR, RoutingDecisionKind, TraceabilityChain
from reqhq.synthesis.models import (
    AssumptionLike,
    EmittedADRFile,
    TriageItem,
    TriagePlan,
    TriageTrack,
)

_SHA256_LEN = 64
_YAML_RESERVED_WORDS: frozenset[str] = frozenset(
    {"true", "false", "null", "none", "yes", "no", "on", "off", "~"}
)
_YAML_SPECIAL_CHARS = set(":#[]{},&*!|>'\"%@`")
_INT_RE = re.compile(r"^[+-]?[0-9]+$")
_FLOAT_RE = re.compile(r"^[+-]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
_HEX_ONLY_RE = re.compile(r"^[0-9a-fA-F]{16,}$")


def _needs_yaml_quoting(text: str) -> bool:
    if text == "":
        return True
    if text != text.strip():
        return True
    if "\n" in text or "\r" in text or "\t" in text:
        return True
    lowered = text.lower()
    if lowered in _YAML_RESERVED_WORDS:
        return True
    if _INT_RE.match(text) or _FLOAT_RE.match(text) or _HEX_ONLY_RE.match(text):
        return True
    if text[0] in ("-", "?", " "):
        return True
    if any(ch in _YAML_SPECIAL_CHARS for ch in text):
        return True
    return False


def _format_yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if _needs_yaml_quoting(text):
        return json.dumps(text, ensure_ascii=False)
    return text


def _dump_yaml_lines(data: Mapping[str, Any], indent: int = 0) -> list[str]:
    prefix = " " * indent
    lines: list[str] = []
    for key, value in data.items():
        key_str = str(key)
        if isinstance(value, Mapping):
            if not value:
                lines.append(f"{prefix}{key_str}: {{}}")
            else:
                lines.append(f"{prefix}{key_str}:")
                lines.extend(_dump_yaml_lines(value, indent=indent + 2))
        elif isinstance(value, list):
            if not value:
                lines.append(f"{prefix}{key_str}: []")
            else:
                lines.append(f"{prefix}{key_str}:")
                item_prefix = " " * (indent + 2)
                for item in value:
                    if isinstance(item, Mapping):
                        if not item:
                            lines.append(f"{item_prefix}- {{}}")
                        else:
                            first = True
                            for sub_k, sub_v in item.items():
                                if isinstance(sub_v, (Mapping, list)):
                                    rendered = json.dumps(sub_v, ensure_ascii=False)
                                else:
                                    rendered = _format_yaml_scalar(sub_v)
                                if first:
                                    lines.append(f"{item_prefix}- {sub_k}: {rendered}")
                                    first = False
                                else:
                                    lines.append(f"{item_prefix}  {sub_k}: {rendered}")
                    else:
                        lines.append(f"{item_prefix}- {_format_yaml_scalar(item)}")
        else:
            lines.append(f"{prefix}{key_str}: {_format_yaml_scalar(value)}")
    return lines


def dump_yaml_frontmatter(data: Mapping[str, Any]) -> str:
    """Serialize a mapping into a strict LF-delimited YAML frontmatter block."""
    body_lines = _dump_yaml_lines(data, indent=0)
    return "---\n" + "\n".join(body_lines) + "\n---\n"


def _parse_yaml_scalar(raw: str) -> Any:
    text = raw.strip()
    if text.startswith('"'):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid double-quoted YAML scalar: {raw!r}") from exc
    if text.startswith("'"):
        if len(text) < 2 or not text.endswith("'"):
            raise ValueError(f"Unterminated single-quoted YAML scalar: {raw!r}")
        return text[1:-1].replace("''", "'")
    if text == "[]":
        return []
    if text == "{}":
        return {}
    if text.startswith("["):
        if not text.endswith("]"):
            raise ValueError(f"Invalid inline YAML list: {raw!r}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid inline YAML list: {raw!r}") from exc
    if text.startswith("{"):
        if not text.endswith("}"):
            raise ValueError(f"Invalid inline YAML mapping: {raw!r}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid inline YAML mapping: {raw!r}") from exc

    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in ("null", "~"):
        return None
    if _INT_RE.match(text):
        return int(text)
    if _FLOAT_RE.match(text):
        return float(text)
    return text


def _split_key_value(line: str) -> tuple[str, str]:
    """Split a YAML mapping line 'key: value' outside of quotes."""
    in_single = False
    in_double = False
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_double:
            escaped = True
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            continue
        if ch == ":" and not in_single and not in_double:
            next_ch = line[idx + 1] if idx + 1 < len(line) else ""
            if next_ch in ("", " "):
                key = line[:idx].strip()
                val = line[idx + 1 :].strip()
                if not key:
                    raise ValueError(f"Empty YAML key in line: {line!r}")
                if (key.startswith('"') and key.endswith('"')) or (
                    key.startswith("'") and key.endswith("'")
                ):
                    key = str(_parse_yaml_scalar(key))
                return key, val
    raise ValueError(f"Expected 'key: value' YAML entry, got: {line!r}")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def parse_yaml_frontmatter(yaml_text: str) -> dict[str, Any]:
    """Parse strict pure-Python YAML frontmatter (mappings, lists, lists of dicts, scalars)."""
    raw_lines = yaml_text.replace("\r\n", "\n").split("\n")
    lines: list[tuple[int, str]] = []
    for raw_line in raw_lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        leading_ws = raw_line[: len(raw_line) - len(raw_line.lstrip(" \t"))]
        if "\t" in leading_ws:
            raise ValueError(f"Tabs are not permitted for YAML indentation: {raw_line!r}")
        lines.append((_indent_of(raw_line), stripped))

    if not lines:
        return {}

    def _parse_block(index: int) -> tuple[Any, int]:
        indent, content = lines[index]
        if content.startswith("- ") or content == "-":
            return _parse_list(index, indent)
        return _parse_mapping(index, indent)

    def _parse_list(index: int, list_indent: int) -> tuple[list[Any], int]:
        items: list[Any] = []
        while index < len(lines):
            indent, content = lines[index]
            if indent < list_indent:
                break
            if indent > list_indent:
                raise ValueError(f"Unexpected list indentation at: {content!r}")
            if not (content.startswith("- ") or content == "-"):
                break

            remainder = content[2:].strip() if content.startswith("- ") else ""
            index += 1

            if remainder == "":
                if index < len(lines) and lines[index][0] > list_indent:
                    nested_val, index = _parse_block(index)
                    items.append(nested_val)
                else:
                    items.append(None)
                continue

            # Detect inline mapping start: `- key: value`
            if not remainder.startswith(('"', "'", "[", "{")):
                try:
                    sub_key, sub_val_str = _split_key_value(remainder)
                except ValueError:
                    items.append(_parse_yaml_scalar(remainder))
                    continue

                item_dict: dict[str, Any] = {}
                if sub_val_str == "":
                    if index < len(lines) and lines[index][0] > list_indent + 2:
                        nested, index = _parse_block(index)
                        item_dict[sub_key] = nested
                    else:
                        item_dict[sub_key] = None
                else:
                    item_dict[sub_key] = _parse_yaml_scalar(sub_val_str)

                entry_indent = list_indent + 2
                while index < len(lines) and lines[index][0] >= entry_indent:
                    sub_indent, sub_line = lines[index]
                    if sub_indent != entry_indent:
                        raise ValueError(
                            f"Unexpected indentation in list-item mapping: {sub_line!r}"
                        )
                    k2, v2_str = _split_key_value(sub_line)
                    index += 1
                    if v2_str == "":
                        if index < len(lines) and lines[index][0] > entry_indent:
                            nested_val, index = _parse_block(index)
                            item_dict[k2] = nested_val
                        else:
                            item_dict[k2] = None
                    else:
                        item_dict[k2] = _parse_yaml_scalar(v2_str)
                items.append(item_dict)
            else:
                items.append(_parse_yaml_scalar(remainder))
        return items, index

    def _parse_mapping(index: int, map_indent: int) -> tuple[dict[str, Any], int]:
        mapping: dict[str, Any] = {}
        while index < len(lines):
            indent, content = lines[index]
            if indent < map_indent:
                break
            if indent > map_indent:
                raise ValueError(f"Unexpected mapping indentation at: {content!r}")
            if content.startswith("- ") or content == "-":
                break

            key, val_str = _split_key_value(content)
            index += 1
            if val_str == "":
                if index < len(lines) and lines[index][0] > map_indent:
                    nested_val, index = _parse_block(index)
                    mapping[key] = nested_val
                else:
                    mapping[key] = None
            else:
                mapping[key] = _parse_yaml_scalar(val_str)
        return mapping, index

    root, next_idx = _parse_mapping(0, lines[0][0])
    if next_idx != len(lines):
        raise ValueError(f"Unparsed trailing YAML content at: {lines[next_idx][1]!r}")
    return root


def parse_emitted_adr(markdown: str) -> tuple[dict[str, Any], str]:
    """Split a generated Routing/Assumption ADR Markdown document into (frontmatter, body)."""
    normalized = markdown.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        raise ValueError("ADR markdown must begin with a '---' YAML frontmatter delimiter")
    end_marker = "\n---\n"
    end_pos = normalized.find(end_marker, 4)
    if end_pos == -1:
        if normalized.endswith("\n---"):
            yaml_block = normalized[4:-4]
            body = ""
        else:
            raise ValueError("ADR markdown is missing closing '---' YAML frontmatter delimiter")
    else:
        yaml_block = normalized[4:end_pos]
        body = normalized[end_pos + len(end_marker) :]
    frontmatter = parse_yaml_frontmatter(yaml_block)
    return frontmatter, body


def _resolve_node_source_trace(node: RequirementNode) -> tuple[str, str]:
    """Return primary (source_sha256, span) for a RequirementNode."""
    if node.trace_links:
        first = node.trace_links[0]
        span = first.anchor.strip() or first.excerpt.strip() or first.source_uri
        return first.source_sha256, span
    fallback_sha = hashlib.sha256(
        f"{node.req_id}:{node.statement}".encode("utf-8")
    ).hexdigest()
    return fallback_sha, f"synthetic:{node.req_id}"


def _build_routing_adr_object(
    adr_id: str,
    node: RequirementNode,
    item: TriageItem,
    *,
    alloy_ref_list: list[str],
    pbt_ref_list: list[str],
) -> tuple[RoutingADR, str, str]:
    primary_sha, primary_span = _resolve_node_source_trace(node)
    primary_alloy = alloy_ref_list[0] if alloy_ref_list else None
    primary_pbt = pbt_ref_list[0] if pbt_ref_list else None

    seen_shas: list[str] = []
    if node.trace_links:
        for link in node.trace_links:
            if link.source_sha256 not in seen_shas:
                seen_shas.append(link.source_sha256)
    else:
        seen_shas.append(primary_sha)

    trace_chains = [
        TraceabilityChain(
            source_sha256=sha,
            req_id=node.req_id,
            adr_id=adr_id,
            alloy_model_ref=primary_alloy,
            pbt_spec_id=primary_pbt,
        )
        for sha in seen_shas
    ]

    decision_kind = item.track.to_routing_kind()
    title = f"Route {node.req_id} ({node.title}) to {decision_kind.value}"

    risks: list[str] = []
    if item.prerequisites:
        risks.append(
            "Go-live blocked if non-software prerequisites remain incomplete: "
            + "; ".join(item.prerequisites)
        )
    if item.track in (TriageTrack.PROCESS, TriageTrack.TRAINING, TriageTrack.HYBRID):
        risks.append(
            f"Human execution drift across roles ({', '.join(item.human_roles) or 'Operations Lead'}) "
            "requires periodic SOP/training audit."
        )
    if not risks:
        risks.append(
            f"Implementation drift in `{item.software_module}` mitigated via Phase 3 Hypothesis PBT invariants."
        )

    consequences: list[str] = [
        f"Requirement {node.req_id} is routed to the {decision_kind.value} track.",
    ]
    if item.software_module:
        consequences.append(
            f"Software logic isolated in pure Functional Core module `{item.software_module}`."
        )
    if item.human_roles:
        consequences.append(
            f"Operational/training ownership assigned to: {', '.join(item.human_roles)}."
        )
    for prereq in item.prerequisites:
        consequences.append(f"Go-live prerequisite: {prereq}")

    routing_adr = RoutingADR(
        adr_id=adr_id,
        title=title,
        decision=decision_kind,
        rationale=item.rationale,
        req_ids=[node.req_id],
        traceability=trace_chains,
        component_breakdown=dict(item.component_breakdown),
        risks=risks,
        consequences=consequences,
        cloud_verified=item.cloud_verified,
    )
    return routing_adr, primary_sha, primary_span


def _render_routing_adr_markdown(
    routing_adr: RoutingADR,
    node: RequirementNode,
    item: TriageItem,
    frontmatter: dict[str, Any],
) -> str:
    yaml_header = dump_yaml_frontmatter(frontmatter)
    lines: list[str] = [
        f"# {routing_adr.adr_id}: {routing_adr.title}",
        "",
        "## Context",
        "",
        f"- **Requirement**: `{node.req_id}` — {node.title}",
        f"- **Kind / Priority / Status**: `{node.kind.value}` / `{node.priority}` / `{node.status.value}`",
        f"- **Source SHA-256**: `{frontmatter['source_sha256']}`",
        f"- **Source Span**: `{frontmatter['span']}`",
        "",
        f"> {node.statement}",
        "",
        "## Routing Decision",
        "",
        f"- **Decision Track**: `{routing_adr.decision.value}` (`{item.track.value}`)",
        f"- **Cloud Verified**: `{str(routing_adr.cloud_verified).lower()}`",
        f"- **Software Module**: `{item.software_module or 'none (non-software track)'}`",
        f"- **Human Roles**: {', '.join(item.human_roles) if item.human_roles else 'none'}",
        "",
        routing_adr.rationale,
        "",
    ]

    if routing_adr.component_breakdown:
        lines.extend(["### Hybrid Component Breakdown", ""])
        for kind, desc in routing_adr.component_breakdown.items():
            lines.append(f"- **{kind.value}**: {desc}")
        lines.append("")

    lines.extend(["## Rejected Alternatives", ""])
    if item.rejected_alternatives:
        for alt in item.rejected_alternatives:
            lines.append(f"- {alt}")
    else:
        lines.append("- *(none recorded)*")
    lines.append("")

    lines.extend(["## Non-Software Go-Live Prerequisites", ""])
    if item.prerequisites:
        for prereq in item.prerequisites:
            lines.append(f"- [ ] {prereq}")
    else:
        lines.append("- None (pure software track).")
    lines.append("")

    lines.extend(["## Consequences & Risks", ""])
    for cons in routing_adr.consequences:
        lines.append(f"- **Consequence**: {cons}")
    for risk in routing_adr.risks:
        lines.append(f"- **Risk**: {risk}")
    lines.append("")

    lines.extend(
        [
            "## Traceability Chain",
            "",
            "| Field | Value |",
            "| --- | --- |",
            f"| `source_sha256` | `{frontmatter['source_sha256']}` |",
            f"| `span` | `{frontmatter['span']}` |",
            f"| `requirement_ids` | {', '.join(f'`{r}`' for r in frontmatter['requirement_ids'])} |",
            f"| `adr_id` | `{routing_adr.adr_id}` |",
            "| `alloy_refs` | "
            + (
                ", ".join(f"`{a}`" for a in frontmatter["alloy_refs"])
                if frontmatter["alloy_refs"]
                else "*(none)*"
            )
            + " |",
            "| `pbt_refs` | "
            + (
                ", ".join(f"`{p}`" for p in frontmatter["pbt_refs"])
                if frontmatter["pbt_refs"]
                else "*(pending Phase 3)*"
            )
            + " |",
            "",
        ]
    )

    return yaml_header + "\n" + "\n".join(lines)


def build_routing_adrs(
    graph: RequirementGraph,
    plan: TriagePlan,
    *,
    alloy_refs: Mapping[str, Sequence[str]] | None = None,
    pbt_refs: Mapping[str, Sequence[str]] | None = None,
) -> list[EmittedADRFile]:
    """Emit validated ADR-R001.md.. target-system Routing ADRs from a TriagePlan."""
    alloy_map = alloy_refs or {}
    pbt_map = pbt_refs or {}
    emitted: list[EmittedADRFile] = []

    for index, item in enumerate(plan.items, start=1):
        node = graph.get_node(item.req_id)
        if node is None:
            raise ValueError(
                f"TriagePlan references req_id {item.req_id!r} which does not exist in RequirementGraph"
            )

        adr_id = f"ADR-R{index:03d}"
        req_alloy_refs = [str(ref).strip() for ref in alloy_map.get(item.req_id, ()) if str(ref).strip()]
        req_pbt_refs = [str(ref).strip() for ref in pbt_map.get(item.req_id, ()) if str(ref).strip()]

        routing_adr, primary_sha, primary_span = _build_routing_adr_object(
            adr_id,
            node,
            item,
            alloy_ref_list=req_alloy_refs,
            pbt_ref_list=req_pbt_refs,
        )

        status_label = (
            "Accepted"
            if node.status in (RequirementStatus.ACCEPTED, RequirementStatus.VERIFIED)
            else "Proposed"
        )
        frontmatter: dict[str, Any] = {
            "adr_id": routing_adr.adr_id,
            "type": "routing",
            "title": routing_adr.title,
            "status": status_label,
            "decision": routing_adr.decision.value,
            "track": item.track.value,
            "requirement_ids": list(routing_adr.req_ids),
            "source_sha256": primary_sha,
            "span": primary_span,
            "alloy_refs": req_alloy_refs,
            "pbt_refs": req_pbt_refs,
            "cloud_verified": routing_adr.cloud_verified,
            "user_confirmed": True,
            "rationale": routing_adr.rationale,
            "component_breakdown": {
                kind.value: desc for kind, desc in routing_adr.component_breakdown.items()
            },
            "rejected_alternatives": list(item.rejected_alternatives),
            "prerequisites": list(item.prerequisites),
            "human_roles": list(item.human_roles),
            "software_module": item.software_module,
            "risks": list(routing_adr.risks),
            "consequences": list(routing_adr.consequences),
            "traceability": [
                link.model_dump(mode="json") for link in routing_adr.traceability
            ],
        }

        markdown = _render_routing_adr_markdown(routing_adr, node, item, frontmatter)
        # Deterministic round-trip validation check
        validated = validate_routing_adr_markdown(markdown)

        emitted.append(
            EmittedADRFile(
                adr_id=adr_id,
                kind="routing",
                filename=f"{adr_id}.md",
                frontmatter=frontmatter,
                markdown=markdown,
                routing_adr=validated,
            )
        )

    return emitted


def validate_routing_adr_markdown(markdown: str) -> RoutingADR:
    """Parse an emitted ADR-RXXX.md document and strictly validate it as a RoutingADR."""
    frontmatter, _body = parse_emitted_adr(markdown)

    adr_type = frontmatter.get("type")
    if adr_type != "routing":
        raise ValueError(
            f"Expected routing ADR frontmatter with type='routing', got {adr_type!r}"
        )

    for required_field in ("source_sha256", "span", "requirement_ids", "alloy_refs", "pbt_refs"):
        if required_field not in frontmatter:
            raise ValueError(
                f"Routing ADR frontmatter missing required traceability field: {required_field!r}"
            )

    raw_req_ids = frontmatter.get("requirement_ids")
    if not isinstance(raw_req_ids, list) or not raw_req_ids:
        raise ValueError("Routing ADR frontmatter 'requirement_ids' must be a non-empty list")

    raw_breakdown = frontmatter.get("component_breakdown") or {}
    if not isinstance(raw_breakdown, dict):
        raise ValueError("Routing ADR frontmatter 'component_breakdown' must be a mapping")
    breakdown: dict[RoutingDecisionKind, str] = {}
    for k, v in raw_breakdown.items():
        breakdown[RoutingDecisionKind(str(k))] = str(v)

    raw_trace = frontmatter.get("traceability") or []
    if not isinstance(raw_trace, list):
        raise ValueError("Routing ADR frontmatter 'traceability' must be a list")

    payload: dict[str, Any] = {
        "adr_id": frontmatter.get("adr_id", ""),
        "title": frontmatter.get("title", ""),
        "decision": frontmatter.get("decision", ""),
        "rationale": frontmatter.get("rationale", ""),
        "req_ids": [str(r) for r in raw_req_ids],
        "traceability": raw_trace,
        "component_breakdown": breakdown,
        "risks": [str(r) for r in (frontmatter.get("risks") or [])],
        "consequences": [str(c) for c in (frontmatter.get("consequences") or [])],
        "cloud_verified": bool(frontmatter.get("cloud_verified", False)),
    }
    return RoutingADR.model_validate(payload)


def _extract_assumption_fields(
    raw: AssumptionLike | Mapping[str, Any],
    fallback_index: int,
) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        getter = raw.get
    else:
        getter = lambda key, default=None: getattr(raw, key, default)  # noqa: E731

    raw_user_confirmed = getter("user_confirmed", False)
    if raw_user_confirmed is True:
        raise ValueError(
            "Assumption ADR must have user_confirmed=False; confirmed decisions belong in requirements or Routing ADRs."
        )

    raw_id = str(getter("assumption_id", "") or "").strip()
    adr_id = raw_id if raw_id else f"ADR-A{fallback_index:03d}"
    title = str(getter("title", "") or "").strip()
    statement = str(getter("statement", "") or "").strip()
    rationale = str(getter("rationale", "") or "").strip()
    if not title:
        raise ValueError(f"Assumption {adr_id!r} must have a non-blank title")
    if not statement:
        raise ValueError(f"Assumption {adr_id!r} must have a non-blank statement")
    if not rationale:
        rationale = "Auto-accepted recommended default pending stakeholder confirmation."

    req_ids = [str(r).strip() for r in (getter("req_ids", []) or []) if str(r).strip()]
    finding_ids = [
        str(f).strip() for f in (getter("finding_ids", []) or []) if str(f).strip()
    ]

    raw_sha = str(getter("source_sha256", "") or "").strip().lower()
    if not raw_sha:
        raw_sha = hashlib.sha256(f"{adr_id}:{title}:{statement}".encode("utf-8")).hexdigest()
    elif len(raw_sha) != _SHA256_LEN or any(c not in "0123456789abcdef" for c in raw_sha):
        raise ValueError(
            f"Assumption {adr_id!r} has invalid source_sha256: expected 64 hex chars, got {raw_sha!r}"
        )

    return {
        "adr_id": adr_id,
        "title": title,
        "statement": statement,
        "req_ids": req_ids,
        "finding_ids": finding_ids,
        "source_sha256": raw_sha,
        "rationale": rationale,
        "type": "assumption",
        "user_confirmed": False,
    }


def validate_assumption_adr_markdown(markdown: str) -> dict[str, Any]:
    """Parse an emitted ADR-AXXX.md document and strictly verify assumption invariants."""
    frontmatter, _body = parse_emitted_adr(markdown)
    if frontmatter.get("type") != "assumption" or frontmatter.get("user_confirmed") is not False:
        raise ValueError(
            "Assumption ADR frontmatter must have type='assumption' and user_confirmed=false"
        )
    return frontmatter


def build_assumption_adrs(
    assumptions: Sequence[AssumptionLike | Mapping[str, Any]],
) -> list[EmittedADRFile]:
    """Emit ADR-A001.md.. Assumption ADRs strictly tagged type='assumption' & user_confirmed=false."""
    emitted: list[EmittedADRFile] = []
    seen_ids: set[str] = set()

    for idx, raw in enumerate(assumptions, start=1):
        fields = _extract_assumption_fields(raw, idx)
        adr_id = fields["adr_id"]
        if adr_id in seen_ids:
            candidate_num = idx
            adr_id = f"ADR-A{candidate_num:03d}"
            while adr_id in seen_ids:
                candidate_num += 1
                adr_id = f"ADR-A{candidate_num:03d}"
            fields["adr_id"] = adr_id
        seen_ids.add(adr_id)

        frontmatter: dict[str, Any] = {
            "adr_id": adr_id,
            "type": "assumption",
            "user_confirmed": False,
            "status": "Unconfirmed Assumption",
            "title": fields["title"],
            "requirement_ids": fields["req_ids"],
            "finding_ids": fields["finding_ids"],
            "source_sha256": fields["source_sha256"],
        }

        yaml_header = dump_yaml_frontmatter(frontmatter)
        body_lines = [
            f"# {adr_id}: {fields['title']}",
            "",
            "> **UNCONFIRMED ASSUMPTION (`user_confirmed: false`)**: "
            "This decision was auto-accepted from recommended defaults during finalization "
            "and MUST be reviewed and signed off by stakeholders prior to production launch.",
            "",
            "## Assumption Statement",
            "",
            fields["statement"],
            "",
            "## Rationale",
            "",
            fields["rationale"],
            "",
            "## Traceability & Linked Findings",
            "",
            f"- **Requirement IDs**: {', '.join(f'`{r}`' for r in fields['req_ids']) if fields['req_ids'] else '*(none)*'}",
            f"- **Critic Finding IDs**: {', '.join(f'`{f}`' for f in fields['finding_ids']) if fields['finding_ids'] else '*(none)*'}",
            f"- **Source SHA-256**: `{fields['source_sha256']}`",
            "",
            "## Stakeholder Confirmation Checklist",
            "",
            f"- [ ] Confirm or override assumption `{adr_id}` with domain owner.",
            "- [ ] Update affected requirements and re-run verification if assumption is rejected.",
            "",
        ]
        markdown = yaml_header + "\n" + "\n".join(body_lines)
        validate_assumption_adr_markdown(markdown)

        emitted.append(
            EmittedADRFile(
                adr_id=adr_id,
                kind="assumption",
                filename=f"{adr_id}.md",
                frontmatter=frontmatter,
                markdown=markdown,
                routing_adr=None,
            )
        )

    return emitted


__all__ = [
    "build_assumption_adrs",
    "build_routing_adrs",
    "dump_yaml_frontmatter",
    "parse_emitted_adr",
    "parse_yaml_frontmatter",
    "validate_assumption_adr_markdown",
    "validate_routing_adr_markdown",
]
