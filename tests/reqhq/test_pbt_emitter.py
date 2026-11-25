"""Unit, Hypothesis PBT, and real execution tests for src/reqhq/pbt."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from hypothesis import given, settings, strategies as st
from pydantic import ValidationError
import pytest

from reqhq.alloy.emitter import AlloySpecEmitter
from reqhq.llm.providers.base import LLMRequest, LLMResponse, ProviderError
from reqhq.llm.router import ProviderRouter
from reqhq.pbt import (
    ContractTamperError,
    CounterexampleClassification,
    CounterexampleRepairReport,
    GeneratedGuardrailBundle,
    GuardrailEnforcement,
    PBTContractDocument,
    PBTPropertyContract,
    PBTSpecEmitter,
    PropertyKind,
    compile_pbt_guardrails,
    dump_pbt_spec_yaml,
    export_pbt_bundle,
    load_pbt_spec_yaml,
    parse_hypothesis_counterexamples,
    verify_contract_lock,
    write_contract_lock,
)
from reqhq.pbt.emitter import (
    _format_yaml_scalar,
    _parse_yaml_document,
    _parse_yaml_scalar,
    _split_yaml_key_value,
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
    TraceLink,
)
from reqhq.schemas.routing_adr import RoutingADR, RoutingDecisionKind
from reqhq.synthesis.design_emitter import emit_design_md
from reqhq.synthesis.models import EmittedADRFile, TriageItem, TriagePlan, TriageTrack

SHA_A = hashlib.sha256(b"source-doc-alpha").hexdigest()
SHA_B = hashlib.sha256(b"source-doc-beta").hexdigest()


class _FakeProvider:
    name = "fake-local"
    is_local = True
    default_model = "qwen-local"

    def __init__(self, response_text: str | None = None, *, should_raise: bool = False) -> None:
        self._text = response_text or "{}"
        self._should_raise = should_raise

    def generate(self, request: LLMRequest) -> LLMResponse:
        if self._should_raise:
            raise ProviderError("local backend unavailable", provider=self.name)
        return LLMResponse(text=self._text, provider=self.name, model=self.default_model)


def _sample_graph() -> RequirementGraph:
    link = TraceLink(source_sha256=SHA_A, source_uri="prd://orders.md", anchor="L1-L10")
    return RequirementGraph(
        graph_id="orders-core",
        version="1.0.0",
        nodes=[
            RequirementNode(
                req_id="REQ-001",
                title="Non-negative settlement amount",
                statement="Settlement amount must always remain non-negative.",
                kind=RequirementKind.FUNCTIONAL,
                status=RequirementStatus.VERIFIED,
                priority="P0",
                trace_links=[link],
            ),
            RequirementNode(
                req_id="REQ-002",
                title="Idempotent payment normalization",
                statement="Retrying duplicate payment normalization must be idempotent.",
                kind=RequirementKind.FUNCTIONAL,
                status=RequirementStatus.ACCEPTED,
                priority="P0",
                trace_links=[link],
            ),
            RequirementNode(
                req_id="REQ-003",
                title="Order payload round-trip serialization",
                statement="Encode and decode payload must preserve exact roundtrip text.",
                kind=RequirementKind.DATA,
                status=RequirementStatus.ACCEPTED,
                priority="P1",
                trace_links=[link],
            ),
            RequirementNode(
                req_id="REQ-004",
                title="Order lifecycle state transition",
                statement="Workflow state transition version must stay monotonically non-negative.",
                kind=RequirementKind.FUNCTIONAL,
                status=RequirementStatus.VERIFIED,
                priority="P0",
                trace_links=[link],
            ),
            RequirementNode(
                req_id="REQ-005",
                title="API receipt postcondition",
                statement="Interface must produce a non-null receipt response.",
                kind=RequirementKind.INTERFACE,
                status=RequirementStatus.ACCEPTED,
                priority="P1",
                trace_links=[link],
            ),
            RequirementNode(
                req_id="REQ-006",
                title="Deprecated legacy requirement",
                statement="Superseded rule ignored by emitter.",
                kind=RequirementKind.FUNCTIONAL,
                status=RequirementStatus.DEPRECATED,
                priority="P3",
                trace_links=[link],
            ),
        ],
    )


def _sample_adrs() -> list[EmittedADRFile]:
    routing = RoutingADR(
        adr_id="ADR-R001",
        title="Route settlement invariants to software core",
        req_ids=["REQ-001", "REQ-002"],
        decision=RoutingDecisionKind.SOFTWARE,
        rationale="Deterministic pure domain checks belong in software.",
    )
    return [
        EmittedADRFile(
            adr_id="ADR-R001",
            kind="routing",
            filename="ADR-R001.md",
            frontmatter={"requirement_ids": ["REQ-001", "REQ-002"]},
            markdown="# ADR-R001\n",
            routing_adr=routing,
        ),
        EmittedADRFile(
            adr_id="ADR-R003",
            kind="routing",
            filename="ADR-R003.md",
            frontmatter={"requirement_ids": ["REQ-003", "REQ-004"]},
            markdown="# ADR-R003\n",
        ),
    ]


def _sample_design(graph: RequirementGraph, adrs: list[EmittedADRFile]) -> str:
    plan = TriagePlan(
        session_id="sess-orders",
        items=[
            TriageItem(
                req_id=rid,
                track=TriageTrack.SOFTWARE,
                rationale="Pure functional core module.",
                software_module="orders_core",
            )
            for rid in ["REQ-001", "REQ-002", "REQ-003", "REQ-004", "REQ-005"]
        ],
    )
    return emit_design_md(graph, plan, adrs)


def _build_executable_contract() -> PBTContractDocument:
    """Build a contract covering invariant, idempotency, roundtrip, and stateful transition."""
    props = [
        PBTPropertyContract(
            property_id="PBT-001-AMOUNT",
            req_id="REQ-001",
            adr_id="ADR-R001",
            source_sha256=SHA_A,
            alloy_ref="core_domain.als::Inv_REQ_001",
            target_symbol="clamp_amount",
            kind=PropertyKind.INVARIANT,
            title="Clamped amount is always non-negative",
            precondition="amount >= -500",
            assertion_expr="result >= 0",
            input_strategies=[
                HypothesisStrategySpec(
                    parameter_name="amount",
                    strategy_expr="st.integers(min_value=-500, max_value=500)",
                )
            ],
            guardrail_enforcement=GuardrailEnforcement.BOTH,
            max_examples=200,
        ),
        PBTPropertyContract(
            property_id="PBT-002-IDEMP",
            req_id="REQ-002",
            adr_id="ADR-R001",
            source_sha256=SHA_A,
            alloy_ref="core_domain.als::Inv_REQ_002",
            target_symbol="normalize_code",
            kind=PropertyKind.IDEMPOTENCY,
            title="Code normalization is idempotent",
            precondition="value >= 0",
            assertion_expr="first == second",
            input_strategies=[
                HypothesisStrategySpec(
                    parameter_name="value",
                    strategy_expr="st.integers(min_value=0, max_value=200)",
                )
            ],
            guardrail_enforcement=GuardrailEnforcement.RUNTIME_ASSERT,
            max_examples=200,
        ),
        PBTPropertyContract(
            property_id="PBT-003-ROUNDTRIP",
            req_id="REQ-003",
            adr_id="ADR-R003",
            source_sha256=SHA_A,
            alloy_ref="core_domain.als::Inv_REQ_003",
            target_symbol="roundtrip_text",
            kind=PropertyKind.ROUNDTRIP,
            title="Payload survives encode/decode roundtrip",
            precondition="len(payload) > 0",
            assertion_expr="result == payload",
            input_strategies=[
                HypothesisStrategySpec(
                    parameter_name="payload",
                    strategy_expr="st.text(alphabet='abcdef012345', min_size=1, max_size=12)",
                )
            ],
            guardrail_enforcement=GuardrailEnforcement.BOTH,
            max_examples=200,
        ),
        PBTPropertyContract(
            property_id="PBT-004-STATEFUL",
            req_id="REQ-004",
            adr_id="ADR-R003",
            source_sha256=SHA_A,
            alloy_ref="behavior.als::ActivateEntity",
            target_symbol="advance_state",
            kind=PropertyKind.STATEFUL_TRANSITION,
            title="State version never drops below zero",
            precondition="delta >= 0",
            assertion_expr="state['version'] >= 0",
            input_strategies=[
                HypothesisStrategySpec(
                    parameter_name="delta",
                    strategy_expr="st.integers(min_value=0, max_value=10)",
                )
            ],
            guardrail_enforcement=GuardrailEnforcement.PBT_ONLY,
            max_examples=200,
        ),
    ]
    spec = PBTSpec(
        pbt_spec_id="pbt-orders-exec",
        target_module="orders_impl",
        req_ids=["REQ-001", "REQ-002", "REQ-003", "REQ-004"],
        invariants=[
            PropertyInvariant(
                invariant_id=p.property_id,
                req_id=p.req_id,
                title=p.title,
                precondition=p.precondition,
                assertion_expr=p.assertion_expr,
                strategies=p.input_strategies,
                max_examples=p.max_examples,
            )
            for p in props
        ],
        guardrails=[
            PydanticGuardrail(
                guardrail_id="GR-001",
                model_name="OrderInput",
                field_name="amount",
                constraint_type="range",
                rule="ge=0, le=10000",
            ),
            PydanticGuardrail(
                guardrail_id="GR-002",
                model_name="OrderInput",
                field_name="code",
                constraint_type="regex",
                rule="^[A-Z0-9_-]+$",
            ),
            PydanticGuardrail(
                guardrail_id="GR-003",
                model_name="OrderInput",
                field_name="currency",
                constraint_type="enum",
                rule="USD, EUR, GBP",
            ),
            PydanticGuardrail(
                guardrail_id="GR-004",
                model_name="OrderInput",
                field_name="note",
                constraint_type="type",
                rule="str",
            ),
            PydanticGuardrail(
                guardrail_id="GR-005",
                model_name="OrderInput",
                field_name="actor",
                constraint_type="custom_validator",
                rule="non_null_actor",
                error_message="actor cannot be None",
            ),
            PydanticGuardrail(
                guardrail_id="GR-006",
                model_name="OrderInput",
                field_name="metadata",
                constraint_type="extra_forbid",
                rule="extra=forbid",
            ),
        ],
        alloy_model_ref="core_domain.als",
        seed=42,
    )
    return PBTContractDocument(
        spec=spec,
        session_id="sess-orders-exec",
        source_sha256=SHA_A,
        properties=props,
        seed=42,
    )


# ---------------------------------------------------------------------------
# 1. Model validation tests
# ---------------------------------------------------------------------------


def test_models_strict_validation_and_helpers() -> None:
    doc = _build_executable_contract()
    assert doc.to_pbt_refs_by_req() == {
        "REQ-001": ["PBT-001-AMOUNT"],
        "REQ-002": ["PBT-002-IDEMP"],
        "REQ-003": ["PBT-003-ROUNDTRIP"],
        "REQ-004": ["PBT-004-STATEFUL"],
    }

    with pytest.raises(ValidationError):
        PBTPropertyContract(
            property_id="P-1",
            req_id="REQ-001",
            adr_id="ADR-R001",
            source_sha256="not-a-sha",
            alloy_ref="a.als::x",
            target_symbol="fn",
            kind=PropertyKind.INVARIANT,
            title="t",
            assertion_expr="True",
            max_examples=200,
        )

    with pytest.raises(ValidationError):
        PBTPropertyContract(
            property_id="P-1",
            req_id="REQ-001",
            adr_id="ADR-R001",
            source_sha256=SHA_A,
            alloy_ref="a.als::x",
            target_symbol="fn",
            kind=PropertyKind.INVARIANT,
            title="   ",
            assertion_expr="True",
            max_examples=200,
        )

    with pytest.raises(ValidationError):
        PBTPropertyContract(
            property_id="P-1",
            req_id="REQ-001",
            adr_id="ADR-R001",
            source_sha256=SHA_A,
            alloy_ref="a.als::x",
            target_symbol="fn",
            kind=PropertyKind.INVARIANT,
            title="valid",
            assertion_expr="True",
            max_examples=100,  # Below ge=200 floor
        )

    with pytest.raises(ValidationError):
        PBTPropertyContract(
            property_id="P-1",
            req_id="REQ-001",
            adr_id="ADR-R001",
            source_sha256=SHA_A,
            alloy_ref="a.als::x",
            target_symbol="fn",
            kind=PropertyKind.INVARIANT,
            title="dup strat",
            assertion_expr="True",
            input_strategies=[
                HypothesisStrategySpec(parameter_name="x", strategy_expr="st.integers()"),
                HypothesisStrategySpec(parameter_name="x", strategy_expr="st.integers()"),
            ],
        )

    # Extra field rejected
    with pytest.raises(ValidationError):
        PBTContractDocument(
            spec=doc.spec,
            session_id="s1",
            source_sha256=SHA_A,
            properties=[],
            unexpected="nope",  # type: ignore[call-arg]
        )

    # Duplicate property_id rejected
    dup_prop = doc.properties[0]
    with pytest.raises(ValidationError):
        PBTContractDocument(
            spec=doc.spec,
            session_id="s1",
            source_sha256=SHA_A,
            properties=[dup_prop, dup_prop],
        )

    # Unknown req_id rejected
    orphan_prop = dup_prop.model_copy(update={"property_id": "PBT-999", "req_id": "REQ-999"})
    with pytest.raises(ValidationError):
        PBTContractDocument(
            spec=doc.spec,
            session_id="s1",
            source_sha256=SHA_A,
            properties=[orphan_prop],
        )

    # Report blank validation
    with pytest.raises(ValidationError):
        CounterexampleRepairReport(
            test_name="   ",
            falsifying_example="x=1",
            assertion_error="AssertionError",
            classification=CounterexampleClassification.CODE_DEFECT,
            suggested_action="fix",
        )


# ---------------------------------------------------------------------------
# 2. Pure-Python YAML serializer/parser tests
# ---------------------------------------------------------------------------


def test_yaml_roundtrip_and_edge_cases() -> None:
    doc = _build_executable_contract()
    yaml_text = dump_pbt_spec_yaml(doc)
    assert "\r\n" not in yaml_text
    assert yaml_text.endswith("\n")
    reloaded = load_pbt_spec_yaml(yaml_text)
    assert reloaded == doc

    # Exercise flow syntax, comments, single quotes, nested lists/dicts, floats, bools, nulls
    complex_yaml = (
        "# Header comment\n"
        "flag_t: true # inline comment\n"
        "flag_f: false\n"
        "empty_val: null\n"
        "tilde_val: ~\n"
        "pi: 3.1415\n"
        "neg: -42\n"
        "single: 'it''s ok # not a comment'\n"
        "flow_list: [1, 'two', \"three, four\", []]\n"
        "flow_map: {a: 1, \"b:c\": 'd', empty: {}}\n"
        "empty_flow_list: []\n"
        "empty_flow_map: {}\n"
        "nested_lists:\n"
        "  -\n"
        "    - inner_a\n"
        "    - inner_b\n"
        "  - {}\n"
        "  - []\n"
        "  -\n"
        "dict_list:\n"
        "  - first_nested:\n"
        "      - item1\n"
        "    sibling: ok\n"
        "  - empty_nested: {}\n"
        "  - empty_list_nested: []\n"
        "  - null_key:\n"
        "\"quoted:key\": \"value # keep\"\n"
        "'single_key': plain text\n"
        "blank_child:\n"
    )
    parsed = _parse_yaml_document(complex_yaml)
    assert parsed["flag_t"] is True
    assert parsed["flag_f"] is False
    assert parsed["empty_val"] is None
    assert parsed["tilde_val"] is None
    assert parsed["pi"] == pytest.approx(3.1415)
    assert parsed["neg"] == -42
    assert parsed["single"] == "it's ok # not a comment"
    assert parsed["flow_list"] == [1, "two", "three, four", []]
    assert parsed["flow_map"] == {"a": 1, "b:c": "d", "empty": {}}
    assert parsed["nested_lists"] == [["inner_a", "inner_b"], {}, [], None]
    assert parsed["dict_list"][0] == {"first_nested": ["item1"], "sibling": "ok"}
    assert parsed["dict_list"][3] == {"null_key": None}
    assert parsed["quoted:key"] == "value # keep"
    assert parsed["single_key"] == "plain text"
    assert parsed["blank_child"] is None


def test_yaml_parser_error_paths() -> None:
    with pytest.raises(ValueError, match="Empty YAML"):
        load_pbt_spec_yaml("   \n# just comment\n")

    with pytest.raises(ValueError, match="Tabs are not allowed"):
        load_pbt_spec_yaml("spec:\n\tfoo: bar\n")

    with pytest.raises(ValueError, match="must be a mapping"):
        load_pbt_spec_yaml("- a\n- b\n")

    with pytest.raises(ValueError, match="must contain a 'spec'"):
        load_pbt_spec_yaml("session_id: s1\n")

    with pytest.raises(ValueError, match="Malformed YAML"):
        _parse_yaml_document("not_a_mapping_entry\n")

    with pytest.raises(ValueError, match="Unexpected indentation"):
        _parse_yaml_document("a: 1\n  b: 2\n")

    with pytest.raises(ValueError, match="Unexpected indentation"):
        _parse_yaml_document("- a\n  - b\n")

    with pytest.raises(ValueError, match="Trailing unexpected"):
        _parse_yaml_document("  a: 1\nb: 2\n")

    with pytest.raises(ValueError, match="Empty YAML mapping key"):
        _split_yaml_key_value(": value")

    with pytest.raises(ValueError, match="Invalid double-quoted YAML key"):
        _split_yaml_key_value('"\\x": 1')

    assert _split_yaml_key_value("") is None
    assert _parse_yaml_scalar("") is None

    with pytest.raises(ValueError, match="Invalid double-quoted"):
        _parse_yaml_scalar('"unterminated')

    with pytest.raises(ValueError, match="Unterminated single-quoted"):
        _parse_yaml_scalar("'unterminated")

    with pytest.raises(ValueError, match="Unbalanced YAML flow"):
        _parse_yaml_scalar("[1, 2")

    with pytest.raises(ValueError, match="Invalid flow mapping entry"):
        _parse_yaml_scalar("{not_a_pair}")

    with pytest.raises(TypeError, match="Unsupported YAML scalar"):
        _format_yaml_scalar(object())


# ---------------------------------------------------------------------------
# 3. PBTSpecEmitter tests (deterministic + optional ProviderRouter)
# ---------------------------------------------------------------------------


def test_pbt_spec_emitter_full_traceability_and_router() -> None:
    graph = _sample_graph()
    adrs = _sample_adrs()
    spec_md = AlloySpecEmitter().emit_spec_md(graph, adrs=adrs)
    design_md = _sample_design(graph, adrs)

    emitter = PBTSpecEmitter()
    doc = emitter.emit_contract(
        graph,
        session_id="sess-orders",
        spec_markdown=spec_md,
        adrs=adrs,
        design_markdown=design_md,
        seed=77,
    )
    assert doc.session_id == "sess-orders"
    assert doc.source_sha256 == SHA_A
    assert doc.seed == 77
    assert doc.spec.target_module == "orders_core"
    assert "REQ-006" not in doc.spec.req_ids  # Deprecated excluded
    assert len(doc.properties) == 5

    by_req = {p.req_id: p for p in doc.properties}
    assert by_req["REQ-001"].adr_id == "ADR-R001"
    assert by_req["REQ-001"].alloy_ref.startswith("core_domain.als::")
    assert by_req["REQ-001"].target_symbol == "apply_orders_core_command"
    assert by_req["REQ-001"].kind == PropertyKind.INVARIANT
    assert by_req["REQ-002"].kind == PropertyKind.IDEMPOTENCY
    assert by_req["REQ-003"].kind == PropertyKind.ROUNDTRIP
    assert by_req["REQ-004"].kind == PropertyKind.STATEFUL_TRANSITION
    assert by_req["REQ-005"].kind == PropertyKind.POSTCONDITION
    assert by_req["REQ-005"].adr_id.startswith("ADR-R")

    # Dict-style ADR + multi-source SHA + Alloy pred classification
    multi_graph = RequirementGraph(
        graph_id="multi-src",
        nodes=[
            RequirementNode(
                req_id="REQ-10",
                title="Activate record",
                statement="Activate record cleanly.",
                trace_links=[TraceLink(source_sha256=SHA_A, source_uri="a.md")],
            ),
            RequirementNode(
                req_id="REQ-11",
                title="Check ownership",
                statement="Owner must exist.",
                trace_links=[TraceLink(source_sha256=SHA_B, source_uri="b.md")],
            ),
        ],
    )
    custom_spec = (
        "```alloy module=behavior\n"
        "// @trace REQ-10\n"
        "pred DoActivate {\n  some x\n}\n"
        "```\n"
    )
    doc_multi = emitter.emit_contract(
        multi_graph,
        session_id="sess-multi",
        spec_markdown=custom_spec,
        adrs=[
            {"adr_id": "ADR-R010", "kind": "routing", "frontmatter": {"requirement_ids": ["REQ-10"]}},
            {"adr_id": "", "req_ids": ["REQ-11"]},
        ],
    )
    assert doc_multi.source_sha256 != SHA_A and len(doc_multi.source_sha256) == 64
    assert doc_multi.properties[0].kind == PropertyKind.STATEFUL_TRANSITION
    assert doc_multi.properties[0].adr_id == "ADR-R010"
    assert doc_multi.properties[1].alloy_ref == "spec.md::REQ-11"

    # Unlinked graph with empty spec_markdown uses deterministic hash fallback
    unlinked_graph = RequirementGraph(
        graph_id="123-graph",
        nodes=[
            RequirementNode(req_id="REQ-1", title="Simple rule", statement="Keep valid."),
        ],
    )
    doc_unlinked = emitter.emit_contract(
        unlinked_graph,
        session_id="sess-unlinked",
        spec_markdown="",
    )
    assert len(doc_unlinked.source_sha256) == 64
    assert doc_unlinked.spec.target_module.startswith("domain_")

    # Empty active graph raises ValueError
    dep_graph = RequirementGraph(
        graph_id="dep-only",
        nodes=[
            RequirementNode(
                req_id="REQ-OLD",
                title="Old",
                statement="Old statement.",
                status=RequirementStatus.DEPRECATED,
            )
        ],
    )
    with pytest.raises(ValueError, match="no active requirements"):
        emitter.emit_contract(dep_graph, session_id="s-dep", spec_markdown="")

    # Optional ProviderRouter enrichment: valid enrichment + invalid syntax ignored + failure fallback
    valid_llm_json = (
        '{"enrichments": ['
        '{"req_id": "REQ-001", "title": "Enriched settlement non-negativity", '
        '"precondition": "amount >= 0", "assertion_expr": "result >= 0"},'
        '{"req_id": "REQ-002", "title": "", "precondition": "def broken(", "assertion_expr": "???"}'
        "]}"
    )
    router_ok = ProviderRouter(_FakeProvider(valid_llm_json), strict_local=True, enable_cache=False)
    enriched_doc = PBTSpecEmitter(router=router_ok).emit_contract(
        graph,
        session_id="sess-enriched",
        spec_markdown=spec_md,
        adrs=adrs,
        design_markdown=design_md,
    )
    assert enriched_doc.properties[0].title == "Enriched settlement non-negativity"
    # Invalid LLM assertion syntax was ignored safely
    assert enriched_doc.properties[1].assertion_expr == "first == second"

    router_fail = ProviderRouter(_FakeProvider(should_raise=True), strict_local=True, enable_cache=False)
    fallback_doc = PBTSpecEmitter(router=router_fail).emit_contract(
        graph,
        session_id="sess-fallback",
        spec_markdown=spec_md,
    )
    assert len(fallback_doc.properties) == 5


# ---------------------------------------------------------------------------
# 4. Codegen + Runtime Guardrail + Contract Lock tests
# ---------------------------------------------------------------------------


def test_codegen_guardrails_runtime_enforcement_and_ast_gate(tmp_path: Path) -> None:
    doc = _build_executable_contract()
    bundle = export_pbt_bundle(doc, tmp_path)
    assert isinstance(bundle, GeneratedGuardrailBundle)
    assert bundle.pbt_spec_yaml.is_file()
    assert bundle.lock_file.is_file()
    assert len(bundle.guardrail_files) == 1
    assert len(bundle.test_files) == 1

    # Verify LF line endings and lock integrity
    assert b"\r\n" not in bundle.pbt_spec_yaml.read_bytes()
    assert b"\r\n" not in bundle.lock_file.read_bytes()
    verified = verify_contract_lock(tmp_path)
    assert "PBT_Spec.yaml" in verified
    assert "tests/properties/test_orders_impl_pbt.py" in verified

    # Dynamically import the generated guardrails module and test Pydantic + @enforce_contract
    guard_path = bundle.guardrail_files[0]
    spec = importlib.util.spec_from_file_location("generated_orders_guards", guard_path)
    assert spec is not None and spec.loader is not None
    guards_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guards_mod)

    valid_input = guards_mod.OrderInput(
        amount=100,
        code="ORD-1",
        currency="USD",
        note="ok",
        actor="alice",
    )
    assert valid_input.amount == 100

    with pytest.raises(ValidationError):
        guards_mod.OrderInput(
            amount=-1,
            code="ORD-1",
            currency="USD",
            note="ok",
            actor="alice",
        )

    with pytest.raises(ValidationError):
        guards_mod.OrderInput(
            amount=10,
            code="ORD-1",
            currency="USD",
            note="ok",
            actor="alice",
            rogue_extra=True,
        )

    with pytest.raises(ValidationError):
        guards_mod.OrderInput(
            amount=10,
            code="ORD-1",
            currency="USD",
            note="ok",
            actor=None,
        )

    @guards_mod.guard_pbt_001_amount
    def _good_clamp(amount: int) -> int:
        return max(0, amount)

    @guards_mod.guard_pbt_001_amount
    def _bad_clamp(amount: int) -> int:
        return -99

    assert _good_clamp(25) == 25
    with pytest.raises(guards_mod.ContractViolationError, match="Precondition violated"):
        _good_clamp(-999)
    with pytest.raises(guards_mod.ContractViolationError, match="Postcondition violated"):
        _bad_clamp(10)

    # Empty guardrails + empty strategies + no precondition edge cases still produce valid AST
    minimal_prop = PBTPropertyContract(
        property_id="PBT-MIN",
        req_id="REQ-1",
        adr_id="ADR-R001",
        source_sha256=SHA_A,
        alloy_ref="a.als::x",
        target_symbol="class",  # keyword target symbol
        kind=PropertyKind.INVARIANT,
        title="Minimal invariant",
        precondition="",
        assertion_expr="True",
        input_strategies=[],
        guardrail_enforcement=GuardrailEnforcement.BOTH,
    )
    minimal_doc = PBTContractDocument(
        spec=PBTSpec(
            pbt_spec_id="pbt-min",
            target_module="def",  # keyword target module
            req_ids=["REQ-1"],
            invariants=[],
            guardrails=[],
        ),
        session_id="s-min",
        source_sha256=SHA_A,
        properties=[minimal_prop],
    )
    g_files, t_files = compile_pbt_guardrails(minimal_doc, tmp_path / "min")
    ast.parse(g_files[0].read_text(encoding="utf-8"))
    ast.parse(t_files[0].read_text(encoding="utf-8"))

    # Invalid Python assertion fails closed BEFORE writing to disk
    broken_prop = minimal_prop.model_copy(update={"assertion_expr": "def not valid ("})
    broken_doc = minimal_doc.model_copy(update={"properties": [broken_prop]})
    broken_dir = tmp_path / "broken_out"
    with pytest.raises(ValueError, match="Invalid Python expression"):
        compile_pbt_guardrails(broken_doc, broken_dir)
    assert not broken_dir.exists()


def test_contract_lock_error_paths(tmp_path: Path) -> None:
    sample = tmp_path / "PBT_Spec.yaml"
    sample.write_bytes(b"k: v\n")

    # Relative path entry works
    lock_path = write_contract_lock(tmp_path, [Path("PBT_Spec.yaml")])
    assert verify_contract_lock(tmp_path) == {
        "PBT_Spec.yaml": hashlib.sha256(b"k: v\n").hexdigest()
    }

    with pytest.raises(ContractTamperError, match="empty file list"):
        write_contract_lock(tmp_path, [])

    with pytest.raises(ContractTamperError, match="does not exist"):
        write_contract_lock(tmp_path, [tmp_path / "missing.yaml"])

    outside = tmp_path.parent / "outside_reqhq_test_file.txt"
    outside.write_text("x", encoding="utf-8")
    try:
        with pytest.raises(ContractTamperError, match="escapes lock root"):
            write_contract_lock(tmp_path, [outside])
    finally:
        outside.unlink(missing_ok=True)

    # Missing lock file
    lock_path.unlink()
    with pytest.raises(ContractTamperError, match="lock file missing"):
        verify_contract_lock(tmp_path)

    # Empty lock file
    lock_path.write_text("\n\n", encoding="utf-8")
    with pytest.raises(ContractTamperError, match="zero entries"):
        verify_contract_lock(tmp_path)

    # Malformed line
    lock_path.write_text("not-a-valid-sha  PBT_Spec.yaml\n", encoding="utf-8")
    with pytest.raises(ContractTamperError, match="Malformed contract lock"):
        verify_contract_lock(tmp_path)

    # Duplicate entry
    digest = hashlib.sha256(b"k: v\n").hexdigest()
    lock_path.write_text(
        f"{digest}  PBT_Spec.yaml\n{digest}  PBT_Spec.yaml\n",
        encoding="utf-8",
    )
    with pytest.raises(ContractTamperError, match="Duplicate lock entry"):
        verify_contract_lock(tmp_path)

    # Path traversal
    lock_path.write_text(f"{digest}  ../escape.yaml\n", encoding="utf-8")
    with pytest.raises(ContractTamperError, match="Illegal path traversal"):
        verify_contract_lock(tmp_path)

    # Missing target file
    lock_path.write_text(f"{digest}  gone.yaml\n", encoding="utf-8")
    with pytest.raises(ContractTamperError, match="file is missing"):
        verify_contract_lock(tmp_path)

    # 1-byte tamper
    write_contract_lock(tmp_path, [sample])
    sample.write_text("k: w\n", encoding="utf-8")
    with pytest.raises(ContractTamperError, match="Contract tamper detected"):
        verify_contract_lock(tmp_path)


# ---------------------------------------------------------------------------
# 5. Counterexample feedback parser unit tests
# ---------------------------------------------------------------------------


def test_parse_hypothesis_counterexamples_classifications() -> None:
    doc = _build_executable_contract()
    assert parse_hypothesis_counterexamples("", doc) == []

    sample_log = """
________________________________ test_pbt_001_amount ________________________________
    assert (result >= 0), '[PBT-001-AMOUNT | REQ-001] Clamped amount is always non-negative'
E       AssertionError: [PBT-001-AMOUNT | REQ-001] Clamped amount is always non-negative
E       assert -1 >= 0
Falsifying example: test_pbt_001_amount(
    amount=-1,
)
You can reproduce this example by temporarily adding @reproduce_failure(...)

________________________________ test_strategy_issue ________________________________
E       hypothesis.errors.Unsatisfiable: Unable to satisfy assumptions of hypothesis strategy
Falsifying example: test_strategy_issue(
    x=0,
)

________________________________ test_ambiguous_rule ________________________________
E       AssertionError: [PBT-003-ROUNDTRIP | REQ-003] Spec_ambiguity: boundary unclear for empty delimiter
Falsifying example: test_ambiguous_rule(
    payload='a',
)
"""
    reports = parse_hypothesis_counterexamples(sample_log, doc)
    assert len(reports) == 3

    assert reports[0].test_name == "test_pbt_001_amount"
    assert reports[0].property_id == "PBT-001-AMOUNT"
    assert reports[0].req_id == "REQ-001"
    assert reports[0].classification == CounterexampleClassification.CODE_DEFECT
    assert "amount=-1" in reports[0].falsifying_example

    assert reports[1].test_name == "test_strategy_issue"
    assert reports[1].classification == CounterexampleClassification.STRATEGY_MISMATCH
    assert "Refine Hypothesis input strategies" in reports[1].suggested_action

    assert reports[2].property_id == "PBT-003-ROUNDTRIP"
    assert reports[2].req_id == "REQ-003"
    assert reports[2].classification == CounterexampleClassification.SPEC_AMBIGUITY
    assert "Escalate" in reports[2].suggested_action

    # Headerless stateful trace lookup via doc
    stateful_log = """
E       AssertionError: negative version
Falsifying example:
state = TestPbt004StatefulStateMachine()
state.step_pbt_004_stateful(delta=1)
state.teardown()
"""
    stateful_reports = parse_hypothesis_counterexamples(stateful_log, doc)
    assert len(stateful_reports) == 1
    assert "state.step_pbt_004_stateful" in stateful_reports[0].falsifying_example
    assert stateful_reports[0].classification == CounterexampleClassification.CODE_DEFECT


# ---------------------------------------------------------------------------
# 6. MANDATORY REAL EXECUTION TEST (valid passes, broken fails + parsed)
# ---------------------------------------------------------------------------


def test_real_execution_generated_pbt_passes_valid_and_catches_broken(tmp_path: Path) -> None:
    doc = _build_executable_contract()
    bundle = export_pbt_bundle(doc, tmp_path)
    test_file = bundle.test_files[0]

    impl_dir = tmp_path / "impl_pkg"
    impl_dir.mkdir()
    impl_file = impl_dir / "orders_impl.py"

    # (A) Valid implementation: all 4 generated Hypothesis tests (incl. RuleBasedStateMachine) PASS!
    impl_file.write_text(
        "def clamp_amount(amount: int) -> int:\n"
        "    return max(0, amount)\n\n"
        "def normalize_code(value: int) -> int:\n"
        "    return value % 50\n\n"
        "def roundtrip_text(payload: str) -> str:\n"
        "    return payload.encode('utf-8').decode('utf-8')\n\n"
        "def advance_state(state: dict, delta: int) -> dict:\n"
        "    return {'version': state['version'] + delta, 'events': [*state.get('events', []), delta]}\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{impl_dir}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env["REQHQ_TARGET_MODULE"] = "orders_impl"

    pass_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(test_file),
            "-v",
            "--tb=short",
            "-W",
            "error::hypothesis.errors.HypothesisWarning",
        ],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert pass_proc.returncode == 0, f"Expected pass:\n{pass_proc.stdout}\n{pass_proc.stderr}"

    # (B) Broken implementation: violates clamp_amount & normalize_code idempotency -> FAILS!
    impl_file.write_text(
        "def clamp_amount(amount: int) -> int:\n"
        "    return amount  # BUG: allows negative amounts\n\n"
        "def normalize_code(value: int) -> int:\n"
        "    return value + 1  # BUG: not idempotent\n\n"
        "def roundtrip_text(payload: str) -> str:\n"
        "    return payload\n\n"
        "def advance_state(state: dict, delta: int) -> dict:\n"
        "    return {'version': state['version'] + delta, 'events': []}\n",
        encoding="utf-8",
    )

    fail_proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "--tb=short"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert fail_proc.returncode != 0
    combined_output = f"{fail_proc.stdout}\n{fail_proc.stderr}"
    assert "Falsifying example:" in combined_output or "Failing test case:" in combined_output

    reports = parse_hypothesis_counterexamples(combined_output, doc)
    assert len(reports) >= 2
    found_prop_ids = {r.property_id for r in reports}
    assert "PBT-001-AMOUNT" in found_prop_ids
    assert "PBT-002-IDEMP" in found_prop_ids
    for report in reports:
        assert report.classification == CounterexampleClassification.CODE_DEFECT
        assert report.req_id in {"REQ-001", "REQ-002"}
        assert report.falsifying_example


# ---------------------------------------------------------------------------
# 7. MANDATORY DETERMINISTIC HYPOTHESIS PROPERTY-BASED TESTS
# ---------------------------------------------------------------------------

_SAFE_ID = st.from_regex(r"[A-Za-z][A-Za-z0-9_-]{1,12}", fullmatch=True)
_PY_IDENT = st.sampled_from(["value", "amount", "count", "payload", "entity_id", "item_score"])
_SAFE_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    min_size=1,
    max_size=40,
).filter(lambda s: bool(s.strip()))


@st.composite
def _pbt_contract_documents(draw: st.DrawFn) -> PBTContractDocument:
    req_num = draw(st.integers(min_value=1, max_value=3))
    req_ids = [f"REQ-{i:03d}" for i in range(1, req_num + 1)]
    seed_val = draw(st.integers(min_value=-1000, max_value=10000))
    spec_seed = draw(st.one_of(st.none(), st.integers(min_value=0, max_value=10000)))
    session_id = draw(_SAFE_ID)
    target_mod = draw(st.sampled_from(["ledger_core", "orders_engine", "vault_domain", "billing"]))

    properties: list[PBTPropertyContract] = []
    invariants: list[PropertyInvariant] = []
    guardrails: list[PydanticGuardrail] = []

    guardrail_specs: list[tuple[str, str]] = [
        ("range", "ge=0, le=100"),
        ("regex", "^[a-z0-9_]+$"),
        ("enum", "OPEN, CLOSED"),
        ("type", "int"),
        ("custom_validator", "non_null"),
        ("extra_forbid", "extra=forbid"),
    ]

    for idx, req_id in enumerate(req_ids, start=1):
        kind = draw(st.sampled_from(list(PropertyKind)))
        enforcement = draw(st.sampled_from(list(GuardrailEnforcement)))
        param = draw(_PY_IDENT)
        title = draw(_SAFE_TEXT)
        desc = draw(
            st.text(
                alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
                min_size=0,
                max_size=24,
            )
        )
        max_ex = draw(st.integers(min_value=200, max_value=10000))
        prop_id = f"PBT-{idx:03d}-{req_id}"
        strategy = HypothesisStrategySpec(
            parameter_name=param,
            strategy_expr="st.integers(min_value=0, max_value=100)",
            description=desc,
            constraints=["ge=0"],
        )
        assertion = "state['version'] >= 0" if kind == PropertyKind.STATEFUL_TRANSITION else f"{param} >= 0"
        precondition = draw(st.sampled_from(["", f"{param} >= 0"]))
        g_type, g_rule = draw(st.sampled_from(guardrail_specs))

        prop = PBTPropertyContract(
            property_id=prop_id,
            req_id=req_id,
            adr_id=f"ADR-R{idx:03d}",
            source_sha256=SHA_A,
            alloy_ref=f"core_domain.als::Inv_{idx}",
            target_symbol=f"handle_{idx}",
            kind=kind,
            title=title,
            precondition=precondition,
            assertion_expr=assertion,
            input_strategies=[strategy],
            guardrail_enforcement=enforcement,
            max_examples=max_ex,
        )
        properties.append(prop)
        invariants.append(
            PropertyInvariant(
                invariant_id=prop_id,
                req_id=req_id,
                title=title,
                precondition=precondition,
                assertion_expr=assertion,
                strategies=[strategy],
                max_examples=max_ex,
            )
        )
        guardrails.append(
            PydanticGuardrail(
                guardrail_id=f"GR-{idx:03d}",
                model_name=f"DomainModel{idx}",
                field_name=param,
                constraint_type=g_type,  # type: ignore[arg-type]
                rule=g_rule,
                error_message=desc,
            )
        )

    spec = PBTSpec(
        pbt_spec_id=f"pbt-{session_id}",
        target_module=target_mod,
        req_ids=req_ids,
        invariants=invariants,
        guardrails=guardrails,
        alloy_model_ref="core_domain.als",
        seed=spec_seed,
    )
    return PBTContractDocument(
        spec=spec,
        session_id=session_id,
        source_sha256=SHA_A,
        properties=properties,
        seed=seed_val,
    )


@settings(derandomize=True, max_examples=50, deadline=None)
@given(doc=_pbt_contract_documents())
def test_pbt_yaml_roundtrip_fidelity(doc: PBTContractDocument) -> None:
    """Property (a): Arbitrary valid PBTContractDocuments round-trip losslessly through YAML."""
    dumped = dump_pbt_spec_yaml(doc)
    assert "\r\n" not in dumped
    reloaded = load_pbt_spec_yaml(dumped)
    assert reloaded == doc
    assert reloaded.spec == doc.spec
    assert dump_pbt_spec_yaml(reloaded) == dumped


@settings(derandomize=True, max_examples=50, deadline=None)
@given(
    target_choice=st.sampled_from(["yaml", "guardrail", "test"]),
    offset_ratio=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    xor_mask=st.integers(min_value=1, max_value=255),
)
def test_pbt_single_byte_tamper_always_triggers_error(
    tmp_path_factory: pytest.TempPathFactory,
    target_choice: str,
    offset_ratio: float,
    xor_mask: int,
) -> None:
    """Property (b): Flipping 1 byte at ANY offset in locked PBT files ALWAYS triggers ContractTamperError."""
    root = tmp_path_factory.mktemp("lock_pbt")
    doc = _build_executable_contract()
    bundle = export_pbt_bundle(doc, root)

    if target_choice == "yaml":
        target_file = bundle.pbt_spec_yaml
    elif target_choice == "guardrail":
        target_file = bundle.guardrail_files[0]
    else:
        target_file = bundle.test_files[0]
    original = bytearray(target_file.read_bytes())
    assert len(original) > 0

    idx = min(len(original) - 1, int(offset_ratio * len(original)))
    original[idx] ^= xor_mask
    target_file.write_bytes(bytes(original))

    with pytest.raises(ContractTamperError):
        verify_contract_lock(root)


@settings(derandomize=True, max_examples=50, deadline=None)
@given(doc=_pbt_contract_documents())
def test_pbt_codegen_always_emits_valid_python_ast(
    tmp_path_factory: pytest.TempPathFactory,
    doc: PBTContractDocument,
) -> None:
    """Property (c): Arbitrary valid PBTContractDocuments always emit ast.parse()-clean Python."""
    out_dir = tmp_path_factory.mktemp("codegen_pbt")
    guardrail_files, test_files = compile_pbt_guardrails(doc, out_dir)
    assert len(guardrail_files) == 1
    assert len(test_files) == 1

    for py_file in (*guardrail_files, *test_files):
        source = py_file.read_text(encoding="utf-8")
        assert "\r\n" not in source
        tree = ast.parse(source, filename=str(py_file))
        assert isinstance(tree, ast.Module)

    test_src = test_files[0].read_text(encoding="utf-8")
    assert "derandomize=True" in test_src
    for prop in doc.properties:
        assert f"max_examples={prop.max_examples}" in test_src


def test_edge_cases_across_pbt_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise remaining defensive edge paths across models, emitter, codegen, and feedback."""
    from reqhq.pbt.codegen import (
        _pascal_ident as cg_pascal,
        _safe_ident as cg_safe,
        _validate_python_source,
    )
    from reqhq.pbt.emitter import (
        _emit_yaml_lines,
        _is_valid_python_expr,
        _pascal_ident as em_pascal,
        _sanitize_ident as em_safe,
    )

    # models.py: 64-char non-hex SHA-256 hits _validate_sha256
    with pytest.raises(ValidationError, match="64-character hex"):
        PBTPropertyContract(
            property_id="P-HEX",
            req_id="REQ-001",
            adr_id="ADR-R001",
            source_sha256="z" * 64,
            alloy_ref="a.als::x",
            target_symbol="fn",
            kind=PropertyKind.INVARIANT,
            title="t",
            assertion_expr="True",
        )

    # emitter.py YAML emission & parsing edge cases
    complex_obj: dict[str, Any] = {
        "a:b": None,
        "flag": True,
        "empty_d": {},
        "empty_l": [],
        "seq": [
            {"nested_d": {"k": "v"}, "tail": False},
            {"nested_l": [1, 2]},
            {"empty_nested_d": {}},
            {"empty_nested_l": []},
            [10, 20],
            {},
            [],
        ],
    }
    dumped = "\n".join(_emit_yaml_lines(complex_obj)) + "\n"
    assert _parse_yaml_document(dumped) == complex_obj
    assert _emit_yaml_lines({}) == ["{}"]
    assert _emit_yaml_lines([]) == ["[]"]
    assert _emit_yaml_lines(42) == ["42"]

    assert _parse_yaml_scalar('["a, \\"b\\"", \'c, d\', [1, 2]]') == ['a, "b"', "c, d", [1, 2]]
    assert _parse_yaml_scalar("{   }") == {}
    assert _parse_yaml_scalar("[   ]") == []
    with pytest.raises(ValueError, match="Trailing unexpected"):
        _parse_yaml_document("- a\n- b\nk: v\n")
    with pytest.raises(ValueError, match="Trailing unexpected"):
        _parse_yaml_document("k: v\n- stray\n")
    with pytest.raises(ValueError, match="Unbalanced YAML flow"):
        _parse_yaml_scalar("{a: 1")
    with pytest.raises(ValueError, match="Unbalanced YAML flow"):
        _parse_yaml_scalar("[1, [2]")

    # contract_lock.py lines 106-107: symlink/junction resolving outside lock root fails closed
    lock_root = tmp_path / "symlink_lock_root"
    lock_root.mkdir()
    outside_target = tmp_path / "escaped_outside.yaml"
    outside_target.write_bytes(b"secret: 1\n")
    digest = hashlib.sha256(b"secret: 1\n").hexdigest()
    (lock_root / "pbt_lock.sha256").write_bytes(f"{digest}  linked.yaml\n".encode("utf-8"))
    orig_resolve = Path.resolve

    def _simulated_symlink_resolve(self: Path, strict: bool = False) -> Path:
        if self.name == "linked.yaml":
            return orig_resolve(outside_target, strict=strict)
        return orig_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", _simulated_symlink_resolve)
    with pytest.raises(ContractTamperError, match="resolves outside lock root"):
        verify_contract_lock(lock_root)
    monkeypatch.undo()

    assert _is_valid_python_expr("   ") is False
    assert em_safe("class") == "item_class"
    assert em_pascal("123").startswith("DomainModel")
    assert em_pascal("True") == "TrueModel"
    assert cg_safe("123") == "item_123"
    assert cg_pascal("123").startswith("Contract")
    assert cg_pascal("True") == "TrueModel"

    with pytest.raises(ValueError, match="failed ast.parse"):
        _validate_python_source("def broken(:", "broken.py")

    # Router returning non-JSON (parsed=None) and invalid parsed dict
    graph = RequirementGraph(
        graph_id="edge-g",
        nodes=[RequirementNode(req_id="REQ-1", title="Edge", statement="Check edge.")],
    )
    router_bad_json = ProviderRouter(
        _FakeProvider("not valid json"),
        strict_local=True,
        enable_cache=False,
        max_local_retries=0,
    )
    doc_bad_json = PBTSpecEmitter(router=router_bad_json).emit_contract(
        graph, session_id="s-edge", spec_markdown=""
    )
    assert len(doc_bad_json.properties) == 1

    class _BadParsedRouter:
        def route(self, request: LLMRequest) -> LLMResponse:
            return LLMResponse(
                text="{}",
                provider="fake",
                model="fake",
                parsed={"bad_field": "triggers_validation_error"},
            )

    doc_bad_parsed = PBTSpecEmitter(router=_BadParsedRouter()).emit_contract(  # type: ignore[arg-type]
        graph, session_id="s-edge2", spec_markdown=""
    )
    assert len(doc_bad_parsed.properties) == 1

    # codegen edge cases: empty range/enum rules, duplicate field, stateless precondition w/o strategies, stateful w/o strategies
    edge_doc = PBTContractDocument(
        spec=PBTSpec(
            pbt_spec_id="pbt-edge",
            target_module="edge_mod",
            req_ids=["REQ-1", "REQ-2"],
            guardrails=[
                PydanticGuardrail(
                    guardrail_id="G1",
                    model_name="EdgeModel",
                    field_name="x",
                    constraint_type="range",
                    rule="no_bounds_here",
                ),
                PydanticGuardrail(
                    guardrail_id="G2",
                    model_name="EdgeModel",
                    field_name="x",  # duplicate field skipped cleanly
                    constraint_type="type",
                    rule="int",
                ),
                PydanticGuardrail(
                    guardrail_id="G3",
                    model_name="EdgeModel",
                    field_name="choice",
                    constraint_type="enum",
                    rule=" , ",
                ),
            ],
        ),
        session_id="s-edge",
        source_sha256=SHA_A,
        properties=[
            PBTPropertyContract(
                property_id="PBT-EDGE-1",
                req_id="REQ-1",
                adr_id="ADR-R001",
                source_sha256=SHA_A,
                alloy_ref="a.als::x",
                target_symbol="do_edge",
                kind=PropertyKind.INVARIANT,
                title="No-strategy precondition",
                precondition="True",
                assertion_expr="True",
                input_strategies=[],
            ),
            PBTPropertyContract(
                property_id="PBT-EDGE-2",
                req_id="REQ-2",
                adr_id="ADR-R002",
                source_sha256=SHA_A,
                alloy_ref="b.als::y",
                target_symbol="step_edge",
                kind=PropertyKind.STATEFUL_TRANSITION,
                title="No-strategy stateful",
                precondition="",
                assertion_expr="state['version'] >= 0",
                input_strategies=[],
                guardrail_enforcement=GuardrailEnforcement.PBT_ONLY,
            ),
        ],
    )
    g_files, t_files = compile_pbt_guardrails(edge_doc, tmp_path / "edge_cg")
    ast.parse(g_files[0].read_text(encoding="utf-8"))
    ast.parse(t_files[0].read_text(encoding="utf-8"))

    # counterexample_feedback edge cases: doc=None, raw ContractViolationError, no error fallback, dotted test_name
    no_doc_reports = parse_hypothesis_counterexamples(
        "ContractViolationError: bad state\nFalsifying example: test_sample(\n    a=1,\n)\n",
        doc=None,
    )
    assert len(no_doc_reports) == 1
    assert no_doc_reports[0].test_name == "test_sample"
    assert "ContractViolationError" in no_doc_reports[0].assertion_error

    fallback_err_reports = parse_hypothesis_counterexamples(
        "____ TestPbt004StatefulStateMachine.custom ____\nFalsifying example: state = S()\n",
        doc=_build_executable_contract(),
    )
    assert len(fallback_err_reports) == 1
    assert fallback_err_reports[0].property_id == "PBT-004-STATEFUL"
    assert "Hypothesis falsified" in fallback_err_reports[0].assertion_error

