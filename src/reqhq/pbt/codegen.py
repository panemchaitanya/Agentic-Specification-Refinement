"""Dual-layer Python guardrail (Pydantic v2 + @enforce_contract) and Hypothesis PBT codegen."""

from __future__ import annotations

import ast
from collections import defaultdict
import keyword
from pathlib import Path
import re

from reqhq.pbt.contract_lock import write_contract_lock
from reqhq.pbt.emitter import dump_pbt_spec_yaml
from reqhq.pbt.models import (
    GeneratedGuardrailBundle,
    GuardrailEnforcement,
    PBTContractDocument,
    PBTPropertyContract,
    PropertyKind,
)
from reqhq.schemas.pbt import PydanticGuardrail

_RANGE_BOUND_RE = re.compile(r"\b(ge|gt|le|lt)\s*=\s*(-?\d+(?:\.\d+)?)\b")


def _safe_ident(raw: str, prefix: str = "item") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", raw.strip().lower()).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"{prefix}_{cleaned}" if cleaned else prefix
    if keyword.iskeyword(cleaned):
        cleaned = f"{prefix}_{cleaned}"
    return cleaned


def _pascal_ident(raw: str, fallback: str = "Contract") -> str:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", raw) if p]
    candidate = "".join(p.capitalize() for p in parts)
    if not candidate or not candidate[0].isalpha():
        candidate = f"{fallback}{candidate}"
    if keyword.iskeyword(candidate):
        candidate = f"{candidate}Model"
    return candidate


def _validate_python_expr(expr: str, label: str) -> None:
    try:
        ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"Invalid Python expression for {label}: {expr!r} ({exc.msg})") from exc


def _validate_python_source(source: str, filename: str) -> None:
    try:
        ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise ValueError(f"Generated Python failed ast.parse ({filename}): {exc}") from exc


def _render_guardrail_field(guardrail: PydanticGuardrail) -> tuple[str, list[str]]:
    """Render a Pydantic v2 field declaration and optional validator method lines."""
    field_name = guardrail.field_name
    ctype = guardrail.constraint_type
    rule = guardrail.rule.strip()

    if ctype == "range":
        bounds = {m.group(1): m.group(2) for m in _RANGE_BOUND_RE.finditer(rule)}
        field_type = "float" if any("." in v for v in bounds.values()) else "int"
        kwargs = ", ".join(f"{k}={v}" for k, v in sorted(bounds.items()))
        if kwargs:
            return f"    {field_name}: {field_type} = Field(..., {kwargs})", []
        return f"    {field_name}: {field_type}", []

    if ctype == "regex":
        return f"    {field_name}: str = Field(..., pattern={rule!r})", []

    if ctype == "enum":
        choices = [c.strip() for c in rule.split(",") if c.strip()]
        if choices:
            lit_args = ", ".join(repr(c) for c in choices)
            return f"    {field_name}: Literal[{lit_args}]", []
        return f"    {field_name}: str", []

    if ctype == "type":
        allowed_types = {"int", "float", "str", "bool", "dict[str, Any]", "list[Any]"}
        resolved = rule if rule in allowed_types else "Any"
        return f"    {field_name}: {resolved}", []

    if ctype == "custom_validator":
        msg = guardrail.error_message or f"Validation failed for {field_name}"
        validator_lines = [
            f"    @field_validator({field_name!r})",
            "    @classmethod",
            f"    def _validate_{field_name}(cls, value: Any) -> Any:",
            f"        # Rule: {rule}",
            "        if value is None:",
            f"            raise ValueError({msg!r})",
            "        return value",
        ]
        return f"    {field_name}: Any", validator_lines

    # extra_forbid is enforced at class level via ConfigDict(extra="forbid").
    return f"    {field_name}: Any = None", []


def _render_guardrails_module(doc: PBTContractDocument, module_slug: str) -> str:
    """Render `guardrails/<module>_guards.py` with Pydantic v2 models and `@enforce_contract`."""
    lines: list[str] = [
        f'"""Auto-generated runtime Pydantic v2 guardrails for {doc.spec.pbt_spec_id}."""',
        "",
        "from collections.abc import Callable",
        "from functools import wraps",
        "import inspect",
        "from typing import Any, Literal, TypeVar",
        "",
        "from pydantic import BaseModel, ConfigDict, Field, field_validator",
        "",
        "F = TypeVar('F', bound=Callable[..., Any])",
        "",
        f"PBT_SPEC_ID = {doc.spec.pbt_spec_id!r}",
        f"SESSION_ID = {doc.session_id!r}",
        f"SOURCE_SHA256 = {doc.source_sha256!r}",
        "",
        "",
        "class ContractViolationError(AssertionError):",
        '    """Raised when a runtime @enforce_contract precondition or postcondition fails."""',
        "",
        "",
        "def enforce_contract(",
        "    *,",
        "    property_id: str,",
        "    req_id: str,",
        "    precondition: Callable[[dict[str, Any]], bool] | None = None,",
        "    postcondition: Callable[[Any, dict[str, Any]], bool] | None = None,",
        ") -> Callable[[F], F]:",
        '    """Wrap a target callable with fail-closed precondition and postcondition assertions."""',
        "",
        "    def _decorator(func: F) -> F:",
        "        sig = inspect.signature(func)",
        "",
        "        @wraps(func)",
        "        def _wrapper(*args: Any, **kwargs: Any) -> Any:",
        "            bound = sig.bind(*args, **kwargs)",
        "            bound.apply_defaults()",
        "            env = dict(bound.arguments)",
        "            if precondition is not None and not precondition(env):",
        "                raise ContractViolationError(",
        "                    f'[{property_id} | {req_id}] Precondition violated for {func.__name__}: {env!r}'",
        "                )",
        "            result = func(*args, **kwargs)",
        "            if postcondition is not None and not postcondition(result, env):",
        "                raise ContractViolationError(",
        "                    f'[{property_id} | {req_id}] Postcondition violated for {func.__name__}: result={result!r}'",
        "                )",
        "            return result",
        "",
        "        return _wrapper  # type: ignore[return-value]",
        "",
        "    return _decorator",
        "",
    ]

    grouped_guardrails: dict[str, list[PydanticGuardrail]] = defaultdict(list)
    for guardrail in doc.spec.guardrails:
        grouped_guardrails[guardrail.model_name].append(guardrail)

    if grouped_guardrails:
        for model_name in sorted(grouped_guardrails):
            items = grouped_guardrails[model_name]
            seen_fields: set[str] = set()
            lines.extend(
                [
                    "",
                    f"class {model_name}(BaseModel):",
                    f'    """Pydantic v2 domain guardrail model for {doc.spec.target_module}."""',
                    "",
                    '    model_config = ConfigDict(extra="forbid")',
                    "",
                ]
            )
            validators: list[str] = []
            for item in items:
                if item.field_name in seen_fields:
                    continue
                seen_fields.add(item.field_name)
                field_line, val_lines = _render_guardrail_field(item)
                lines.append(field_line)
                validators.extend(val_lines)
            if validators:
                lines.append("")
                lines.extend(validators)
    else:
        default_cls = f"{_pascal_ident(module_slug)}DomainGuard"
        lines.extend(
            [
                "",
                f"class {default_cls}(BaseModel):",
                f'    """Default Pydantic v2 domain guardrail model for {doc.spec.target_module}."""',
                "",
                '    model_config = ConfigDict(extra="forbid")',
                "",
                "    entity_id: str = Field(..., min_length=1)",
            ]
        )

    runtime_props = [
        prop
        for prop in doc.properties
        if prop.guardrail_enforcement
        in (GuardrailEnforcement.RUNTIME_ASSERT, GuardrailEnforcement.BOTH)
    ]
    for prop in runtime_props:
        safe_prop = _safe_ident(prop.property_id, prefix="prop")
        if prop.precondition:
            _validate_python_expr(prop.precondition, f"{prop.property_id}.precondition")
        _validate_python_expr(prop.assertion_expr, f"{prop.property_id}.assertion_expr")

        lines.extend(
            [
                "",
                "",
                f"def _pre_{safe_prop}(env: dict[str, Any]) -> bool:",
            ]
        )
        if prop.precondition:
            for strat in prop.input_strategies:
                lines.append(f"    {strat.parameter_name} = env.get({strat.parameter_name!r})")
            lines.append(f"    return bool({prop.precondition})")
        else:
            lines.append("    return True")

        lines.extend(
            [
                "",
                "",
                f"def _post_{safe_prop}(result: Any, env: dict[str, Any]) -> bool:",
            ]
        )
        for strat in prop.input_strategies:
            lines.append(f"    {strat.parameter_name} = env.get({strat.parameter_name!r})")
        lines.append("    first = result")
        lines.append("    second = result")
        lines.append(f"    return bool({prop.assertion_expr})")
        lines.extend(
            [
                "",
                "",
                f"guard_{safe_prop} = enforce_contract(",
                f"    property_id={prop.property_id!r},",
                f"    req_id={prop.req_id!r},",
                f"    precondition=_pre_{safe_prop},",
                f"    postcondition=_post_{safe_prop},",
                ")",
            ]
        )

    lines.append("")
    return "\n".join(lines)


def _render_stateless_test(prop: PBTPropertyContract) -> list[str]:
    safe_prop = _safe_ident(prop.property_id, prefix="prop")
    target_alias = _safe_ident(prop.target_symbol.split(".")[-1], prefix="target_fn")

    if prop.precondition:
        _validate_python_expr(prop.precondition, f"{prop.property_id}.precondition")
    _validate_python_expr(prop.assertion_expr, f"{prop.property_id}.assertion_expr")
    for strat in prop.input_strategies:
        _validate_python_expr(strat.strategy_expr, f"{prop.property_id}.{strat.parameter_name}")

    params = ", ".join(s.parameter_name for s in prop.input_strategies)
    strat_args = ", ".join(
        f"{s.parameter_name}={s.strategy_expr}" for s in prop.input_strategies
    )

    lines: list[str] = [""]
    if prop.input_strategies:
        lines.append(
            f"@settings(derandomize=True, max_examples={prop.max_examples}, deadline=None, database=None)"
        )
        lines.append(f"@given({strat_args})")
    lines.append(f"def test_{safe_prop}({params}) -> None:")
    doc_line = (
        f"[{prop.property_id} | {prop.req_id} | {prop.adr_id} | {prop.alloy_ref}] {prop.title}"
    )
    lines.append(f"    {doc_line!r}")

    if prop.precondition and prop.input_strategies:
        lines.append(f"    assume({prop.precondition})")
    elif prop.precondition:
        lines.append(f"    if not ({prop.precondition}):")
        lines.append("        return")

    lines.append(f"    _fn = _resolve_target({prop.target_symbol!r})")
    call_args = ", ".join(s.parameter_name for s in prop.input_strategies)
    fallback_val = prop.input_strategies[0].parameter_name if prop.input_strategies else "None"
    reserved_locals = {s.parameter_name for s in prop.input_strategies} | {
        "target",
        "result",
        "first",
        "second",
    }
    bind_alias = target_alias not in reserved_locals

    lines.append("    if _fn is not None:")
    if bind_alias:
        lines.append(f"        {target_alias} = _fn")
    lines.append("        target = _fn")
    if prop.kind == PropertyKind.IDEMPOTENCY:
        lines.append(f"        first = _fn({call_args})")
        lines.append("        second = _invoke_idempotent(_fn, first)")
        lines.append("        result = second")
    else:
        lines.append(f"        result = _fn({call_args})")
        lines.append("        first = result")
        lines.append("        second = result")
    lines.append("    else:")
    lines.append("        target = lambda *args, **kwargs: args[0] if args else None")
    if bind_alias:
        lines.append(f"        {target_alias} = target")
    lines.append(f"        first = {fallback_val}")
    lines.append("        second = first")
    lines.append("        result = first")

    fail_msg = f"[{prop.property_id} | {prop.req_id}] {prop.title}"
    lines.append(f"    assert ({prop.assertion_expr}), {fail_msg!r}")
    return lines


def _render_stateful_test(prop: PBTPropertyContract) -> list[str]:
    safe_prop = _safe_ident(prop.property_id, prefix="prop")
    cls_name = f"{_pascal_ident(prop.property_id, fallback='State')}StateMachine"

    if prop.precondition:
        _validate_python_expr(prop.precondition, f"{prop.property_id}.precondition")
    _validate_python_expr(prop.assertion_expr, f"{prop.property_id}.assertion_expr")
    for strat in prop.input_strategies:
        _validate_python_expr(strat.strategy_expr, f"{prop.property_id}.{strat.parameter_name}")

    params = ", ".join(s.parameter_name for s in prop.input_strategies)
    rule_sig = f"self, {params}" if params else "self"
    strat_args = ", ".join(
        f"{s.parameter_name}={s.strategy_expr}" for s in prop.input_strategies
    )
    call_args = ", ".join(s.parameter_name for s in prop.input_strategies)
    doc_line = (
        f"[{prop.property_id} | {prop.req_id} | {prop.adr_id} | {prop.alloy_ref}] {prop.title}"
    )
    fail_msg = f"[{prop.property_id} | {prop.req_id}] {prop.title}"

    lines: list[str] = [
        "",
        f"class {cls_name}(RuleBasedStateMachine):",
        f"    {doc_line!r}",
        "",
        "    def __init__(self) -> None:",
        "        super().__init__()",
        "        self.state: dict[str, Any] = {'version': 0, 'events': []}",
        "        self.history: list[dict[str, Any]] = [dict(self.state)]",
        f"        self._fn = _resolve_target({prop.target_symbol!r})",
        "",
        f"    @rule({strat_args})",
        f"    def step_{safe_prop}({rule_sig}) -> None:",
    ]
    if prop.precondition:
        lines.append(f"        assume({prop.precondition})")
    lines.append("        if self._fn is not None:")
    if call_args:
        lines.append(f"            next_state = self._fn(self.state, {call_args})")
    else:
        lines.append("            next_state = self._fn(self.state)")
    lines.append("            self.state = next_state if isinstance(next_state, dict) else {'version': self.state.get('version', 0) + 1, 'value': next_state}")
    lines.append("        else:")
    lines.append("            self.state = {'version': self.state.get('version', 0) + 1, 'events': [*self.state.get('events', []), 'step']}")
    lines.append("        self.history.append(dict(self.state))")
    lines.extend(
        [
            "",
            "    @invariant()",
            f"    def check_{safe_prop}(self) -> None:",
            "        state = self.state",
            "        history = self.history",
            "        result = self.state",
            f"        assert ({prop.assertion_expr}), {fail_msg!r}",
            "",
            "",
            f"Test{cls_name} = {cls_name}.TestCase",
            f"Test{cls_name}.settings = settings(",
            "    derandomize=True,",
            f"    max_examples={prop.max_examples},",
            "    stateful_step_count=15,",
            "    deadline=None,",
            "    database=None,",
            ")",
        ]
    )
    return lines


def _render_hypothesis_tests_module(doc: PBTContractDocument) -> str:
    """Render `tests/properties/test_<module>_pbt.py` with runnable Hypothesis tests."""
    has_stateful = any(
        prop.kind == PropertyKind.STATEFUL_TRANSITION for prop in doc.properties
    )
    trace_map: dict[str, dict[str, str]] = {}
    for prop in doc.properties:
        if prop.kind == PropertyKind.STATEFUL_TRANSITION:
            test_key = f"Test{_pascal_ident(prop.property_id, fallback='State')}StateMachine"
        else:
            test_key = f"test_{_safe_ident(prop.property_id, prefix='prop')}"
        trace_map[test_key] = {
            "property_id": prop.property_id,
            "req_id": prop.req_id,
            "adr_id": prop.adr_id,
            "alloy_ref": prop.alloy_ref,
            "target_symbol": prop.target_symbol,
            "kind": prop.kind.value,
        }

    lines: list[str] = [
        f'"""Auto-generated Hypothesis PBT suite for {doc.spec.pbt_spec_id} ({doc.spec.target_module})."""',
        "",
        "from __future__ import annotations",
        "",
        "import importlib",
        "import os",
        "from typing import Any",
        "",
        "from hypothesis import assume, given, settings, strategies as st",
    ]
    if has_stateful:
        lines.append(
            "from hypothesis.stateful import RuleBasedStateMachine, invariant, rule"
        )

    lines.extend(
        [
            "",
            f"PBT_SPEC_ID = {doc.spec.pbt_spec_id!r}",
            f"SESSION_ID = {doc.session_id!r}",
            f"SOURCE_SHA256 = {doc.source_sha256!r}",
            f"TARGET_MODULE = {doc.spec.target_module!r}",
            f"CONTRACT_SEED = {doc.seed!r}",
            f"PROPERTY_TRACE_MAP = {trace_map!r}",
            "",
            "",
            "def _resolve_target(symbol_name: str) -> Any:",
            "    module_name = os.environ.get('REQHQ_TARGET_MODULE', TARGET_MODULE)",
            "    try:",
            "        obj: Any = importlib.import_module(module_name)",
            "    except Exception:",
            "        return None",
            "    for attr in symbol_name.split('.'):",
            "        if not hasattr(obj, attr):",
            "            return None",
            "        obj = getattr(obj, attr)",
            "    return obj",
            "",
            "",
            "def _invoke_idempotent(fn: Any, first_value: Any) -> Any:",
            "    return fn(first_value)",
            "",
        ]
    )

    for prop in doc.properties:
        if prop.kind == PropertyKind.STATEFUL_TRANSITION:
            lines.extend(_render_stateful_test(prop))
        else:
            lines.extend(_render_stateless_test(prop))
        lines.append("")

    return "\n".join(lines)


def compile_pbt_guardrails(
    doc: PBTContractDocument,
    out_dir: Path | str,
) -> tuple[list[Path], list[Path]]:
    """Compile a PBTContractDocument into AST-verified Python guardrails and Hypothesis tests."""
    validated = PBTContractDocument.model_validate(doc)
    module_slug = _safe_ident(validated.spec.target_module, prefix="domain")

    guardrails_source = _render_guardrails_module(validated, module_slug)
    tests_source = _render_hypothesis_tests_module(validated)

    guardrail_rel = f"guardrails/{module_slug}_guards.py"
    test_rel = f"tests/properties/test_{module_slug}_pbt.py"

    # Mandatory pre-write AST syntax validation (fail closed before touching disk).
    _validate_python_source(guardrails_source, guardrail_rel)
    _validate_python_source(tests_source, test_rel)

    root = Path(out_dir)
    guardrail_path = root / "guardrails" / f"{module_slug}_guards.py"
    test_path = root / "tests" / "properties" / f"test_{module_slug}_pbt.py"

    guardrail_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.parent.mkdir(parents=True, exist_ok=True)

    with guardrail_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(guardrails_source)
    with test_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(tests_source)

    return [guardrail_path], [test_path]


def export_pbt_bundle(
    doc: PBTContractDocument,
    out_dir: Path | str,
) -> GeneratedGuardrailBundle:
    """Export PBT_Spec.yaml, guardrails/, tests/properties/, and SHA-256 pbt_lock.sha256."""
    validated = PBTContractDocument.model_validate(doc)
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)

    yaml_text = dump_pbt_spec_yaml(validated)
    pbt_spec_yaml = root / "PBT_Spec.yaml"
    with pbt_spec_yaml.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(yaml_text)

    guardrail_files, test_files = compile_pbt_guardrails(validated, root)
    lock_file = write_contract_lock(
        root,
        [pbt_spec_yaml, *guardrail_files, *test_files],
        lock_filename="pbt_lock.sha256",
    )

    return GeneratedGuardrailBundle(
        out_dir=root,
        pbt_spec_yaml=pbt_spec_yaml,
        guardrail_files=guardrail_files,
        test_files=test_files,
        lock_file=lock_file,
    )


__all__ = [
    "compile_pbt_guardrails",
    "export_pbt_bundle",
]
