"""Deterministic pure-Python YAML serialization and Phase 3 PBT contract emitter."""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
import hashlib
import json
import keyword
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from reqhq.alloy.extractor import build_req_to_alloy_refs, extract_alloy_blocks
from reqhq.llm.providers.base import LLMRequest
from reqhq.llm.router import ProviderRouter
from reqhq.pbt.models import (
    GuardrailEnforcement,
    PBTContractDocument,
    PBTPropertyContract,
    PropertyKind,
)
from reqhq.schemas.pbt import (
    HypothesisStrategySpec,
    PBTSpec,
    PropertyInvariant,
    PydanticGuardrail,
)
from reqhq.schemas.requirements import (
    RequirementGraph,
    RequirementKind,
    RequirementNode,
    RequirementStatus,
)

# ---------------------------------------------------------------------------
# Deterministic pure-Python YAML serializer & parser (zero compiled deps)
# ---------------------------------------------------------------------------

_YAML_KEYWORDS = frozenset(
    {
        "null",
        "~",
        "true",
        "false",
        "yes",
        "no",
        "on",
        "off",
        ".inf",
        "-.inf",
        "+.inf",
        ".nan",
    }
)
_PLAIN_SCALAR_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_./+-]*(?: [A-Za-z0-9_./+-]+)*$")
_PLAIN_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_INT_RE = re.compile(r"^-?[0-9]+$")
_FLOAT_RE = re.compile(r"^-?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")


def _format_yaml_key(key: str) -> str:
    if _PLAIN_KEY_RE.match(key) and key.lower() not in _YAML_KEYWORDS:
        return key
    return json.dumps(key, ensure_ascii=True)


def _format_yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if _PLAIN_SCALAR_RE.match(value) and value.lower() not in _YAML_KEYWORDS:
            return value
        return json.dumps(value, ensure_ascii=True)
    raise TypeError(f"Unsupported YAML scalar type: {type(value).__name__}")


def _emit_yaml_lines(value: Any, indent: int = 0) -> list[str]:
    prefix = " " * indent
    if isinstance(value, dict):
        if not value:
            return [f"{prefix}{{}}"]
        lines: list[str] = []
        for raw_key, item in value.items():
            key_str = _format_yaml_key(str(raw_key))
            if isinstance(item, dict) and item:
                lines.append(f"{prefix}{key_str}:")
                lines.extend(_emit_yaml_lines(item, indent + 2))
            elif isinstance(item, list) and item:
                lines.append(f"{prefix}{key_str}:")
                lines.extend(_emit_yaml_lines(item, indent + 2))
            elif isinstance(item, dict):
                lines.append(f"{prefix}{key_str}: {{}}")
            elif isinstance(item, list):
                lines.append(f"{prefix}{key_str}: []")
            else:
                lines.append(f"{prefix}{key_str}: {_format_yaml_scalar(item)}")
        return lines

    if isinstance(value, list):
        if not value:
            return [f"{prefix}[]"]
        lines = []
        for elem in value:
            if isinstance(elem, dict) and elem:
                items = list(elem.items())
                first_k, first_v = items[0]
                first_key_str = _format_yaml_key(str(first_k))
                if isinstance(first_v, dict) and first_v:
                    lines.append(f"{prefix}- {first_key_str}:")
                    lines.extend(_emit_yaml_lines(first_v, indent + 4))
                elif isinstance(first_v, list) and first_v:
                    lines.append(f"{prefix}- {first_key_str}:")
                    lines.extend(_emit_yaml_lines(first_v, indent + 4))
                elif isinstance(first_v, dict):
                    lines.append(f"{prefix}- {first_key_str}: {{}}")
                elif isinstance(first_v, list):
                    lines.append(f"{prefix}- {first_key_str}: []")
                else:
                    lines.append(f"{prefix}- {first_key_str}: {_format_yaml_scalar(first_v)}")
                for rest_k, rest_v in items[1:]:
                    rest_dict = {rest_k: rest_v}
                    lines.extend(_emit_yaml_lines(rest_dict, indent + 2))
            elif isinstance(elem, list) and elem:
                lines.append(f"{prefix}-")
                lines.extend(_emit_yaml_lines(elem, indent + 2))
            elif isinstance(elem, dict):
                lines.append(f"{prefix}- {{}}")
            elif isinstance(elem, list):
                lines.append(f"{prefix}- []")
            else:
                lines.append(f"{prefix}- {_format_yaml_scalar(elem)}")
        return lines

    return [f"{prefix}{_format_yaml_scalar(value)}"]


def _strip_inline_comment(content: str) -> str:
    """Strip unquoted `# ...` comments while preserving `#` inside quotes."""
    in_double = False
    in_single = False
    escaped = False
    for idx, ch in enumerate(content):
        if in_double:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
        elif in_single:
            if ch == "'":
                in_single = False
        else:
            if ch == '"':
                in_double = True
            elif ch == "'":
                in_single = True
            elif ch == "#" and (idx == 0 or content[idx - 1].isspace()):
                return content[:idx].rstrip()
    return content.rstrip()


def _split_yaml_key_value(line: str) -> tuple[str, str] | None:
    """Split a YAML mapping line into `(key, value_rest)` respecting quoted keys."""
    if not line:
        return None
    in_double = False
    in_single = False
    escaped = False
    colon_idx: int | None = None

    for idx, ch in enumerate(line):
        if in_double:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
        elif in_single:
            if ch == "'":
                in_single = False
        else:
            if ch == '"':
                in_double = True
            elif ch == "'":
                in_single = True
            elif ch == ":" and (idx + 1 == len(line) or line[idx + 1].isspace()):
                colon_idx = idx
                break

    if colon_idx is None:
        return None

    raw_key = line[:colon_idx].strip()
    if not raw_key:
        raise ValueError(f"Empty YAML mapping key in line: {line!r}")

    if raw_key.startswith('"'):
        try:
            key = json.loads(raw_key)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid double-quoted YAML key {raw_key!r}: {exc}") from exc
    elif raw_key.startswith("'") and raw_key.endswith("'") and len(raw_key) >= 2:
        key = raw_key[1:-1].replace("''", "'")
    else:
        key = raw_key

    rest = line[colon_idx + 1 :].strip()
    return str(key), rest


def _split_flow_items(inner: str) -> list[str]:
    """Split comma-separated YAML flow collection items respecting quotes and nesting."""
    items: list[str] = []
    buf: list[str] = []
    depth = 0
    in_double = False
    in_single = False
    escaped = False

    for ch in inner:
        if in_double:
            buf.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
        elif in_single:
            buf.append(ch)
            if ch == "'":
                in_single = False
        else:
            if ch == '"':
                in_double = True
                buf.append(ch)
            elif ch == "'":
                in_single = True
                buf.append(ch)
            elif ch in "[{":
                depth += 1
                buf.append(ch)
            elif ch in "]}":
                depth -= 1
                buf.append(ch)
            elif ch == "," and depth == 0:
                items.append("".join(buf).strip())
                buf = []
            else:
                buf.append(ch)
    if depth != 0 or in_double or in_single:
        raise ValueError(f"Unbalanced YAML flow collection: {inner!r}")
    tail = "".join(buf).strip()
    if tail:
        items.append(tail)
    return items


def _parse_yaml_scalar(raw: str) -> Any:
    token = raw.strip()
    if not token:
        return None
    if token.startswith('"'):
        try:
            return json.loads(token)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid double-quoted YAML string {token!r}: {exc}") from exc
    if token.startswith("'"):
        if len(token) < 2 or not token.endswith("'"):
            raise ValueError(f"Unterminated single-quoted YAML string: {token!r}")
        return token[1:-1].replace("''", "'")
    if token == "[]":
        return []
    if token == "{}":
        return {}
    if token.startswith("["):
        if not token.endswith("]"):
            raise ValueError(f"Unbalanced YAML flow collection: {token!r}")
        inner = token[1:-1].strip()
        return [_parse_yaml_scalar(item) for item in _split_flow_items(inner)] if inner else []
    if token.startswith("{"):
        if not token.endswith("}"):
            raise ValueError(f"Unbalanced YAML flow collection: {token!r}")
        inner = token[1:-1].strip()
        result: dict[str, Any] = {}
        if not inner:
            return result
        for pair in _split_flow_items(inner):
            kv = _split_yaml_key_value(pair)
            if kv is None:
                raise ValueError(f"Invalid flow mapping entry: {pair!r}")
            result[kv[0]] = _parse_yaml_scalar(kv[1])
        return result

    lower = token.lower()
    if lower in ("null", "~"):
        return None
    if lower == "true":
        return True
    if lower == "false":
        return False
    if _INT_RE.match(token):
        return int(token)
    if _FLOAT_RE.match(token):
        return float(token)
    return token


def _tokenize_yaml_lines(yaml_text: str) -> list[tuple[int, str, int]]:
    tokens: list[tuple[int, str, int]] = []
    raw_lines = yaml_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for line_no, raw_line in enumerate(raw_lines, start=1):
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip(" \t"))]:
            raise ValueError(f"Tabs are not allowed in YAML indentation (line {line_no})")
        stripped = _strip_inline_comment(raw_line.lstrip(" "))
        if not stripped:
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        tokens.append((indent, stripped, line_no))
    return tokens


def _parse_yaml_sequence(
    tokens: list[tuple[int, str, int]],
    idx: int,
    base_indent: int,
) -> tuple[list[Any], int]:
    result: list[Any] = []
    total = len(tokens)

    while idx < total:
        indent, content, line_no = tokens[idx]
        if indent < base_indent:
            break
        if indent > base_indent:
            raise ValueError(f"Unexpected indentation at YAML line {line_no}")
        if not (content == "-" or content.startswith("- ")):
            break

        rest = content[2:].strip() if content.startswith("- ") else ""
        idx += 1

        if not rest:
            if idx < total and tokens[idx][0] > base_indent:
                nested, idx = _parse_yaml_block(tokens, idx, tokens[idx][0])
                result.append(nested)
            else:
                result.append(None)
            continue

        kv = (
            None
            if rest.startswith(("[", "{"))
            else _split_yaml_key_value(rest)
        )
        if kv is not None:
            key, val_rest = kv
            item_map: dict[str, Any] = {}
            map_indent = base_indent + 2
            if val_rest:
                item_map[key] = _parse_yaml_scalar(val_rest)
            elif idx < total and tokens[idx][0] > map_indent:
                item_map[key], idx = _parse_yaml_block(tokens, idx, tokens[idx][0])
            else:
                item_map[key] = None

            if idx < total and tokens[idx][0] == map_indent and not (
                tokens[idx][1] == "-" or tokens[idx][1].startswith("- ")
            ):
                extra_map, idx = _parse_yaml_mapping(tokens, idx, map_indent)
                item_map.update(extra_map)
            result.append(item_map)
        else:
            result.append(_parse_yaml_scalar(rest))

    return result, idx


def _parse_yaml_mapping(
    tokens: list[tuple[int, str, int]],
    idx: int,
    base_indent: int,
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    total = len(tokens)

    while idx < total:
        indent, content, line_no = tokens[idx]
        if indent < base_indent:
            break
        if indent > base_indent:
            raise ValueError(f"Unexpected indentation in YAML mapping at line {line_no}")
        if content == "-" or content.startswith("- "):
            break

        kv = _split_yaml_key_value(content)
        if kv is None:
            raise ValueError(f"Malformed YAML mapping entry at line {line_no}: {content!r}")
        key, val_rest = kv
        idx += 1

        if val_rest:
            result[key] = _parse_yaml_scalar(val_rest)
        elif idx < total and tokens[idx][0] > base_indent:
            result[key], idx = _parse_yaml_block(tokens, idx, tokens[idx][0])
        else:
            result[key] = None

    return result, idx


def _parse_yaml_block(
    tokens: list[tuple[int, str, int]],
    idx: int,
    base_indent: int,
) -> tuple[Any, int]:
    content = tokens[idx][1]
    if content == "-" or content.startswith("- "):
        return _parse_yaml_sequence(tokens, idx, base_indent)
    return _parse_yaml_mapping(tokens, idx, base_indent)


def _parse_yaml_document(yaml_text: str) -> Any:
    tokens = _tokenize_yaml_lines(yaml_text)
    if not tokens:
        raise ValueError("Empty YAML document")
    parsed, next_idx = _parse_yaml_block(tokens, 0, tokens[0][0])
    if next_idx != len(tokens):
        raise ValueError(
            f"Trailing unexpected YAML content at line {tokens[next_idx][2]}: {tokens[next_idx][1]!r}"
        )
    return parsed


def dump_pbt_spec_yaml(doc: PBTContractDocument) -> str:
    """Serialize a validated PBTContractDocument into deterministic pure-Python YAML (LF)."""
    validated = PBTContractDocument.model_validate(doc)
    payload = validated.model_dump(mode="json")
    return "\n".join(_emit_yaml_lines(payload, indent=0)) + "\n"


def load_pbt_spec_yaml(yaml_text: str) -> PBTContractDocument:
    """Parse pure-Python YAML and strictly validate both PBTSpec and PBTContractDocument."""
    raw = _parse_yaml_document(yaml_text)
    if not isinstance(raw, dict):
        raise ValueError(f"PBT_Spec.yaml root must be a mapping, got {type(raw).__name__}")
    if "spec" not in raw or not isinstance(raw["spec"], dict):
        raise ValueError("PBT_Spec.yaml must contain a 'spec' mapping")
    PBTSpec.model_validate(raw["spec"])
    return PBTContractDocument.model_validate(raw)


# ---------------------------------------------------------------------------
# Phase 3 PBT Contract Emitter
# ---------------------------------------------------------------------------

_DECL_KIND_RE = re.compile(
    r"^\s*(?:(?:abstract|one|lone|some|var)\s+)*"
    r"(sig|fact|pred|fun|assert)\s+([A-Za-z_][A-Za-z0-9_']*)"
)
_DESIGN_SECTION_RE = re.compile(r"^###\s+`([A-Za-z0-9_]+)`\s+\(`([^`]+)`\)")
_DESIGN_DEF_RE = re.compile(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_DESIGN_TABLE_ROW_RE = re.compile(r"^\|\s*`([A-Za-z0-9_]+)`\s*\|\s*([^|]+)\|")
_BACKTICK_TOKEN_RE = re.compile(r"`([^`]+)`")


class _LLMEnrichmentItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    req_id: str = Field(..., min_length=1)
    title: str = Field(default="")
    precondition: str = Field(default="")
    assertion_expr: str = Field(default="")


class _LLMEnrichmentBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enrichments: list[_LLMEnrichmentItem] = Field(default_factory=list)


def _sanitize_ident(raw: str, prefix: str = "item") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", raw.strip().lower()).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"{prefix}_{cleaned}" if cleaned else prefix
    if keyword.iskeyword(cleaned):
        cleaned = f"{prefix}_{cleaned}"
    return cleaned


def _pascal_ident(raw: str, fallback: str = "DomainModel") -> str:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", raw) if p]
    candidate = "".join(part.capitalize() for part in parts)
    if not candidate or not candidate[0].isalpha():
        candidate = f"{fallback}{candidate}"
    if keyword.iskeyword(candidate):
        candidate = f"{candidate}Model"
    return candidate


def _extract_adr_map(adrs: Sequence[Any]) -> dict[str, str]:
    """Map REQ-ID -> primary ADR-ID (preferring Routing ADRs) from EmittedADRFile or dicts."""
    req_to_adr: dict[str, str] = {}
    for adr in adrs:
        adr_id = ""
        req_ids: list[str] = []
        is_routing = True

        if isinstance(adr, Mapping):
            adr_id = str(adr.get("adr_id", "")).strip()
            kind = str(adr.get("kind", "routing")).strip().lower()
            is_routing = kind == "routing"
            req_ids = [str(r) for r in adr.get("req_ids", []) or []]
            fm = adr.get("frontmatter")
            if isinstance(fm, Mapping) and not req_ids:
                req_ids = [str(r) for r in fm.get("requirement_ids", []) or []]
        else:
            adr_id = str(getattr(adr, "adr_id", "")).strip()
            kind = str(getattr(adr, "kind", "routing")).strip().lower()
            is_routing = kind == "routing"
            routing_adr = getattr(adr, "routing_adr", None)
            if routing_adr is not None:
                req_ids = [str(r) for r in getattr(routing_adr, "req_ids", []) or []]
            else:
                req_ids = [str(r) for r in getattr(adr, "req_ids", []) or []]
                fm = getattr(adr, "frontmatter", None)
                if isinstance(fm, Mapping) and not req_ids:
                    req_ids = [str(r) for r in fm.get("requirement_ids", []) or []]

        if not adr_id:
            continue
        for rid in req_ids:
            if rid not in req_to_adr or is_routing:
                req_to_adr[rid] = adr_id
    return req_to_adr


def _extract_design_symbols(design_markdown: str) -> tuple[str | None, dict[str, tuple[str, str]]]:
    """Parse Design.md to map REQ-ID -> (module_name, target_symbol)."""
    if not design_markdown.strip():
        return None, {}

    first_module: str | None = None
    req_to_module: dict[str, str] = {}
    module_to_symbol: dict[str, str] = {}

    current_module: str | None = None
    for line in design_markdown.splitlines():
        sec_match = _DESIGN_SECTION_RE.match(line.strip())
        if sec_match:
            mod_name = _sanitize_ident(sec_match.group(1), prefix="mod")
            first_module = first_module or mod_name
            current_module = mod_name
            for req_tok in [r.strip() for r in sec_match.group(2).split(",") if r.strip()]:
                req_to_module[req_tok] = mod_name
            continue

        row_match = _DESIGN_TABLE_ROW_RE.match(line.strip())
        if row_match:
            mod_name = _sanitize_ident(row_match.group(1), prefix="mod")
            first_module = first_module or mod_name
            for req_tok in _BACKTICK_TOKEN_RE.findall(row_match.group(2)):
                req_to_module.setdefault(req_tok.strip(), mod_name)
            continue

        def_match = _DESIGN_DEF_RE.match(line)
        if def_match and current_module is not None and current_module not in module_to_symbol:
            module_to_symbol[current_module] = def_match.group(1)

    req_symbols: dict[str, tuple[str, str]] = {}
    for req_id, mod_name in req_to_module.items():
        symbol = module_to_symbol.get(mod_name, f"apply_{mod_name}_command")
        req_symbols[req_id] = (mod_name, symbol)
    return first_module, req_symbols


def _extract_alloy_constructs(spec_markdown: str) -> tuple[dict[str, list[str]], dict[str, list[tuple[str, str, str]]]]:
    """Extract Alloy traceability refs and `(decl_kind, symbol, module.als::symbol)` per REQ-ID."""
    if not spec_markdown.strip():
        return {}, {}
    blocks = extract_alloy_blocks(spec_markdown)
    req_to_refs = build_req_to_alloy_refs(blocks)
    req_constructs: dict[str, list[tuple[str, str, str]]] = {}

    for module_name in sorted(blocks, key=lambda name: (0 if name == "core_domain" else 1, name)):
        block = blocks[module_name]
        code_lines = block.code.splitlines()
        for line_no in sorted(block.line_to_req_ids):
            line_text = code_lines[line_no - 1] if 1 <= line_no <= len(code_lines) else ""
            decl_match = _DECL_KIND_RE.match(line_text)
            if not decl_match:
                continue
            decl_kind, symbol = decl_match.group(1), decl_match.group(2)
            alloy_ref = f"{module_name}.als::{symbol}"
            for req_id in block.line_to_req_ids[line_no]:
                bucket = req_constructs.setdefault(req_id, [])
                entry = (decl_kind, symbol, alloy_ref)
                if entry not in bucket:
                    bucket.append(entry)

    return req_to_refs, req_constructs


def _classify_property_kind(
    node: RequirementNode,
    alloy_constructs: Sequence[tuple[str, str, str]],
) -> PropertyKind:
    text = f"{node.title} {node.statement} {' '.join(node.acceptance_criteria)}".lower()
    if any(kw in text for kw in ("idempoten", "duplicate", "repeat", "retry", "twice")):
        return PropertyKind.IDEMPOTENCY
    if any(kw in text for kw in ("roundtrip", "round-trip", "serializ", "deserializ", "encode", "decode", "export")):
        return PropertyKind.ROUNDTRIP
    if any(kw in text for kw in ("state", "transition", "workflow", "lifecycle", "monotonic", "sequence")):
        return PropertyKind.STATEFUL_TRANSITION
    if any(decl_kind == "pred" for decl_kind, _, _ in alloy_constructs):
        return PropertyKind.STATEFUL_TRANSITION
    if (
        node.kind == RequirementKind.INTERFACE
        or any(kw in text for kw in ("return", "postcondition", "response", "output", "must produce"))
    ):
        return PropertyKind.POSTCONDITION
    return PropertyKind.INVARIANT


def _build_default_contract_spec(
    node: RequirementNode,
    kind: PropertyKind,
) -> tuple[list[HypothesisStrategySpec], str, str, GuardrailEnforcement, list[PydanticGuardrail]]:
    text = f"{node.title} {node.statement}".lower()
    model_name = f"{_pascal_ident(node.req_id, fallback='Req')}Input"
    guardrails: list[PydanticGuardrail] = []

    if kind == PropertyKind.IDEMPOTENCY:
        strategies = [
            HypothesisStrategySpec(
                parameter_name="value",
                strategy_expr="st.integers(min_value=0, max_value=10000)",
                description=f"Domain input for idempotency of {node.req_id}",
                constraints=["non_negative"],
            )
        ]
        precondition = "value >= 0"
        assertion_expr = "first == second"
        enforcement = GuardrailEnforcement.BOTH
        guardrails.append(
            PydanticGuardrail(
                guardrail_id=f"GR-{_sanitize_ident(node.req_id, prefix='req').upper()}-IDEMP",
                model_name=model_name,
                field_name="value",
                constraint_type="range",
                rule="ge=0, le=10000",
                error_message=f"{node.req_id} value must be within [0, 10000]",
            )
        )
    elif kind == PropertyKind.ROUNDTRIP:
        strategies = [
            HypothesisStrategySpec(
                parameter_name="payload",
                strategy_expr="st.text(min_size=1, max_size=64)",
                description=f"Payload text for round-trip fidelity of {node.req_id}",
                constraints=["non_empty"],
            )
        ]
        precondition = "len(payload) > 0"
        assertion_expr = "result == payload"
        enforcement = GuardrailEnforcement.BOTH
        guardrails.append(
            PydanticGuardrail(
                guardrail_id=f"GR-{_sanitize_ident(node.req_id, prefix='req').upper()}-RT",
                model_name=model_name,
                field_name="payload",
                constraint_type="type",
                rule="str",
                error_message=f"{node.req_id} payload must be a non-empty string",
            )
        )
    elif kind == PropertyKind.STATEFUL_TRANSITION:
        strategies = [
            HypothesisStrategySpec(
                parameter_name="actor_id",
                strategy_expr="st.text(alphabet='abcdefghijklmnopqrstuvwxyz0123456789_', min_size=1, max_size=16)",
                description=f"Authorized actor identifier for {node.req_id}",
                constraints=["identifier_chars"],
            ),
            HypothesisStrategySpec(
                parameter_name="delta",
                strategy_expr="st.integers(min_value=0, max_value=100)",
                description=f"Non-negative transition step delta for {node.req_id}",
                constraints=["non_negative"],
            ),
        ]
        precondition = "len(actor_id) > 0 and delta >= 0"
        assertion_expr = "state['version'] >= 0"
        enforcement = GuardrailEnforcement.PBT_ONLY
        guardrails.append(
            PydanticGuardrail(
                guardrail_id=f"GR-{_sanitize_ident(node.req_id, prefix='req').upper()}-ACTOR",
                model_name=model_name,
                field_name="actor_id",
                constraint_type="regex",
                rule="^[a-z0-9_]{1,16}$",
                error_message=f"{node.req_id} actor_id must match ^[a-z0-9_]{{1,16}}$",
            )
        )
    elif any(kw in text for kw in ("amount", "balance", "budget", "limit", "count", "latency", "ms", "score", "price")):
        strategies = [
            HypothesisStrategySpec(
                parameter_name="amount",
                strategy_expr="st.integers(min_value=0, max_value=100000)",
                description=f"Non-negative numeric quantity for {node.req_id}",
                constraints=["ge=0"],
            )
        ]
        precondition = "amount >= 0"
        assertion_expr = "result >= 0"
        enforcement = GuardrailEnforcement.BOTH
        guardrails.append(
            PydanticGuardrail(
                guardrail_id=f"GR-{_sanitize_ident(node.req_id, prefix='req').upper()}-AMT",
                model_name=model_name,
                field_name="amount",
                constraint_type="range",
                rule="ge=0, le=100000",
                error_message=f"{node.req_id} amount must be in [0, 100000]",
            )
        )
    else:
        strategies = [
            HypothesisStrategySpec(
                parameter_name="entity_id",
                strategy_expr="st.text(alphabet='abcdefghijklmnopqrstuvwxyz0123456789_-', min_size=1, max_size=32)",
                description=f"Domain entity identifier for {node.req_id}",
                constraints=["non_empty"],
            )
        ]
        precondition = "len(entity_id) > 0"
        assertion_expr = "result is not None"
        enforcement = GuardrailEnforcement.BOTH
        guardrails.append(
            PydanticGuardrail(
                guardrail_id=f"GR-{_sanitize_ident(node.req_id, prefix='req').upper()}-ID",
                model_name=model_name,
                field_name="entity_id",
                constraint_type="regex",
                rule="^[a-z0-9_-]{1,32}$",
                error_message=f"{node.req_id} entity_id must be a valid identifier token",
            )
        )

    return strategies, precondition, assertion_expr, enforcement, guardrails


def _is_valid_python_expr(expr: str) -> bool:
    cleaned = expr.strip()
    if not cleaned:
        return False
    try:
        ast.parse(cleaned, mode="eval")
    except SyntaxError:
        return False
    return True


def _resolve_doc_sha256(
    active_nodes: Sequence[RequirementNode],
    spec_markdown: str,
    session_id: str,
    graph_id: str,
) -> str:
    digests = sorted(
        {
            link.source_sha256
            for node in active_nodes
            for link in node.trace_links
        }
    )
    if len(digests) == 1:
        return digests[0]
    if len(digests) > 1:
        return hashlib.sha256("|".join(digests).encode("utf-8")).hexdigest()
    seed_material = spec_markdown if spec_markdown.strip() else f"{session_id}:{graph_id}"
    return hashlib.sha256(seed_material.encode("utf-8")).hexdigest()


class PBTSpecEmitter:
    """Translate verified Alloy Spec.md, ADRs, and Design.md into a strict PBTContractDocument."""

    def __init__(self, router: ProviderRouter | None = None) -> None:
        self.router = router

    def _optional_router_enrichments(
        self,
        active_nodes: Sequence[RequirementNode],
        session_id: str,
    ) -> dict[str, _LLMEnrichmentItem]:
        if self.router is None:
            return {}
        summary = "\n".join(
            f"- {node.req_id}: {node.title} ({node.kind.value})"
            for node in active_nodes[:12]
        )
        prompt = (
            f"Session {session_id}: suggest concise PBT property titles or Python boolean assertions "
            f"for these requirements:\n{summary}"
        )[:3800]
        request = LLMRequest(
            prompt=prompt,
            system_prompt="Return strict JSON matching the schema with valid Python boolean expressions.",
            max_tokens=512,
            temperature=0.0,
            response_schema=_LLMEnrichmentBatch,
        )
        try:
            response = self.router.route(request)
        except Exception:
            return {}
        if not response.parsed:
            return {}
        try:
            batch = _LLMEnrichmentBatch.model_validate(response.parsed)
        except Exception:
            return {}
        return {item.req_id: item for item in batch.enrichments}

    def emit_contract(
        self,
        graph: RequirementGraph,
        *,
        session_id: str,
        spec_markdown: str,
        adrs: Sequence[Any] = (),
        design_markdown: str = "",
        seed: int = 42,
    ) -> PBTContractDocument:
        """Emit a deterministic, fully traceable PBTContractDocument."""
        active_nodes = [
            node
            for node in graph.nodes
            if node.status not in (RequirementStatus.DEPRECATED, RequirementStatus.SUPERSEDED)
        ]
        if not active_nodes:
            raise ValueError(f"RequirementGraph {graph.graph_id!r} has no active requirements")

        req_to_alloy_refs, req_constructs = _extract_alloy_constructs(spec_markdown)
        req_to_adr = _extract_adr_map(adrs)
        first_design_mod, req_design_symbols = _extract_design_symbols(design_markdown)

        target_module = first_design_mod or _sanitize_ident(graph.graph_id, prefix="domain")
        doc_sha256 = _resolve_doc_sha256(active_nodes, spec_markdown, session_id, graph.graph_id)
        llm_enrichments = self._optional_router_enrichments(active_nodes, session_id)

        properties: list[PBTPropertyContract] = []
        invariants: list[PropertyInvariant] = []
        guardrails: list[PydanticGuardrail] = []

        for idx, node in enumerate(active_nodes, start=1):
            constructs = req_constructs.get(node.req_id, [])
            alloy_refs = req_to_alloy_refs.get(node.req_id, [])
            primary_alloy_ref = (
                constructs[0][2]
                if constructs
                else (alloy_refs[0] if alloy_refs else f"spec.md::{node.req_id}")
            )
            adr_id = req_to_adr.get(node.req_id, f"ADR-R{idx:03d}")
            node_sha256 = (
                node.trace_links[0].source_sha256 if node.trace_links else doc_sha256
            )

            _, design_symbol = req_design_symbols.get(
                node.req_id,
                (target_module, f"enforce_{_sanitize_ident(node.req_id, prefix='req')}"),
            )

            kind = _classify_property_kind(node, constructs)
            strategies, precondition, assertion_expr, enforcement, node_guardrails = (
                _build_default_contract_spec(node, kind)
            )
            title = f"{node.title} ({kind.value})"

            enrichment = llm_enrichments.get(node.req_id)
            if enrichment is not None:
                if enrichment.title.strip():
                    title = enrichment.title.strip()
                if enrichment.precondition.strip() and _is_valid_python_expr(enrichment.precondition):
                    precondition = enrichment.precondition.strip()
                if enrichment.assertion_expr.strip() and _is_valid_python_expr(enrichment.assertion_expr):
                    assertion_expr = enrichment.assertion_expr.strip()

            prop_id = f"PBT-{idx:03d}-{_sanitize_ident(node.req_id, prefix='req').upper()}"
            prop = PBTPropertyContract(
                property_id=prop_id,
                req_id=node.req_id,
                adr_id=adr_id,
                source_sha256=node_sha256,
                alloy_ref=primary_alloy_ref,
                target_symbol=design_symbol,
                kind=kind,
                title=title,
                precondition=precondition,
                assertion_expr=assertion_expr,
                input_strategies=strategies,
                guardrail_enforcement=enforcement,
                max_examples=200,
            )
            properties.append(prop)
            invariants.append(
                PropertyInvariant(
                    invariant_id=prop_id,
                    req_id=node.req_id,
                    title=title,
                    precondition=precondition,
                    assertion_expr=assertion_expr,
                    strategies=strategies,
                    max_examples=prop.max_examples,
                )
            )
            guardrails.extend(node_guardrails)

        alloy_model_ref = (
            properties[0].alloy_ref.split("::", 1)[0]
            if properties and "::" in properties[0].alloy_ref
            else None
        )
        pbt_spec = PBTSpec(
            pbt_spec_id=f"pbt-{_sanitize_ident(session_id, prefix='session')}",
            target_module=target_module,
            req_ids=[node.req_id for node in active_nodes],
            invariants=invariants,
            guardrails=guardrails,
            alloy_model_ref=alloy_model_ref,
            seed=seed,
        )
        return PBTContractDocument(
            spec=pbt_spec,
            session_id=session_id,
            source_sha256=doc_sha256,
            properties=properties,
            seed=seed,
        )


__all__ = [
    "PBTSpecEmitter",
    "dump_pbt_spec_yaml",
    "load_pbt_spec_yaml",
]
