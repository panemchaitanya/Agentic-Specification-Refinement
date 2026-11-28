"""Offline unit and Hypothesis property tests for reqhq.config, schemas, and store."""

from __future__ import annotations

import keyword
import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from reqhq.config import ReqHQConfig
from reqhq.schemas import (
    BrownfieldArtifactRef,
    EdgeKind,
    GreenfieldEmptyContext,
    HypothesisStrategySpec,
    PBTSpec,
    PropertyInvariant,
    PydanticGuardrail,
    RequirementEdge,
    RequirementGraph,
    RequirementKind,
    RequirementNode,
    RequirementStatus,
    RoutingADR,
    RoutingDecisionKind,
    SystemContext,
    SystemContextBase,
    TraceabilityChain,
    TraceLink,
)
from reqhq.store import EventKind, PromptCache, SessionEvent, SessionEventLog

SAMPLE_SHA = "a" * 64
SAMPLE_SHA_UPPER = "A1B2C3D4" * 8

# ---------------------------------------------------------------------------
# Hypothesis strategies for strict Pydantic models
# ---------------------------------------------------------------------------

hex64_st = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)
non_blank_st = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), min_codepoint=48, max_codepoint=122),
    min_size=1,
    max_size=24,
)
py_ident_st = st.from_regex(r"[a-z][a-z0-9_]{0,14}", fullmatch=True).filter(
    lambda s: not keyword.iskeyword(s)
)


@st.composite
def trace_link_st(draw: st.DrawFn) -> TraceLink:
    return TraceLink(
        source_sha256=draw(hex64_st),
        source_uri=f"docs/{draw(non_blank_st)}.md",
        anchor=f"[page:{draw(st.integers(min_value=1, max_value=50))}]",
        excerpt=draw(st.text(max_size=40)),
        chunk_id=draw(st.none() | non_blank_st),
    )


@st.composite
def requirement_dag_st(draw: st.DrawFn) -> RequirementGraph:
    count = draw(st.integers(min_value=1, max_value=6))
    nodes: list[RequirementNode] = []
    for idx in range(count):
        nodes.append(
            RequirementNode(
                req_id=f"REQ-{idx:03d}",
                title=f"Title {idx} {draw(non_blank_st)}",
                statement=f"System shall satisfy {draw(non_blank_st)}",
                kind=draw(st.sampled_from(list(RequirementKind))),
                status=draw(st.sampled_from(list(RequirementStatus))),
                priority=draw(st.sampled_from(["P0", "P1", "P2", "P3"])),
                acceptance_criteria=draw(st.lists(non_blank_st, max_size=2)),
                trace_links=draw(st.lists(trace_link_st(), min_size=1, max_size=2)),
                tags=draw(st.lists(non_blank_st, max_size=2)),
            )
        )

    edges: list[RequirementEdge] = []
    # Only orient edges from lower index to higher index -> guaranteed DAG
    for i in range(count):
        for j in range(i + 1, count):
            if draw(st.booleans()):
                edges.append(
                    RequirementEdge(
                        source_id=f"REQ-{i:03d}",
                        target_id=f"REQ-{j:03d}",
                        relation=draw(st.sampled_from(list(EdgeKind))),
                    )
                )

    return RequirementGraph(graph_id=f"G-{draw(non_blank_st)}", nodes=nodes, edges=edges)


# ---------------------------------------------------------------------------
# ReqHQConfig tests
# ---------------------------------------------------------------------------


def test_config_defaults_and_resolved_paths() -> None:
    cfg = ReqHQConfig()
    assert cfg.sessions_dir == Path(".reqhq_sessions")
    assert cfg.adr_dir == Path("docs/adr")
    assert cfg.strict_local is False
    assert cfg.hardware_tier_override is None
    assert cfg.ollama_base_url == "http://127.0.0.1:11434"
    assert cfg.quota_ceiling_ratio == pytest.approx(0.90)
    assert cfg.resolved_quota_db_path() == Path(".reqhq_sessions/quota_ledger.db")
    assert cfg.resolved_cache_db_path() == Path(".reqhq_sessions/prompt_cache.db")


def test_config_explicit_db_paths_and_validation(tmp_path: Path) -> None:
    qdb = tmp_path / "custom_quota.db"
    cdb = tmp_path / "custom_cache.db"
    cfg = ReqHQConfig(
        sessions_dir=tmp_path / "sessions",
        ollama_base_url="http://localhost:11434/",
        quota_db_path=qdb,
        cache_db_path=cdb,
        hardware_tier_override=2,
        quota_ceiling_ratio=0.85,
    )
    assert cfg.ollama_base_url == "http://localhost:11434"
    assert cfg.resolved_quota_db_path() == qdb
    assert cfg.resolved_cache_db_path() == cdb

    with pytest.raises(ValidationError):
        ReqHQConfig(unknown_extra_field=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ReqHQConfig(hardware_tier_override=4)
    with pytest.raises(ValidationError):
        ReqHQConfig(hardware_tier_override=True)
    with pytest.raises(ValidationError):
        ReqHQConfig(quota_ceiling_ratio=0.0)
    with pytest.raises(ValidationError):
        ReqHQConfig(quota_ceiling_ratio=float("nan"))
    with pytest.raises(ValidationError):
        ReqHQConfig(ollama_base_url="ftp://localhost:11434")
    with pytest.raises(ValidationError):
        ReqHQConfig(ollama_base_url="http://")


def test_config_from_env() -> None:
    env = {
        "REQHQ_SESSIONS_DIR": "custom_sessions",
        "REQHQ_ADR_DIR": "custom_adrs",
        "REQHQ_STRICT_LOCAL": "true",
        "REQHQ_HARDWARE_TIER": "1",
        "OLLAMA_BASE_URL": "https://ollama.internal:11434/",
        "REQHQ_QUOTA_CEILING_RATIO": "0.75",
        "REQHQ_QUOTA_DB_PATH": "q.db",
        "REQHQ_CACHE_DB_PATH": "c.db",
    }
    cfg = ReqHQConfig.from_env(env)
    assert cfg.sessions_dir == Path("custom_sessions")
    assert cfg.adr_dir == Path("custom_adrs")
    assert cfg.strict_local is True
    assert cfg.hardware_tier_override == 1
    assert cfg.ollama_base_url == "https://ollama.internal:11434"
    assert cfg.quota_ceiling_ratio == pytest.approx(0.75)
    assert cfg.quota_db_path == Path("q.db")
    assert cfg.cache_db_path == Path("c.db")

    assert ReqHQConfig.from_env({"REQHQ_STRICT_LOCAL": "0"}).strict_local is False
    assert ReqHQConfig.from_env({}).strict_local is False

    with pytest.raises(ValueError, match="Invalid boolean"):
        ReqHQConfig.from_env({"REQHQ_STRICT_LOCAL": "maybe"})
    with pytest.raises(ValueError, match="Invalid integer"):
        ReqHQConfig.from_env({"REQHQ_HARDWARE_TIER": "high"})
    with pytest.raises(ValueError, match="Invalid float"):
        ReqHQConfig.from_env({"REQHQ_QUOTA_CEILING_RATIO": "ninety"})


# ---------------------------------------------------------------------------
# Hypothesis round-trip & extra="forbid" property tests
# ---------------------------------------------------------------------------


@settings(max_examples=30, deadline=None)
@given(graph=requirement_dag_st())
def test_hypothesis_requirement_graph_roundtrip_and_dag(graph: RequirementGraph) -> None:
    dumped = graph.model_dump_json()
    restored = RequirementGraph.model_validate_json(dumped)
    assert restored == graph
    assert restored.is_dag() is True
    assert restored.validate_dag() == []
    assert restored.validate_reference_integrity() == []

    order = restored.topological_order()
    assert len(order) == len(restored.nodes)
    pos = {rid: idx for idx, rid in enumerate(order)}
    for edge in restored.edges:
        if edge.relation != EdgeKind.CONFLICTS_WITH:
            assert pos[edge.source_id] < pos[edge.target_id]

    mutated = graph.model_dump()
    mutated["unexpected_key"] = "forbidden"
    with pytest.raises(ValidationError):
        RequirementGraph.model_validate(mutated)


@settings(max_examples=25, deadline=None)
@given(
    sha=hex64_st,
    req_id=non_blank_st,
    adr_id=non_blank_st,
    decision=st.sampled_from(list(RoutingDecisionKind)),
    title=non_blank_st,
    rationale=non_blank_st,
)
def test_hypothesis_routing_adr_roundtrip_and_extra_forbid(
    sha: str,
    req_id: str,
    adr_id: str,
    decision: RoutingDecisionKind,
    title: str,
    rationale: str,
) -> None:
    chain = TraceabilityChain(
        source_sha256=sha,
        req_id=req_id,
        adr_id=adr_id,
        alloy_model_ref="models/spec.als#Inv",
        pbt_spec_id="PBT-001",
    )
    adr = RoutingADR(
        adr_id=adr_id,
        title=title,
        decision=decision,
        rationale=rationale,
        req_ids=[req_id],
        traceability=[chain],
    )
    restored = RoutingADR.model_validate_json(adr.model_dump_json())
    assert restored == adr
    assert chain.is_complete() is True

    bad_payload = adr.model_dump()
    bad_payload["extra_field"] = 123
    with pytest.raises(ValidationError):
        RoutingADR.model_validate(bad_payload)


@settings(max_examples=25, deadline=None)
@given(
    param=py_ident_st,
    model_name=py_ident_st,
    field_name=py_ident_st,
    req_id=non_blank_st,
)
def test_hypothesis_pbt_spec_roundtrip_and_extra_forbid(
    param: str,
    model_name: str,
    field_name: str,
    req_id: str,
) -> None:
    spec = PBTSpec(
        pbt_spec_id="PBT-100",
        target_module="reqhq.schemas.requirements",
        req_ids=[req_id],
        invariants=[
            PropertyInvariant(
                invariant_id="INV-1",
                req_id=req_id,
                title="Non-negative length",
                assertion_expr=f"len(str({param})) >= 0",
                strategies=[
                    HypothesisStrategySpec(
                        parameter_name=param,
                        strategy_expr="st.integers(min_value=0, max_value=10)",
                    )
                ],
            )
        ],
        guardrails=[
            PydanticGuardrail(
                guardrail_id="GR-1",
                model_name=model_name,
                field_name=field_name,
                constraint_type="extra_forbid",
                rule="ConfigDict(extra='forbid')",
            )
        ],
    )
    restored = PBTSpec.model_validate_json(spec.model_dump_json())
    assert restored == spec
    compile(restored.render_hypothesis_stub(), "<pbt_hypothesis_stub>", "exec")

    bad = spec.model_dump()
    bad["bogus"] = True
    with pytest.raises(ValidationError):
        PBTSpec.model_validate(bad)


# ---------------------------------------------------------------------------
# Detailed schema validation & edge-case tests
# ---------------------------------------------------------------------------


def test_requirements_schema_validations_and_cycles() -> None:
    link = TraceLink(source_sha256=SAMPLE_SHA_UPPER, source_uri="spec.md")
    assert link.source_sha256 == SAMPLE_SHA_UPPER.lower()

    with pytest.raises(ValidationError):
        TraceLink(source_sha256="z" * 64, source_uri="spec.md")
    with pytest.raises(ValidationError):
        TraceLink(source_sha256=SAMPLE_SHA, source_uri="   ")
    with pytest.raises(ValidationError):
        RequirementNode(req_id="   ", title="T", statement="S")
    with pytest.raises(ValidationError):
        RequirementEdge(source_id="REQ-1", target_id="REQ-1")
    with pytest.raises(ValidationError):
        RequirementEdge(source_id=" REQ-1 ", target_id="REQ-1")
    with pytest.raises(ValidationError):
        RequirementEdge(source_id="   ", target_id="REQ-1")
    with pytest.raises(ValidationError):
        RequirementGraph(graph_id="   ")
    with pytest.raises(ValidationError):
        RequirementGraph(graph_id="G1", version="   ")

    n1 = RequirementNode(
        req_id="REQ-1",
        title="Auth",
        statement="Authenticate user",
        status=RequirementStatus.ACCEPTED,
        trace_links=[link],
    )
    n2 = RequirementNode(
        req_id="REQ-2",
        title="Audit",
        statement="Log auth events",
        status=RequirementStatus.DRAFT,
    )
    n3 = RequirementNode(
        req_id="REQ-3",
        title="Legacy",
        statement="Old flow",
        status=RequirementStatus.DEPRECATED,
    )

    # Duplicate node IDs rejected
    with pytest.raises(ValidationError, match="Duplicate requirement ID"):
        RequirementGraph(graph_id="G1", nodes=[n1, n1])

    # Dangling edge rejected on validation
    with pytest.raises(ValidationError, match="not found in graph nodes"):
        RequirementGraph(
            graph_id="G1",
            nodes=[n1],
            edges=[RequirementEdge(source_id="REQ-1", target_id="REQ-999")],
        )

    # Corrupt graph constructed without validation caught by validate_reference_integrity
    corrupt = RequirementGraph.model_construct(
        graph_id="G-bad",
        version="1.0.0",
        nodes=[n1],
        edges=[RequirementEdge.model_construct(source_id="REQ-X", target_id="REQ-Y", relation=EdgeKind.DEPENDS_ON, rationale="")],
    )
    assert len(corrupt.validate_reference_integrity()) == 2

    # Cycle detection on DEPENDS_ON
    cyclic = RequirementGraph(
        graph_id="G-cyc",
        nodes=[n1, n2, n3],
        edges=[
            RequirementEdge(source_id="REQ-1", target_id="REQ-2", relation=EdgeKind.DEPENDS_ON),
            RequirementEdge(source_id="REQ-2", target_id="REQ-1", relation=EdgeKind.DEPENDS_ON),
        ],
    )
    assert cyclic.is_dag() is False
    assert len(cyclic.find_cycles()) >= 1
    assert len(cyclic.validate_dag()) >= 1
    with pytest.raises(ValueError, match="cycle"):
        cyclic.topological_order()

    # CONFLICTS_WITH is exempt from dependency DAG cycle check
    conflict_graph = RequirementGraph(
        graph_id="G-conf",
        nodes=[n1, n2, n3],
        edges=[
            RequirementEdge(source_id="REQ-1", target_id="REQ-2", relation=EdgeKind.CONFLICTS_WITH),
            RequirementEdge(source_id="REQ-2", target_id="REQ-1", relation=EdgeKind.CONFLICTS_WITH),
        ],
    )
    assert conflict_graph.is_dag() is True
    assert conflict_graph.get_node("REQ-1") == n1
    assert conflict_graph.get_node("REQ-MISSING") is None

    # Traceability checks: REQ-2 (DRAFT) fails require_all=True, passes require_all=False; REQ-3 (DEPRECATED) skipped
    assert len(conflict_graph.validate_traceability(require_all=True)) == 1
    assert conflict_graph.validate_traceability(require_all=False) == []
    report = conflict_graph.validate_all(require_all_traceability=True)
    assert report["reference_integrity"] == []
    assert report["dag"] == []
    assert len(report["traceability"]) == 1


def test_routing_adr_validations() -> None:
    chain = TraceabilityChain(source_sha256=SAMPLE_SHA, req_id="REQ-1", adr_id="0007")
    assert chain.is_complete() is False
    assert "<unlinked>" in chain.chain_summary()

    with pytest.raises(ValidationError):
        TraceabilityChain(source_sha256="bad", req_id="REQ-1", adr_id="0007")
    with pytest.raises(ValidationError, match="64-character hex"):
        TraceabilityChain(source_sha256="z" * 64, req_id="REQ-1", adr_id="0007")
    with pytest.raises(ValidationError):
        TraceabilityChain(source_sha256=SAMPLE_SHA, req_id="  ", adr_id="0007")
    blank_opt_chain = TraceabilityChain(
        source_sha256=SAMPLE_SHA,
        req_id="REQ-1",
        adr_id="0007",
        alloy_model_ref="   ",
        pbt_spec_id="   ",
    )
    assert blank_opt_chain.alloy_model_ref is None
    assert blank_opt_chain.pbt_spec_id is None
    assert blank_opt_chain.is_complete() is False

    hybrid = RoutingADR(
        adr_id="0007",
        title="Hybrid Rollout",
        decision=RoutingDecisionKind.HYBRID,
        rationale="Combine automation with operator runbook",
        req_ids=["REQ-1"],
        traceability=[chain],
        component_breakdown={
            RoutingDecisionKind.SOFTWARE: "Automated API gateway",
            RoutingDecisionKind.PROCESS: "Change approval SOP",
        },
    )
    assert hybrid.decision == RoutingDecisionKind.HYBRID

    with pytest.raises(ValidationError, match="not Hybrid"):
        RoutingADR(
            adr_id="0007",
            title="Invalid Breakdown",
            decision=RoutingDecisionKind.HYBRID,
            rationale="Reason",
            req_ids=["REQ-1"],
            component_breakdown={RoutingDecisionKind.HYBRID: "Self"},
        )
    with pytest.raises(ValidationError, match="at least 2"):
        RoutingADR(
            adr_id="0007",
            title="Too Few Tracks",
            decision=RoutingDecisionKind.HYBRID,
            rationale="Reason",
            req_ids=["REQ-1"],
            component_breakdown={RoutingDecisionKind.SOFTWARE: "Only one"},
        )
    with pytest.raises(ValidationError, match="does not match"):
        RoutingADR(
            adr_id="0008",
            title="Wrong ADR Link",
            decision=RoutingDecisionKind.SOFTWARE,
            rationale="Reason",
            req_ids=["REQ-1"],
            traceability=[chain],
        )
    with pytest.raises(ValidationError, match="not listed in RoutingADR.req_ids"):
        RoutingADR(
            adr_id="0007",
            title="Unknown Req Link",
            decision=RoutingDecisionKind.SOFTWARE,
            rationale="Reason",
            req_ids=["REQ-2"],
            traceability=[chain],
        )
    with pytest.raises(ValidationError, match="blank"):
        RoutingADR(
            adr_id="0007",
            title="   ",
            decision=RoutingDecisionKind.PROCESS,
            rationale="Reason",
            req_ids=["REQ-1"],
        )
    with pytest.raises(ValidationError, match="blank"):
        RoutingADR(
            adr_id="0007",
            title="Valid Title",
            decision=RoutingDecisionKind.PROCESS,
            rationale="Reason",
            req_ids=["   "],
        )


def test_pbt_spec_validations_and_stub_generation() -> None:
    with pytest.raises(ValidationError, match="Python identifier"):
        HypothesisStrategySpec(parameter_name="123bad", strategy_expr="st.integers()")
    with pytest.raises(ValidationError, match="non-keyword Python identifier"):
        HypothesisStrategySpec(parameter_name="class", strategy_expr="st.integers()")
    with pytest.raises(ValidationError, match="blank"):
        HypothesisStrategySpec(parameter_name="x", strategy_expr="   ")
    with pytest.raises(ValidationError, match="blank"):
        PropertyInvariant(invariant_id="I1", req_id="R1", title="  ", assertion_expr="True")
    with pytest.raises(ValidationError, match="Duplicate strategy parameter_name"):
        PropertyInvariant(
            invariant_id="I1",
            req_id="R1",
            title="Dup",
            assertion_expr="x >= 0",
            strategies=[
                HypothesisStrategySpec(parameter_name="x", strategy_expr="st.integers()"),
                HypothesisStrategySpec(parameter_name="x", strategy_expr="st.integers()"),
            ],
        )
    with pytest.raises(ValidationError, match="Python identifier"):
        PydanticGuardrail(
            guardrail_id="G1",
            model_name="Not Valid",
            field_name="ok",
            constraint_type="type",
            rule="int",
        )
    with pytest.raises(ValidationError, match="blank"):
        PydanticGuardrail(
            guardrail_id="   ",
            model_name="Model",
            field_name="ok",
            constraint_type="type",
            rule="int",
        )
    with pytest.raises(ValidationError, match="blank"):
        PBTSpec(pbt_spec_id="   ", target_module="m", req_ids=["REQ-1"])
    with pytest.raises(ValidationError, match="blank"):
        PBTSpec(pbt_spec_id="PBT-1", target_module="m", req_ids=["   "])

    inv = PropertyInvariant(
        invariant_id="INV-1",
        req_id="REQ-1",
        title="Positive balance",
        precondition="amount >= 0",
        assertion_expr="amount >= 0",
        strategies=[HypothesisStrategySpec(parameter_name="amount", strategy_expr="st.integers(min_value=0)")],
        max_examples=50,
    )
    gr = PydanticGuardrail(
        guardrail_id="GR-1",
        model_name="Account",
        field_name="balance",
        constraint_type="range",
        rule="ge=0",
    )
    spec = PBTSpec(
        pbt_spec_id="PBT-01",
        target_module="billing.account",
        req_ids=["REQ-1"],
        invariants=[inv],
        guardrails=[gr],
    )
    stub = spec.render_hypothesis_stub()
    compile(stub, "<pbt_stub>", "exec")
    assert "test_inv_1" in stub
    assert "@given(amount=st.integers(min_value=0))" in stub

    with pytest.raises(ValidationError, match="Duplicate invariant_id"):
        PBTSpec(pbt_spec_id="P", target_module="m", req_ids=["REQ-1"], invariants=[inv, inv])
    with pytest.raises(ValidationError, match="Duplicate guardrail_id"):
        PBTSpec(pbt_spec_id="P", target_module="m", req_ids=["REQ-1"], guardrails=[gr, gr])
    with pytest.raises(ValidationError, match="not in PBTSpec.req_ids"):
        PBTSpec(pbt_spec_id="P", target_module="m", req_ids=["REQ-2"], invariants=[inv])


def test_system_context_greenfield_and_brownfield_extension() -> None:
    ctx = GreenfieldEmptyContext(project_name="ReqHQ", notes="Phase 1 greenfield")
    assert isinstance(ctx, SystemContext)
    assert isinstance(ctx, SystemContextBase)
    assert ctx.mode == "greenfield"
    assert ctx.is_empty() is True
    prompt_text = ctx.to_prompt_context()
    assert "greenfield" in prompt_text.lower()
    assert "ReqHQ" in prompt_text
    assert "Phase 1 greenfield" in prompt_text
    meta = ctx.summary_metadata()
    assert meta["mode"] == "greenfield"
    assert meta["is_empty"] is True
    assert "architecture_md" in meta["phase2_hooks"]

    with pytest.raises(ValidationError):
        GreenfieldEmptyContext(unexpected="x")  # type: ignore[call-arg]

    # Prove Phase 2 brownfield subclassing seam works cleanly
    class _Phase2BrownfieldStub(SystemContextBase):
        mode: str = "brownfield"
        artifacts: list[BrownfieldArtifactRef]

        def is_empty(self) -> bool:
            return len(self.artifacts) == 0

        def to_prompt_context(self) -> str:
            return f"[SystemContext: brownfield ({len(self.artifacts)} artifacts)]"

        def summary_metadata(self) -> dict[str, Any]:
            return {"mode": self.mode, "is_empty": self.is_empty(), "count": len(self.artifacts)}

    bf = _Phase2BrownfieldStub(
        artifacts=[
            BrownfieldArtifactRef(
                artifact_kind="architecture_md",
                path="ARCHITECTURE.md",
                sha256=SAMPLE_SHA_UPPER,
            )
        ]
    )
    assert isinstance(bf, SystemContext)
    assert bf.is_empty() is False
    assert bf.artifacts[0].sha256 == SAMPLE_SHA_UPPER.lower()
    assert "brownfield" in bf.to_prompt_context()

    with pytest.raises(ValidationError, match="blank"):
        BrownfieldArtifactRef(artifact_kind="architecture_md", path="   ")
    with pytest.raises(ValidationError, match="64-character hex"):
        BrownfieldArtifactRef(
            artifact_kind="architecture_md",
            path="ARCHITECTURE.md",
            sha256="not-a-valid-sha",
        )


# ---------------------------------------------------------------------------
# SessionEventLog tests
# ---------------------------------------------------------------------------


def test_session_event_log_lifecycle_revise_and_resume(tmp_path: Path) -> None:
    log = SessionEventLog(tmp_path / "sessions")
    assert log.list_sessions() == []

    # Invalid session IDs rejected
    for bad_id in ("", "../escape", "a/b", "bad\\id"):
        with pytest.raises(ValueError, match="Invalid session_id"):
            log.create_session(session_id=bad_id)

    # Missing session raises FileNotFoundError
    with pytest.raises(FileNotFoundError):
        log.load_events("does-not-exist")

    auto_id = log.create_session(metadata={"project": "auto"})
    assert auto_id.startswith("sess-")

    sid = log.create_session(session_id="proj-alpha", metadata={"strict_local": True})
    assert log.list_sessions() == sorted([auto_id, "proj-alpha"])

    with pytest.raises(FileExistsError):
        log.create_session(session_id="proj-alpha")

    # Append ingestion, 3 turns, escalation, verification, export
    log.append_event(sid, EventKind.SOURCE_INGESTED, {"sha256": SAMPLE_SHA, "uri": "prd.md"})
    log.append_event(sid, EventKind.TURN_COMPLETED, {"draft": "t1-v1"}, turn_id="turn-1")
    log.append_event(sid, EventKind.TURN_COMPLETED, {"draft": "t2-v1"}, turn_id="turn-2")
    log.append_event(sid, EventKind.VERIFICATION_RECORDED, {"passed": True}, turn_id="turn-2")
    log.append_event(sid, EventKind.TURN_COMPLETED, {"draft": "t3-v1"}, turn_id="turn-3")
    log.append_event(sid, EventKind.ESCALATION_TRIGGERED, {"reason": "FINAL_AUDIT"})
    log.append_event(sid, EventKind.EXPORT_COMPLETED, {"format": "json"})

    assert log.verify_jsonl_parity(sid) is True
    assert len(log.load_events(sid)) == 8
    assert len(log.get_active_events(sid)) == 8

    # Revise turn-1 -> turn-2 and turn-3 must be invalidated; append-only history preserved!
    rev_result = log.revise(sid, "turn-1", {"draft": "t1-v2", "note": "clarified SLA"})
    assert rev_result["revised_turn_id"] == "turn-1"
    assert rev_result["invalidated_turn_ids"] == ["turn-2", "turn-3"]

    all_events = log.load_events(sid)
    assert len(all_events) == 11  # 8 original + 1 TURN_REVISED + 2 TURN_INVALIDATED
    assert all_events[2].kind == EventKind.TURN_COMPLETED
    assert all_events[2].payload == {"draft": "t1-v1"}  # Original untouched!
    assert all_events[8].kind == EventKind.TURN_REVISED
    assert all_events[8].payload == {"draft": "t1-v2", "note": "clarified SLA"}
    assert all_events[9].kind == EventKind.TURN_INVALIDATED
    assert all_events[9].turn_id == "turn-2"
    assert all_events[10].kind == EventKind.TURN_INVALIDATED
    assert all_events[10].turn_id == "turn-3"
    assert log.verify_jsonl_parity(sid) is True

    active = log.get_active_events(sid)
    active_turn_ids = [e.turn_id for e in active if e.turn_id is not None]
    assert active_turn_ids == ["turn-1"]

    resumed = log.resume(sid)
    assert resumed["session_id"] == sid
    assert resumed["total_events"] == 11
    assert resumed["active_turns"] == ["turn-1"]
    assert resumed["invalidated_turns"] == ["turn-2", "turn-3"]
    assert resumed["last_turn_id"] == "turn-1"
    assert resumed["metadata"] == {"strict_local": True}
    assert len(resumed["sources"]) == 1
    assert resumed["verifications"] == []  # turn-2 verification was invalidated!

    # Re-executing invalidated turn-2 restores it to active turns
    log.append_event(sid, EventKind.TURN_COMPLETED, {"draft": "t2-v2"}, turn_id="turn-2")
    resumed2 = log.resume(sid)
    assert resumed2["active_turns"] == ["turn-1", "turn-2"]
    assert resumed2["invalidated_turns"] == ["turn-3"]

    # Blank turn_id rejected
    with pytest.raises(ValueError, match="turn_id"):
        log.append_event(sid, EventKind.TURN_COMPLETED, {"draft": "bad"}, turn_id="   ")

    # Revising an invalidated or unknown turn raises ValueError
    with pytest.raises(ValueError, match="not an active turn"):
        log.revise(sid, "turn-3", {"draft": "nope"})

    # Directly appending TURN_REVISED for an invalidated turn restores it cleanly
    log.append_event(sid, EventKind.TURN_REVISED, {"draft": "t3-restored"}, turn_id="turn-3")
    resumed3 = log.resume(sid)
    assert resumed3["active_turns"] == ["turn-1", "turn-2", "turn-3"]
    assert resumed3["invalidated_turns"] == []

    # Revising the latest active turn invalidates 0 downstream turns
    rev_latest = log.revise(sid, "turn-3", {"draft": "t3-v2"})
    assert rev_latest["invalidated_turn_ids"] == []

    # SessionEvent enforces extra="forbid"
    with pytest.raises(ValidationError):
        SessionEvent(
            seq=1,
            event_id="e1",
            session_id=sid,
            timestamp="2026-01-01T00:00:00Z",
            kind=EventKind.SESSION_INIT,
            extra_bad=True,  # type: ignore[call-arg]
        )

    # Missing JSONL detected by verify_jsonl_parity
    (tmp_path / "sessions" / sid / "events.jsonl").unlink()
    assert log.verify_jsonl_parity(sid) is False


# ---------------------------------------------------------------------------
# PromptCache tests
# ---------------------------------------------------------------------------


def test_prompt_cache_deterministic_keys_and_persistence(tmp_path: Path) -> None:
    db_path = tmp_path / "cache" / "prompt_cache.db"
    cache = PromptCache(db_path)

    k1 = PromptCache.compute_key("Extract reqs", "qwen3:8b", "RequirementGraph", "You are strict.")
    k2 = PromptCache.compute_key("Extract reqs", "qwen3:8b", "RequirementGraph", "You are strict.")
    k3 = PromptCache.compute_key("Extract reqs", "qwen3:14b", "RequirementGraph", "You are strict.")
    k4 = PromptCache.compute_key("Extract reqs", "qwen3:8b", None, "You are strict.")
    assert k1 == k2
    assert len(k1) == 64
    assert k1 != k3
    assert k1 != k4

    assert cache.get(k1) is None
    assert cache.stats() == {"entries": 0, "hits": 0, "misses": 1}

    cache.put(k1, {"nodes": ["REQ-1"]}, model="qwen3:8b", metadata={"latency_ms": 120})
    assert cache.get(k1) == {"nodes": ["REQ-1"]}
    assert cache.stats() == {"entries": 1, "hits": 1, "misses": 1}

    # Upsert replaces stored payload
    cache.put(k1, {"nodes": ["REQ-1", "REQ-2"]}, model="qwen3:8b")
    reopened = PromptCache(db_path)
    assert reopened.get(k1) == {"nodes": ["REQ-1", "REQ-2"]}
    assert reopened.stats() == {"entries": 1, "hits": 2, "misses": 1}

    deleted = reopened.clear()
    assert deleted == 1
    assert reopened.stats()["entries"] == 0
