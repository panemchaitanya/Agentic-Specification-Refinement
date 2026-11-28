"""Comprehensive offline tests for Stage 3 Elicitation, 5 Critics & Delta Convergence Engine."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import ValidationError
import pytest

from reqhq.elicitation import (
    AssumptionRecord,
    ConvergenceGateError,
    CriticDisagreement,
    CriticDomain,
    CriticFinding,
    CriticPanel,
    DeltaDependencyTracker,
    ElicitationEngine,
    ElicitationQuestion,
    ElicitationState,
    FindingSeverity,
    FunctionalCompletenessCritic,
    LogicalConsistencyCritic,
    MAX_CRITIC_PROMPT_CHARS,
    MAX_DECOMPOSE_PROMPT_CHARS,
    NonFunctionalQualityCritic,
    ProcessOperationalCritic,
    RequirementDecomposer,
    TrainingAdoptionCritic,
    UserAnswer,
    cluster_and_rank_questions,
    deduplicate_findings,
    is_converged,
)
from reqhq.ingestion.chunker import HierarchicalChunker
from reqhq.ingestion.normalizer import NormalizedDocument
from reqhq.llm import (
    EscalationReason,
    LLMRequest,
    MockProvider,
    ProviderError,
    ProviderRouter,
)
from reqhq.schemas.requirements import (
    EdgeKind,
    RequirementEdge,
    RequirementGraph,
    RequirementKind,
    RequirementNode,
    RequirementStatus,
    TraceLink,
)
from reqhq.store.event_log import EventKind, SessionEventLog

_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _make_node(
    req_id: str,
    title: str,
    statement: str,
    *,
    kind: RequirementKind = RequirementKind.FUNCTIONAL,
    priority: str = "P1",
    ac: list[str] | None = None,
    sha: str = _SHA_A,
    status: RequirementStatus = RequirementStatus.DRAFT,
) -> RequirementNode:
    return RequirementNode(
        req_id=req_id,
        title=title,
        statement=statement,
        kind=kind,
        status=status,
        priority=priority,  # type: ignore[arg-type]
        acceptance_criteria=list(ac or []),
        trace_links=[
            TraceLink(
                source_sha256=sha,
                source_uri="cli://test",
                anchor="char:0-20",
                excerpt=statement[:40],
            )
        ],
    )


def _make_finding(
    fid: str,
    req_id: str,
    critic: CriticDomain,
    severity: FindingSeverity,
    title: str = "Gap",
    description: str = "Missing detail",
    question: str = "What is the rule?",
    default: str = "Use safe default",
    resolved: bool = False,
    resolved_by_turn: str | None = None,
) -> CriticFinding:
    return CriticFinding(
        finding_id=fid,
        req_id=req_id,
        critic=critic,
        severity=severity,
        title=title,
        description=description,
        suggested_question=question,
        recommended_default=default,
        resolved=resolved,
        resolved_by_turn=resolved_by_turn,
        source_sha256=_SHA_A,
    )


# ---------------------------------------------------------------------------
# 1. Strict Pydantic v2 schema contract tests
# ---------------------------------------------------------------------------


def test_models_forbid_extra_fields_and_validate_invariants() -> None:
    with pytest.raises(ValidationError):
        CriticFinding(
            finding_id="F1",
            req_id="REQ-001",
            critic=CriticDomain.FUNCTIONAL_COMPLETENESS,
            severity=FindingSeverity.BLOCKER,
            title="Missing AC",
            description="No AC",
            suggested_question="Add AC?",
            unexpected="boom",  # type: ignore[call-arg]
        )

    with pytest.raises(ValidationError):
        CriticFinding(
            finding_id="F1",
            req_id="REQ-001",
            critic=CriticDomain.FUNCTIONAL_COMPLETENESS,
            severity=FindingSeverity.BLOCKER,
            title="   ",
            description="No AC",
            suggested_question="Add AC?",
        )

    with pytest.raises(ValidationError):
        CriticFinding(
            finding_id="F1",
            req_id="REQ-001",
            critic=CriticDomain.FUNCTIONAL_COMPLETENESS,
            severity=FindingSeverity.BLOCKER,
            title="Title",
            description="No AC",
            suggested_question="Add AC?",
            source_sha256="not-a-sha",
        )

    with pytest.raises(ValidationError):
        AssumptionRecord(
            assumption_id="ADR-A001",
            title="Default SLA",
            statement="Assume 500ms p95",
            req_ids=["REQ-001"],
            source_sha256=_SHA_A,
            rationale="Default accepted",
            user_confirmed=True,  # type: ignore[arg-type]
        )

    with pytest.raises(ValidationError):
        AssumptionRecord(
            assumption_id="ADR-A001",
            title="Default SLA",
            statement="Assume 500ms p95",
            req_ids=["REQ-001"],
            source_sha256="1234",
            rationale="Default accepted",
        )

    with pytest.raises(ValidationError):
        ElicitationQuestion(
            question_id="Q-001",
            prompt="What SLA?",
            severity=FindingSeverity.MAJOR,
            info_gain_score=1.5,
        )

    with pytest.raises(ValidationError):
        UserAnswer(
            question_id="Q-001",
            answer="   ",
            turn_id="turn-001",
            source="user_turn_1",
        )

    with pytest.raises(ValidationError):
        CriticDisagreement(
            req_id="REQ-001",
            critics=[CriticDomain.FUNCTIONAL_COMPLETENESS],
            summary="Only one critic",
        )

    with pytest.raises(ValidationError):
        ElicitationState(
            session_id="   ",
            graph=RequirementGraph(graph_id="g1"),
        )


# ---------------------------------------------------------------------------
# 2. RequirementDecomposer: 1-liner prompt & multi-chunk PRD + GlobalGlossary
# ---------------------------------------------------------------------------


def test_decompose_prompt_structured_json_and_verbatim_trace_links() -> None:
    prompt = (
        "Build a dual-approval invoice payment portal with immutable audit logs "
        "and finance staff onboarding training."
    )
    expected_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    scripted_json = json.dumps(
        {
            "requirements": [
                {
                    "req_id": "R1",
                    "title": "Dual Approval Payment",
                    "statement": "Invoices above $10k require two finance approvers before payment.",
                    "kind": "security",
                    "priority": "P0",
                    "acceptance_criteria": ["Two distinct finance approver IDs recorded."],
                    "excerpt": "dual-approval invoice payment portal",
                    "tags": ["finance"],
                    "rationale": "Prevents unauthorized transfers.",
                },
                {
                    "req_id": "R2",
                    "title": "Immutable Audit Log",
                    "statement": "Every payment approval event must be persisted to an immutable audit log.",
                    "kind": "data",
                    "priority": "P0",
                    "acceptance_criteria": [],
                    "excerpt": "immutable audit logs",
                    "tags": ["audit"],
                    "rationale": "Required for compliance.",
                },
            ],
            "edges": [
                {
                    "source_id": "R2",
                    "target_id": "R1",
                    "relation": "depends_on",
                    "rationale": "Audit logs capture payment approvals.",
                },
                # Cyclic edge back to R2 should be rejected automatically to preserve DAG.
                {
                    "source_id": "R1",
                    "target_id": "R2",
                    "relation": "depends_on",
                    "rationale": "Cycle should be dropped.",
                },
            ],
        }
    )

    local = MockProvider("mock-local", responses=[scripted_json])
    router = ProviderRouter(local, strict_local=True, enable_cache=False)
    decomposer = RequirementDecomposer(router)

    graph = decomposer.decompose_prompt(prompt, session_id="sess-p1")
    assert graph.graph_id == "sess-p1-graph"
    assert [n.req_id for n in graph.nodes] == ["REQ-001", "REQ-002"]
    assert graph.is_dag()
    assert len(graph.edges) == 1
    assert graph.edges[0].source_id == "REQ-002"
    assert graph.edges[0].target_id == "REQ-001"
    for node in graph.nodes:
        assert len(node.trace_links) == 1
        link = node.trace_links[0]
        assert link.source_sha256 == expected_sha
        assert link.source_uri == "cli://prompt"
        assert link.excerpt in prompt
    assert len(local.calls) == 1
    assert len(local.calls[0].prompt) <= MAX_DECOMPOSE_PROMPT_CHARS


def test_decompose_prompt_plain_text_and_error_deterministic_fallback() -> None:
    prompt = (
        "Build a dual-approval invoice system with immutable audit logs "
        "plus role-based staff onboarding training."
    )
    local = MockProvider("mock-local", default_response="Plain text response without JSON.")
    router = ProviderRouter(local, strict_local=True, max_local_retries=0, enable_cache=False)
    decomposer = RequirementDecomposer(router)

    graph = decomposer.decompose_prompt(prompt, session_id="sess-fallback")
    assert len(graph.nodes) >= 2
    assert graph.is_dag()
    expected_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    for node in graph.nodes:
        assert node.trace_links[0].source_sha256 == expected_sha
        assert node.trace_links[0].excerpt in prompt

    # ProviderError also gracefully falls back to deterministic decomposition.
    err_local = MockProvider("mock-err", responses=[ProviderError("down", provider="mock-err")])
    err_router = ProviderRouter(err_local, strict_local=True, max_local_retries=0, enable_cache=False)
    graph2 = RequirementDecomposer(err_router).decompose_prompt(
        "Support offline CLI export with p95 latency under 200ms.",
        session_id="sess-err",
    )
    assert len(graph2.nodes) >= 1
    assert graph2.nodes[0].req_id == "REQ-001"

    with pytest.raises(ValueError):
        decomposer.decompose_prompt("   ", session_id="sess-blank")


def test_decompose_chunks_prepends_global_glossary_and_preserves_chunk_traceability() -> None:
    markdown = (
        "[section:glossary]\n"
        "# Glossary\n"
        "- **PaymentGateway**: External settlement service used for ACH transfers.\n"
        "Service Level Agreement (SLA) targets apply to all payment flows.\n\n"
        "[section:payments]\n"
        "# Payment Processing\n"
        "The PaymentGateway shall enforce dual approval for transfers exceeding $5,000.\n"
        "Every settlement attempt must be logged with cryptographic integrity.\n\n"
        "[section:operations]\n"
        "# Operational Training\n"
        "Finance operators must complete role certification training before production access.\n"
        "Incident escalation runbooks must define a 4-hour response SLA.\n"
    )
    doc_sha = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    doc = NormalizedDocument(
        source_path=Path("prd.md"),
        source_sha256=doc_sha,
        media_type="text/markdown",
        parser_name="markdown-passthrough",
        markdown=markdown,
        char_count=len(markdown),
        anchors=[],
    )
    chunker = HierarchicalChunker(max_chunk_chars=260, overlap_chars=20)
    chunking_result = chunker.chunk(doc)
    assert chunking_result.chunk_count >= 3
    assert "SLA" in chunking_result.glossary.acronyms
    assert "PaymentGateway" in chunking_result.glossary.defined_terms

    def _chunk_handler(req: LLMRequest) -> str:
        chunk_id = req.metadata.get("chunk_id", "chunk")
        return json.dumps(
            {
                "requirements": [
                    {
                        "req_id": f"local-{chunk_id}",
                        "title": f"Requirement from {chunk_id}",
                        "statement": f"System enforces rule extracted from {chunk_id} using PaymentGateway.",
                        "kind": "functional",
                        "priority": "P1",
                        "acceptance_criteria": [],
                        "excerpt": "PaymentGateway",
                        "tags": [chunk_id],
                        "rationale": "Chunk extraction.",
                    }
                ],
                "edges": [],
            }
        )

    local = MockProvider(
        "mock-local",
        responses=[_chunk_handler] * chunking_result.chunk_count,
    )
    router = ProviderRouter(local, strict_local=True, enable_cache=False)
    decomposer = RequirementDecomposer(router)

    graph = decomposer.decompose_chunks(
        chunking_result,
        session_id="sess-prd",
        source_uri="file://prd.md",
    )

    assert len(local.calls) == chunking_result.chunk_count
    assert len(graph.nodes) == chunking_result.chunk_count
    for call in local.calls:
        assert "## Global Glossary" in call.prompt
        assert "SLA: Service Level Agreement" in call.prompt
        assert "PaymentGateway" in call.prompt
        assert len(call.prompt) <= MAX_DECOMPOSE_PROMPT_CHARS

    chunk_ids_seen = set()
    for node in graph.nodes:
        link = node.trace_links[0]
        assert link.source_sha256 == doc_sha
        assert link.source_uri == "file://prd.md"
        assert link.chunk_id is not None
        assert link.anchor != ""
        chunk_ids_seen.add(link.chunk_id)
    assert len(chunk_ids_seen) == chunking_result.chunk_count

    # Deterministic fallback when chunk LLM returns plain text
    plain_local = MockProvider("mock-plain", default_response="non-json")
    plain_router = ProviderRouter(plain_local, strict_local=True, max_local_retries=0, enable_cache=False)
    fallback_graph = RequirementDecomposer(plain_router).decompose_chunks(
        chunking_result,
        session_id="sess-prd-fb",
    )
    assert len(fallback_graph.nodes) >= 3
    assert all(n.trace_links[0].chunk_id is not None for n in fallback_graph.nodes)

    # Empty chunking result returns empty graph with 0 LLM calls.
    empty_doc = NormalizedDocument(
        source_path=Path("empty.md"),
        source_sha256=_SHA_B,
        media_type="text/markdown",
        parser_name="markdown-passthrough",
        markdown="",
        char_count=0,
        anchors=[],
    )
    empty_chunks = chunker.chunk(empty_doc)
    zero_local = MockProvider("mock-zero")
    empty_graph = RequirementDecomposer(
        ProviderRouter(zero_local, strict_local=True)
    ).decompose_chunks(empty_chunks, session_id="sess-empty")
    assert empty_graph.nodes == []
    assert len(zero_local.calls) == 0


# ---------------------------------------------------------------------------
# 3. All 5 sequential specialist critics & inter-critic disagreement escalation
# ---------------------------------------------------------------------------


def test_five_sequential_critics_order_and_deterministic_fallback() -> None:
    graph = RequirementGraph(
        graph_id="g-critics",
        nodes=[
            _make_node(
                "REQ-001",
                "Invoice Approval Workflow",
                "Finance staff approve invoices manually before payment execution.",
                kind=RequirementKind.PROCESS,
                priority="P0",
            ),
            _make_node(
                "REQ-002",
                "Automated Payment Dispatch",
                "System dispatches payments automatically upon invoice receipt.",
                kind=RequirementKind.FUNCTIONAL,
                priority="P1",
            ),
        ],
        edges=[
            RequirementEdge(
                source_id="REQ-001",
                target_id="REQ-002",
                relation=EdgeKind.CONFLICTS_WITH,
                rationale="Manual approval vs instant dispatch.",
            )
        ],
    )

    # Default non-schema response triggers deterministic specialist fallbacks for all 5 critics.
    local = MockProvider("mock-local", default_response="unstructured text")
    router = ProviderRouter(local, strict_local=True, max_local_retries=0, enable_cache=False)
    panel = CriticPanel(router)

    findings, _ = panel.run_on_nodes(graph)

    critic_calls = [c for c in local.calls if c.metadata.get("stage") == "critic"]
    assert [c.metadata["critic"] for c in critic_calls] == [
        CriticDomain.FUNCTIONAL_COMPLETENESS.value,
        CriticDomain.NON_FUNCTIONAL_QUALITY.value,
        CriticDomain.LOGICAL_CONSISTENCY.value,
        CriticDomain.PROCESS_OPERATIONAL.value,
        CriticDomain.TRAINING_ADOPTION.value,
    ]
    domains_found = {f.critic for f in findings}
    assert domains_found == set(CriticDomain)
    assert all(len(c.prompt) <= MAX_CRITIC_PROMPT_CHARS for c in critic_calls)

    # Passing an empty target_req_ids makes zero LLM calls.
    calls_before = len(local.calls)
    empty_findings, empty_disagreements = panel.run_on_nodes(graph, target_req_ids=[])
    assert empty_findings == []
    assert empty_disagreements == []
    assert len(local.calls) == calls_before


def test_inter_critic_disagreement_triggers_escalation_reason_and_event_log(tmp_path: Path) -> None:
    graph = RequirementGraph(
        graph_id="g-conflict",
        nodes=[
            _make_node(
                "REQ-001",
                "Wire Transfer Authorization",
                "Wire transfers over $25k execute via approval flow.",
                priority="P0",
            )
        ],
    )

    def _local_handler(req: LLMRequest) -> str:
        stage = req.metadata.get("stage")
        critic = req.metadata.get("critic")
        if stage == "critic" and critic == CriticDomain.FUNCTIONAL_COMPLETENESS.value:
            return json.dumps(
                {
                    "findings": [
                        {
                            "req_id": "REQ-001",
                            "severity": "blocker",
                            "title": "Require automated instant transfer execution",
                            "description": "Transfer must be automated without human pause.",
                            "suggested_question": "Should transfers be automated immediately?",
                            "recommended_default": "Use automated synchronous approval.",
                            "conflicts_with": "process_operational",
                        }
                    ]
                }
            )
        if stage == "critic" and critic == CriticDomain.PROCESS_OPERATIONAL.value:
            return json.dumps(
                {
                    "findings": [
                        {
                            "req_id": "REQ-001",
                            "severity": "blocker",
                            "title": "Require manual dual-officer sign-off",
                            "description": "High-value wires require manual asynchronous review.",
                            "suggested_question": "Which finance officer performs manual review?",
                            "recommended_default": "Require manual dual-officer sign-off.",
                        }
                    ]
                }
            )
        if stage == "critic_arbitration":
            return json.dumps(
                {
                    "resolution": "Local draft arbitration.",
                    "recommended_default": "Hybrid rule.",
                }
            )
        return json.dumps({"findings": []})

    cloud_calls: list[LLMRequest] = []

    def _cloud_handler(req: LLMRequest) -> str:
        cloud_calls.append(req)
        return json.dumps(
            {
                "resolution": "Use automated pre-checks followed by mandatory manual dual sign-off > $25k.",
                "recommended_default": "Hybrid automated validation + manual dual approval.",
            }
        )

    local = MockProvider("mock-local", is_local=True, responses=[_local_handler] * 10)
    cloud = MockProvider("mock-gemini", is_local=False, responses=[_cloud_handler] * 5)
    router = ProviderRouter(local, cloud_providers=[cloud], strict_local=False, enable_cache=False)
    event_log = SessionEventLog(tmp_path / "sessions")
    event_log.create_session("sess-conflict")
    engine = ElicitationEngine(router, event_log)

    state = ElicitationState(
        session_id="sess-conflict",
        graph=graph,
        stale_req_ids=["REQ-001"],
    )
    state = engine.run_turn(state)

    assert len(state.disagreements) == 1
    disagreement = state.disagreements[0]
    assert disagreement.req_id == "REQ-001"
    assert CriticDomain.FUNCTIONAL_COMPLETENESS in disagreement.critics
    assert CriticDomain.PROCESS_OPERATIONAL in disagreement.critics
    assert disagreement.escalated is True
    assert disagreement.cloud_verified is True
    assert "dual sign-off" in disagreement.resolution
    assert len(cloud_calls) == 1
    assert cloud_calls[0].metadata.get("stage") == "critic_arbitration"

    events = event_log.load_events("sess-conflict")
    esc_events = [e for e in events if e.kind == EventKind.ESCALATION_TRIGGERED]
    assert len(esc_events) == 1
    assert esc_events[0].payload["reason"] == EscalationReason.CRITIC_DISAGREEMENT.value
    assert esc_events[0].payload["cloud_verified"] is True

    # Under strict_local=True, disagreement is still detected & arbitrated locally with 0 cloud calls.
    strict_local = MockProvider("mock-local-strict", is_local=True, responses=[_local_handler] * 10)
    strict_cloud = MockProvider("mock-cloud-unused", is_local=False)
    strict_router = ProviderRouter(
        strict_local,
        cloud_providers=[strict_cloud],
        strict_local=True,
        enable_cache=False,
    )
    _, strict_disagreements = CriticPanel(strict_router).run_on_nodes(graph)
    assert len(strict_disagreements) == 1
    assert strict_disagreements[0].escalated is False
    assert strict_disagreements[0].cloud_verified is False
    assert len(strict_cloud.calls) == 0


# ---------------------------------------------------------------------------
# 4. Delta dependency tracker & PROVEN delta-only re-analysis
# ---------------------------------------------------------------------------


def test_delta_tracker_subgraph_isolation_undirected_and_directed() -> None:
    graph = RequirementGraph(
        graph_id="g-delta",
        nodes=[
            _make_node("REQ-001", "Core Auth", "Users authenticate with MFA."),
            _make_node("REQ-002", "Session Timeout", "Sessions expire after 15 minutes."),
            _make_node("REQ-003", "CSV Export", "Users export reports as CSV."),
            _make_node("REQ-004", "Dark Mode", "UI supports dark theme."),
        ],
        edges=[
            RequirementEdge(
                source_id="REQ-002",
                target_id="REQ-001",
                relation=EdgeKind.DEPENDS_ON,
                rationale="Session depends on auth.",
            )
        ],
    )

    tracker = DeltaDependencyTracker()
    f1 = _make_finding("F-1", "REQ-001", CriticDomain.FUNCTIONAL_COMPLETENESS, FindingSeverity.BLOCKER)
    q1 = ElicitationQuestion(
        question_id="Q-001",
        prompt="Which MFA factors?",
        req_ids=["REQ-001"],
        finding_ids=["F-1"],
        severity=FindingSeverity.BLOCKER,
        info_gain_score=0.8,
    )
    a1 = UserAnswer(
        question_id="Q-001",
        answer="TOTP and WebAuthn",
        turn_id="turn-002",
        source="user_turn_2",
    )
    tracker.register_finding(f1)
    tracker.register_question(q1)
    touched = tracker.register_answer(a1, question=q1)

    assert touched == {"REQ-001"}
    assert tracker.findings_for_req("REQ-001") == {"F-1"}
    assert tracker.req_for_finding("F-1") == "REQ-001"
    assert tracker.reqs_for_question("Q-001") == {"REQ-001"}
    assert tracker.findings_for_question("Q-001") == {"F-1"}
    assert tracker.reqs_for_turn("turn-002") == {"REQ-001"}

    # Changing REQ-001 impacts connected REQ-002, NEVER disconnected REQ-003 or REQ-004.
    assert tracker.compute_impacted_req_ids(graph, ["REQ-001"]) == {"REQ-001", "REQ-002"}
    assert tracker.compute_impacted_req_ids(graph, ["REQ-003"]) == {"REQ-003"}
    assert tracker.compute_impacted_req_ids(graph, ["REQ-999"]) == set()

    # Directed mode: changing REQ-001 impacts REQ-002 (which DEPENDS_ON REQ-001),
    # whereas changing REQ-002 alone does not impact upstream REQ-001.
    assert tracker.compute_impacted_req_ids(graph, ["REQ-001"], directed=True) == {"REQ-001", "REQ-002"}
    assert tracker.compute_impacted_req_ids(graph, ["REQ-002"], directed=True) == {"REQ-002"}


def test_delta_only_reanalysis_proves_untouched_requirements_not_requeried(tmp_path: Path) -> None:
    """Prove with exact LLM call assertions that disconnected reqs are NEVER re-queried."""
    graph = RequirementGraph(
        graph_id="g-selective",
        nodes=[
            _make_node("REQ-001", "Payment Approval", "Approve invoice payments."),
            _make_node("REQ-002", "Payment Ledger", "Record approved payments in ledger."),
            _make_node("REQ-003", "CSV Export", "Allow exporting monthly summaries to CSV."),
            _make_node("REQ-004", "UI Localization", "Support English and Spanish labels."),
        ],
        edges=[
            RequirementEdge(
                source_id="REQ-002",
                target_id="REQ-001",
                relation=EdgeKind.DEPENDS_ON,
                rationale="Ledger entries depend on payment approvals.",
            )
        ],
    )

    def _critic_router(req: LLMRequest) -> str:
        critic = req.metadata.get("critic")
        req_ids = req.metadata.get("req_ids", [])
        if critic != CriticDomain.FUNCTIONAL_COMPLETENESS.value:
            return json.dumps({"findings": []})

        findings = []
        for rid in req_ids:
            # Once user_turn_2 clarifies REQ-001, stop flagging REQ-001.
            if rid == "REQ-001" and "[user_turn_2]" in req.prompt:
                continue
            if rid in ("REQ-001", "REQ-003"):
                findings.append(
                    {
                        "req_id": rid,
                        "severity": "blocker" if rid == "REQ-001" else "major",
                        "title": f"Clarify acceptance criteria for {rid}",
                        "description": f"{rid} needs explicit acceptance bounds.",
                        "suggested_question": f"What are the exact acceptance criteria for {rid}?",
                        "recommended_default": f"Default AC for {rid}.",
                    }
                )
        return json.dumps({"findings": findings})

    local = MockProvider("mock-local", responses=[_critic_router] * 30)
    router = ProviderRouter(local, strict_local=True, enable_cache=False)
    event_log = SessionEventLog(tmp_path / "sessions")
    event_log.create_session("sess-delta")
    engine = ElicitationEngine(router, event_log)

    state = ElicitationState(
        session_id="sess-delta",
        graph=graph,
        stale_req_ids=["REQ-001", "REQ-002", "REQ-003", "REQ-004"],
    )

    # Turn 1: initial full analysis -> 5 calls covering all 4 requirements.
    state = engine.run_turn(state)
    assert state.turn_count == 1
    assert len(local.calls) == 5
    for call in local.calls:
        assert call.metadata["req_ids"] == ["REQ-001", "REQ-002", "REQ-003", "REQ-004"]

    q_req1 = next(q for q in state.pending_questions if "REQ-001" in q.req_ids)
    assert any("REQ-003" in q.req_ids for q in state.pending_questions)

    # Turn 2: answer ONLY the question for REQ-001.
    calls_before_turn2 = len(local.calls)
    state = engine.run_turn(
        state,
        answers=[
            UserAnswer(
                question_id=q_req1.question_id,
                answer="Dual finance sign-off required above $5,000 with idempotency key.",
                turn_id="turn-002",
                source="user_turn_2",
            )
        ],
    )

    turn2_calls = local.calls[calls_before_turn2:]
    assert len(turn2_calls) == 5  # Exactly 1 call per specialist critic on impacted subgraph!
    for call in turn2_calls:
        assert call.metadata["req_ids"] == ["REQ-001", "REQ-002"]
        assert "REQ-003" not in call.prompt
        assert "REQ-004" not in call.prompt

    # Untouched REQ-003 finding is still preserved and pending!
    assert any(f.req_id == "REQ-003" and not f.resolved for f in state.findings)
    # REQ-001 finding was resolved with canonical turn provenance!
    assert any(
        f.req_id == "REQ-001" and f.resolved and f.resolved_by_turn == "turn-002"
        for f in state.findings
    )
    assert state.answers[-1].source == "user_turn_2"
    assert state.answers[-1].turn_id == "turn-002"
    assert any("[user_turn_2]" in ac for ac in state.graph.get_node("REQ-001").acceptance_criteria)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# 5. revise <turn_id>: downstream invalidation + selective re-analysis
# ---------------------------------------------------------------------------


def test_apply_revision_invalidates_downstream_turns_and_requeries_impacted_only(
    tmp_path: Path,
) -> None:
    graph = RequirementGraph(
        graph_id="g-revise",
        nodes=[
            _make_node("REQ-001", "Invoice Approval", "Approve invoices above threshold."),
            _make_node("REQ-002", "Ledger Posting", "Post approved invoices to ledger."),
            _make_node("REQ-003", "Theme Selector", "Allow switching light/dark UI theme."),
        ],
        edges=[
            RequirementEdge(
                source_id="REQ-002",
                target_id="REQ-001",
                relation=EdgeKind.DEPENDS_ON,
                rationale="Posting depends on approval.",
            )
        ],
    )

    def _critic_router(req: LLMRequest) -> str:
        critic = req.metadata.get("critic")
        req_ids = req.metadata.get("req_ids", [])
        if critic != CriticDomain.FUNCTIONAL_COMPLETENESS.value:
            return json.dumps({"findings": []})

        findings = []
        for rid in req_ids:
            if rid == "REQ-001" and "$10,000" in req.prompt:
                continue
            if rid == "REQ-002" and "double-entry" in req.prompt:
                continue
            if rid == "REQ-001" and "$2,500" in req.prompt:
                findings.append(
                    {
                        "req_id": "REQ-001",
                        "severity": "major",
                        "title": "Confirm lower threshold approver count",
                        "description": "Lower $2,500 threshold may need backup approver.",
                        "suggested_question": "Is a backup approver required at $2,500?",
                        "recommended_default": "Allow designated backup approver.",
                    }
                )
                continue
            if rid in ("REQ-001", "REQ-002", "REQ-003"):
                findings.append(
                    {
                        "req_id": rid,
                        "severity": "blocker" if rid != "REQ-003" else "minor",
                        "title": f"Define rule for {rid}",
                        "description": f"Missing specification detail on {rid}.",
                        "suggested_question": f"Specify exact rule for {rid}?",
                        "recommended_default": f"Default rule for {rid}.",
                    }
                )
        return json.dumps({"findings": findings})

    local = MockProvider("mock-local", responses=[_critic_router] * 40)
    router = ProviderRouter(local, strict_local=True, enable_cache=False)
    event_log = SessionEventLog(tmp_path / "sessions")
    event_log.create_session("sess-revise")
    engine = ElicitationEngine(router, event_log)

    state = ElicitationState(
        session_id="sess-revise",
        graph=graph,
        stale_req_ids=["REQ-001", "REQ-002", "REQ-003"],
    )

    # Turn 1: initial elicitation
    state = engine.run_turn(state)
    q1 = next(q for q in state.pending_questions if "REQ-001" in q.req_ids)

    # Turn 2: answer REQ-001 with $10,000 threshold
    state = engine.run_turn(
        state,
        answers=[{"question_id": q1.question_id, "answer": "Dual approval over $10,000."}],
    )
    q2 = next(q for q in state.pending_questions if "REQ-002" in q.req_ids)

    # Turn 3: answer REQ-002
    state = engine.run_turn(
        state,
        answers=[{"question_id": q2.question_id, "answer": "Post double-entry ledger records."}],
    )
    assert state.converged is True  # Only REQ-003 MINOR remains

    # Now revise turn-002: lower threshold to $2,500 -> invalidates turn-003 and re-checks ONLY {REQ-001, REQ-002}.
    calls_before_revise = len(local.calls)
    revised_state = engine.apply_revision(
        state,
        "turn-002",
        updated_answers=[
            {"question_id": q1.question_id, "answer": "Dual approval over $2,500."}
        ],
        note="CFO lowered dual-approval threshold to $2,500.",
    )

    revise_calls = local.calls[calls_before_revise:]
    assert len(revise_calls) == 5
    for call in revise_calls:
        assert call.metadata["req_ids"] == ["REQ-001", "REQ-002"]
        assert "REQ-003" not in call.prompt

    # Event log must show TURN_REVISED(turn-002) and TURN_INVALIDATED(turn-003) with full parity.
    assert event_log.verify_jsonl_parity("sess-revise") is True
    resumed = event_log.resume("sess-revise")
    assert "turn-003" in resumed["invalidated_turns"]
    all_events = event_log.load_events("sess-revise")
    assert any(e.kind == EventKind.TURN_REVISED and e.turn_id == "turn-002" for e in all_events)
    assert any(e.kind == EventKind.TURN_INVALIDATED and e.turn_id == "turn-003" for e in all_events)

    # Old turn-002/turn-003 acceptance criteria rolled back; new $2,500 answer present; convergence reopened.
    req1_ac = revised_state.graph.get_node("REQ-001").acceptance_criteria  # type: ignore[union-attr]
    req2_ac = revised_state.graph.get_node("REQ-002").acceptance_criteria  # type: ignore[union-attr]
    assert any("$2,500" in ac for ac in req1_ac)
    assert not any("$10,000" in ac for ac in req1_ac)
    assert not any("double-entry" in ac for ac in req2_ac)
    assert revised_state.converged is False
    # Untouched REQ-003 finding remained untouched.
    assert any(f.req_id == "REQ-003" for f in revised_state.findings)


# ---------------------------------------------------------------------------
# 6. Deduplication, clustering/ranking, convergence gate, finalize & persistence
# ---------------------------------------------------------------------------


def test_deduplicate_cluster_and_convergence_helpers() -> None:
    findings = [
        _make_finding("F1", "REQ-001", CriticDomain.FUNCTIONAL_COMPLETENESS, FindingSeverity.MAJOR, "Missing SLA", question="What is the SLA?"),
        # Duplicate with higher severity should win
        _make_finding("F2", "REQ-001", CriticDomain.FUNCTIONAL_COMPLETENESS, FindingSeverity.BLOCKER, "Missing SLA", question="What is the SLA?"),
        # Same question across a second requirement clusters together with boosted info gain
        _make_finding("F3", "REQ-002", CriticDomain.NON_FUNCTIONAL_QUALITY, FindingSeverity.MAJOR, "Latency target", question="What is the SLA?"),
        _make_finding("F4", "REQ-003", CriticDomain.PROCESS_OPERATIONAL, FindingSeverity.MAJOR, "SOP owner", question="Who owns incident SOP?"),
        _make_finding("F5", "REQ-004", CriticDomain.TRAINING_ADOPTION, FindingSeverity.MINOR, "Training plan", question="What training is needed?"),
        _make_finding("F6", "REQ-005", CriticDomain.LOGICAL_CONSISTENCY, FindingSeverity.BLOCKER, "Rule conflict", question="Which rule takes precedence?"),
        _make_finding("F7", "REQ-006", CriticDomain.FUNCTIONAL_COMPLETENESS, FindingSeverity.MAJOR, "Retry limit", question="What is max retry count?"),
        _make_finding("F8", "REQ-007", CriticDomain.NON_FUNCTIONAL_QUALITY, FindingSeverity.MINOR, "Log format", question="What log format?"),
        _make_finding("F9", "REQ-001", CriticDomain.TRAINING_ADOPTION, FindingSeverity.MINOR, "Resolved item", question="Done?", resolved=True, resolved_by_turn="turn-001"),
    ]

    deduped = deduplicate_findings(findings)
    unresolved = [f for f in deduped if not f.resolved]
    assert len(unresolved) == 7  # F1 collapsed into F2 (BLOCKER)
    assert any(f.finding_id == "F2" and f.severity == FindingSeverity.BLOCKER for f in unresolved)
    assert any(f.finding_id == "F9" and f.resolved for f in deduped)

    questions = cluster_and_rank_questions(deduped, min_questions=3, max_questions=5)
    assert 3 <= len(questions) <= 5
    assert questions[0].severity == FindingSeverity.BLOCKER
    assert all(0.0 < q.info_gain_score <= 1.0 for q in questions)

    # is_converged checks 0 unresolved BLOCKER and 0 unresolved MAJOR
    assert is_converged(deduped) is False
    only_minor = [
        _make_finding("M1", "REQ-001", CriticDomain.TRAINING_ADOPTION, FindingSeverity.MINOR),
        _make_finding("B1", "REQ-001", CriticDomain.FUNCTIONAL_COMPLETENESS, FindingSeverity.BLOCKER, resolved=True, resolved_by_turn="turn-001"),
    ]
    assert is_converged(only_minor) is True
    assert cluster_and_rank_questions([]) == []

    with pytest.raises(ValueError):
        cluster_and_rank_questions(findings, min_questions=0)
    with pytest.raises(ValueError):
        cluster_and_rank_questions(findings, min_questions=5, max_questions=3)


def test_finalize_gate_refusal_accept_defaults_and_state_persistence(tmp_path: Path) -> None:
    local = MockProvider("mock-local", default_response="plain text fallback")
    router = ProviderRouter(local, strict_local=True, max_local_retries=0, enable_cache=False)
    event_log = SessionEventLog(tmp_path / "sessions")
    engine = ElicitationEngine(router, event_log)

    # Full 1-liner prompt start -> Turn 1 -> convergence gate refusal -> finalize(accept_defaults=True)
    state = engine.start_from_prompt(
        "sess-fin",
        "Build a dual-approval invoice payment system with audit logs and staff training.",
    )
    assert state.turn_count == 0
    assert len(state.graph.nodes) >= 2

    state = engine.run_turn(state)
    assert state.turn_count == 1
    assert state.converged is False
    assert len(state.pending_questions) >= 1

    # Convergence gate refuses finalization without accept_defaults=True
    with pytest.raises(ConvergenceGateError):
        engine.finalize(state, accept_defaults=False)

    # Finalize with accept_defaults=True converts unresolved BLOCKER/MAJOR gaps into AssumptionRecords
    finalized = engine.finalize(state, accept_defaults=True)
    assert finalized.finalized is True
    assert finalized.accepted_defaults is True
    assert finalized.pending_questions == []
    assert len(finalized.assumptions) >= 1
    for idx, assumption in enumerate(finalized.assumptions, start=1):
        assert assumption.assumption_id == f"ADR-A{idx:03d}"
        assert assumption.type == "assumption"
        assert assumption.user_confirmed is False
        assert len(assumption.source_sha256) == 64
        assert len(assumption.req_ids) >= 1
        assert len(assumption.finding_ids) >= 1
    assert all(n.status == RequirementStatus.ACCEPTED for n in finalized.graph.nodes)

    # Persistence round-trip check
    saved_path = tmp_path / "sessions" / "sess-fin" / "elicitation_state.json"
    assert saved_path.is_file()
    loaded = engine.load_state("sess-fin")
    assert loaded == finalized

    with pytest.raises(FileNotFoundError):
        engine.load_state("sess-does-not-exist")

    # Converged session finalizes cleanly without accept_defaults and emits 0 assumptions
    clean_local = MockProvider("mock-clean", default_response=json.dumps({"findings": []}))
    clean_engine = ElicitationEngine(
        ProviderRouter(clean_local, strict_local=True, enable_cache=False),
        event_log,
    )
    doc_md = "# Scope\nThe system shall export JSON telemetry logs.\n"
    doc_sha = hashlib.sha256(doc_md.encode("utf-8")).hexdigest()
    chunks = HierarchicalChunker().chunk(
        NormalizedDocument(
            source_path=Path("scope.md"),
            source_sha256=doc_sha,
            media_type="text/markdown",
            parser_name="markdown-passthrough",
            markdown=doc_md,
            char_count=len(doc_md),
            anchors=[],
        )
    )
    clean_state = clean_engine.start_from_chunks("sess-clean", chunks)
    clean_state = clean_engine.run_turn(clean_state)
    assert clean_state.converged is True
    clean_final = clean_engine.finalize(clean_state, accept_defaults=False)
    assert clean_final.finalized is True
    assert clean_final.accepted_defaults is False
    assert clean_final.assumptions == []


def test_critic_batching_conflict_heuristics_and_glossary_reload(tmp_path: Path) -> None:
    """Exercise >5-node batching, antonym/marker conflicts, provider failure, and glossary reload."""
    nodes = [
        _make_node(f"REQ-{i:03d}", f"Feature {i}", f"The system shall execute operation {i}.")
        for i in range(1, 8)
    ]
    graph = RequirementGraph(graph_id="g-batch", nodes=nodes)

    def _batch_handler(req: LLMRequest) -> str:
        critic = req.metadata.get("critic")
        req_ids = req.metadata.get("req_ids", [])
        if req.metadata.get("stage") == "critic_arbitration":
            raise ProviderError("arbitrator down", provider="mock-batch")
        if "REQ-001" in req_ids and critic == CriticDomain.NON_FUNCTIONAL_QUALITY.value:
            return "```json\n" + json.dumps(
                {
                    "findings": [
                        {
                            "req_id": "REQ-001",
                            "severity": "major",
                            "title": "Enforce strict synchronous validation",
                            "description": "Validation must be strict and synchronous.",
                            "suggested_question": "Require strict synchronous mode?",
                            "recommended_default": "Use strict synchronous checks.",
                        }
                    ]
                }
            ) + "\n```"
        if "REQ-001" in req_ids and critic == CriticDomain.PROCESS_OPERATIONAL.value:
            return json.dumps(
                {
                    "findings": [
                        {
                            "req_id": "REQ-001",
                            "severity": "major",
                            "title": "Allow relaxed async batch review",
                            "description": "Operators prefer relaxed asynchronous review.",
                            "suggested_question": "Allow relaxed async review?",
                            "recommended_default": "Use relaxed async review.",
                        }
                    ]
                }
            )
        return json.dumps({"findings": []})

    local = MockProvider("mock-batch", responses=[_batch_handler] * 20)
    router = ProviderRouter(local, strict_local=True, max_local_retries=0, enable_cache=False)
    panel = CriticPanel(router)
    findings, disagreements = panel.run_on_nodes(graph, glossary_header="## Global Glossary\n- SLA: 99.9%")

    # 7 nodes -> 2 sequential batches per critic x 5 critics = 10 critic calls
    critic_calls = [c for c in local.calls if c.metadata.get("stage") == "critic"]
    assert len(critic_calls) == 10
    assert len(findings) == 2
    assert len(disagreements) == 1
    assert "Arbitration deferred" in disagreements[0].resolution

    # Verify glossary_header reloads cleanly in a new ElicitationEngine instance and idle run_turn makes 0 calls.
    event_log = SessionEventLog(tmp_path / "sessions")
    doc_md = "# Glossary\n- **OrderEngine**: Core order processor.\n\n# Orders\nOrderEngine validates orders.\n"
    doc_sha = hashlib.sha256(doc_md.encode("utf-8")).hexdigest()
    chunks = HierarchicalChunker().chunk(
        NormalizedDocument(
            source_path=Path("orders.md"),
            source_sha256=doc_sha,
            media_type="text/markdown",
            parser_name="markdown-passthrough",
            markdown=doc_md,
            char_count=len(doc_md),
            anchors=[],
        )
    )
    empty_local = MockProvider("mock-empty", default_response=json.dumps({"findings": []}))
    engine1 = ElicitationEngine(ProviderRouter(empty_local, strict_local=True, max_local_retries=0), event_log)
    state = engine1.start_from_chunks("sess-reload", chunks)
    state = engine1.run_turn(state)

    # Fresh engine instance reloads persisted glossary_header.txt and makes 0 calls on idle turn.
    empty_local2 = MockProvider("mock-empty2", default_response=json.dumps({"findings": []}))
    engine2 = ElicitationEngine(ProviderRouter(empty_local2, strict_local=True, max_local_retries=0), event_log)
    loaded = engine2.load_state("sess-reload")
    assert "OrderEngine" in engine2._get_glossary_header("sess-reload")
    calls_before = len(empty_local2.calls)
    idle_state = engine2.run_turn(loaded)
    assert len(empty_local2.calls) == calls_before
    assert idle_state.turn_count == 2


def test_decompose_chunks_cross_chunk_edges_dedup_and_prompt_clamping() -> None:
    """Verify cross-chunk DAG edges, duplicate statement collapse, and <6K prompt budget."""
    long_filler = " ".join(["constraint compliance budget data retention"] * 100)
    markdown = (
        "# Part One\n"
        f"{long_filler}\n"
        "The system must never expose plaintext tokens.\n\n"
        "# Part Two\n"
        "The system provides a REST API endpoint for status queries.\n"
        "The system stores retention archives in SQLite.\n"
    )
    doc_sha = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    doc = NormalizedDocument(
        source_path=Path("big.md"),
        source_sha256=doc_sha,
        media_type="text/markdown",
        parser_name="markdown-passthrough",
        markdown=markdown,
        char_count=len(markdown),
        anchors=[],
    )
    chunks = HierarchicalChunker(max_chunk_chars=5000, overlap_chars=0).chunk(doc)
    assert chunks.chunk_count == 2

    responses = [
        json.dumps(
            {
                "requirements": [
                    {
                        "req_id": "C1-A",
                        "title": "Token Secrecy",
                        "statement": "The system must never expose plaintext tokens.",
                        "kind": "constraint",
                        "priority": "P0",
                        "acceptance_criteria": [],
                        "excerpt": "must never expose plaintext tokens",
                    }
                ],
                "edges": [],
            }
        ),
        json.dumps(
            {
                "requirements": [
                    # Duplicate statement from chunk 1 should be deduplicated
                    {
                        "req_id": "DUP",
                        "title": "Token Secrecy Duplicate",
                        "statement": "The system must never expose plaintext tokens.",
                        "kind": "constraint",
                        "priority": "P0",
                        "acceptance_criteria": [],
                        "excerpt": "",
                    },
                    {
                        "req_id": "C2-A",
                        "title": "Status API Endpoint",
                        "statement": "The system provides a REST API endpoint for status queries.",
                        "kind": "interface",
                        "priority": "P1",
                        "acceptance_criteria": [],
                        "excerpt": "REST API endpoint",
                    },
                ],
                "edges": [
                    {
                        "source_id": "C2-A",
                        "target_id": "REQ-001",
                        "relation": "depends_on",
                        "rationale": "Status API obeys token secrecy.",
                    },
                    # Self-loop / duplicate edge / reverse cycle must be safely ignored
                    {
                        "source_id": "C2-A",
                        "target_id": "C2-A",
                        "relation": "depends_on",
                    },
                    {
                        "source_id": "REQ-001",
                        "target_id": "C2-A",
                        "relation": "depends_on",
                    },
                ],
            }
        ),
    ]
    # Add extra empty responses if chunker produced > 2 chunks
    while len(responses) < chunks.chunk_count:
        responses.append(json.dumps({"requirements": [], "edges": []}))

    local = MockProvider("mock-edges", responses=responses)
    router = ProviderRouter(local, strict_local=True, max_local_retries=0, enable_cache=False)
    graph = RequirementDecomposer(router).decompose_chunks(chunks, session_id="sess-edges")

    assert [n.req_id for n in graph.nodes] == ["REQ-001", "REQ-002"]
    assert graph.is_dag()
    assert len(graph.edges) == 1
    assert graph.edges[0].source_id == "REQ-002"
    assert graph.edges[0].target_id == "REQ-001"
    assert all(len(c.prompt) <= MAX_DECOMPOSE_PROMPT_CHARS for c in local.calls)


# ---------------------------------------------------------------------------
# 7. Edge cases, fallbacks, validators, and defensive branch coverage
# ---------------------------------------------------------------------------


def test_models_blank_validators_and_explicit_none_sha() -> None:
    finding = CriticFinding(
        finding_id="F-NONE",
        req_id="REQ-001",
        critic=CriticDomain.FUNCTIONAL_COMPLETENESS,
        severity=FindingSeverity.MINOR,
        title="Minor note",
        description="Optional source sha",
        suggested_question="Confirm?",
        source_sha256=None,
    )
    assert finding.source_sha256 is None

    with pytest.raises(ValidationError):
        CriticDisagreement(
            req_id="REQ-001",
            critics=[CriticDomain.FUNCTIONAL_COMPLETENESS, CriticDomain.LOGICAL_CONSISTENCY],
            summary="   ",
        )

    with pytest.raises(ValidationError):
        ElicitationQuestion(
            question_id="Q-1",
            prompt="   ",
            severity=FindingSeverity.MINOR,
            info_gain_score=0.5,
        )

    with pytest.raises(ValidationError):
        AssumptionRecord(
            assumption_id="ADR-A001",
            title="   ",
            statement="Default",
            req_ids=["REQ-001"],
            source_sha256=_SHA_A,
            rationale="Fallback",
        )


def test_decomposer_helpers_edge_cases_and_edge_dedup() -> None:
    from reqhq.elicitation.decomposer import (
        _clamp_prompt,
        _infer_kind,
        _make_title,
        _split_clauses_deterministic,
        _try_parse_payload,
    )

    assert _infer_kind("Store encrypted database backup in archive storage.") == RequirementKind.SECURITY
    assert _infer_kind("Maintain database schema retention archive.") == RequirementKind.DATA
    assert _make_title("!!! --- ???", 4) == "Requirement 4"

    clamped = _clamp_prompt("x" * 200, limit=60)
    assert clamped.endswith("[truncated-for-budget]")
    assert len(clamped) <= 65

    # Short clauses (<8 chars) are skipped by deterministic splitter.
    clauses = _split_clauses_deterministic("OK.\nTBD.\nThe system shall validate order totals.")
    assert len(clauses) == 1
    assert "validate order totals" in clauses[0][0]

    # _try_parse_payload handles invalid parsed dict, fenced JSON fallback, and missing key JSON.
    fenced = '```json\n{"requirements": [{"title": "T", "statement": "S"}], "edges": []}\n```'
    parsed_ok = _try_parse_payload(fenced, parsed={"bad": "shape"})
    assert parsed_ok is not None
    assert len(parsed_ok.requirements) == 1
    assert _try_parse_payload('{"status": "ok"}', parsed=None) is None

    # decompose_prompt _safe_edges ignores unknown endpoints, self-loops, and duplicate edges.
    prompt = "Validate orders and write records."
    scripted = json.dumps(
        {
            "requirements": [
                {"req_id": "A", "title": "Validate", "statement": "Validate orders.", "excerpt": "Validate orders"},
                {"req_id": "B", "title": "Write", "statement": "Write records.", "excerpt": "Write records"},
            ],
            "edges": [
                {"source_id": "B", "target_id": "A", "relation": "depends_on"},
                {"source_id": "B", "target_id": "A", "relation": "depends_on"},  # duplicate
                {"source_id": "A", "target_id": "UNKNOWN", "relation": "depends_on"},  # unknown
                {"source_id": "A", "target_id": "A", "relation": "depends_on"},  # self-loop
            ],
        }
    )
    local = MockProvider("mock-safe-edges", responses=[scripted])
    graph = RequirementDecomposer(ProviderRouter(local, strict_local=True)).decompose_prompt(
        prompt, session_id="sess-safe"
    )
    assert len(graph.edges) == 1

    # decompose_chunks: duplicate cross-chunk edge & duplicate fallback clauses across overlapping chunks.
    md = (
        "# Sec A\nThe system shall maintain immutable storage backups.\n\n"
        "# Sec B\nThe system shall maintain immutable storage backups.\n"
        "The system shall publish webhook notifications.\n"
    )
    doc = NormalizedDocument(
        source_path=Path("dup.md"),
        source_sha256=hashlib.sha256(md.encode("utf-8")).hexdigest(),
        media_type="text/markdown",
        parser_name="markdown-passthrough",
        markdown=md,
        char_count=len(md),
        anchors=[],
    )
    chunk_res = HierarchicalChunker(max_chunk_chars=80, overlap_chars=0).chunk(doc)
    assert chunk_res.chunk_count >= 2

    # Structured mode duplicate cross-chunk edge
    edge_resp = [
        json.dumps(
            {
                "requirements": [{"req_id": "X1", "title": "Backup", "statement": "Maintain backups."}],
                "edges": [],
            }
        ),
        json.dumps(
            {
                "requirements": [{"req_id": "X2", "title": "Notify", "statement": "Publish webhooks."}],
                "edges": [
                    {"source_id": "X2", "target_id": "REQ-001", "relation": "depends_on"},
                    {"source_id": "X2", "target_id": "REQ-001", "relation": "depends_on"},
                ],
            }
        ),
    ] + [json.dumps({"requirements": [], "edges": []})] * chunk_res.chunk_count
    g_edge = RequirementDecomposer(
        ProviderRouter(MockProvider("m-dup-e", responses=edge_resp), strict_local=True, max_local_retries=0)
    ).decompose_chunks(chunk_res, session_id="sess-dup-e")
    assert len(g_edge.edges) == 1

    # Deterministic fallback mode deduplicates repeated clause across chunks
    g_fb = RequirementDecomposer(
        ProviderRouter(MockProvider("m-dup-fb", default_response="plain"), strict_local=True, max_local_retries=0)
    ).decompose_chunks(chunk_res, session_id="sess-dup-fb")
    backup_nodes = [n for n in g_fb.nodes if "immutable storage backups" in n.statement]
    assert len(backup_nodes) == 1


def test_critics_edge_cases_cleared_fallbacks_hallucination_and_arbitration_degradation() -> None:
    from reqhq.elicitation.critics import (
        _findings_conflict,
        _format_nodes_compact,
        _node_source_sha,
        _try_parse_critic_payload,
    )

    # Node without trace links -> _node_source_sha returns None
    bare_node = RequirementNode(
        req_id="REQ-001",
        title="Bare Node",
        statement="System completes requests within 150ms p95 with RBAC.",
        acceptance_criteria=["[user_turn_1] Verified with owner and training plan."],
    )
    assert _node_source_sha(bare_node) is None

    # Compact prompt truncation clamp
    clamped_critic = _format_nodes_compact([bare_node], glossary_header="Glossary", max_chars=80)
    assert clamped_critic.endswith("[truncated]")

    # _try_parse_critic_payload handles malformed parsed dict, fenced JSON, and JSON without findings key
    fenced_findings = '```json\n{"findings": []}\n```'
    assert _try_parse_critic_payload(fenced_findings, parsed={"wrong": 1}) is not None
    assert _try_parse_critic_payload('{"status": "ok"}', parsed=None) is None

    # BaseCritic.evaluate with empty node list returns immediately
    router_noop = ProviderRouter(MockProvider("m-noop"), strict_local=True)
    critic = FunctionalCompletenessCritic()
    graph_bare = RequirementGraph(graph_id="g-bare", nodes=[bare_node])
    assert critic.evaluate(router_noop, [], graph=graph_bare) == ([], {})

    # ProviderError inside BaseCritic.evaluate triggers deterministic fallback cleanly
    err_router = ProviderRouter(
        MockProvider("m-err", responses=[ProviderError("offline", provider="m-err")]),
        strict_local=True,
        max_local_retries=0,
    )
    unclarified = RequirementNode(
        req_id="REQ-002",
        title="Unclarified Feature",
        statement="Handle requests.",
    )
    err_findings, _ = critic.evaluate(
        err_router,
        [unclarified],
        graph=RequirementGraph(graph_id="g-err", nodes=[unclarified]),
    )
    assert len(err_findings) == 1
    assert err_findings[0].source_sha256 is None

    # Hallucinated req_id not in batch is silently ignored
    hallucinated = json.dumps(
        {
            "findings": [
                {
                    "req_id": "REQ-999",
                    "severity": "blocker",
                    "title": "Ghost req",
                    "description": "Does not exist",
                    "suggested_question": "Ghost?",
                }
            ]
        }
    )
    hal_router = ProviderRouter(MockProvider("m-hal", responses=[hallucinated]), strict_local=True)
    hal_findings, _ = critic.evaluate(hal_router, [bare_node], graph=graph_bare)
    assert hal_findings == []

    # All 5 deterministic critic fallbacks clear cleanly on clarified / measurable requirements
    nfr_measured = RequirementNode(
        req_id="REQ-003",
        title="Measured Service",
        statement="API responds in <= 200ms p95 with TLS.",
        acceptance_criteria=["Latency verified <= 200ms."],
    )
    graph_clear = RequirementGraph(
        graph_id="g-clear",
        nodes=[bare_node, nfr_measured],
        edges=[
            RequirementEdge(
                source_id="REQ-001",
                target_id="REQ-003",
                relation=EdgeKind.CONFLICTS_WITH,
            )
        ],
    )
    fb_router = ProviderRouter(
        MockProvider("m-fb-clear", default_response='{"status": "ok"}'),
        strict_local=True,
        max_local_retries=0,
    )
    fb_findings, _ = CriticPanel(fb_router).run_on_nodes(graph_clear, target_req_ids=["REQ-001"])
    assert fb_findings == []
    nfr_findings, _ = NonFunctionalQualityCritic().evaluate(
        fb_router, [nfr_measured], graph=graph_clear
    )
    assert nfr_findings == []

    # Same-critic findings on same req_id do NOT conflict with each other
    fa = _make_finding("FA", "REQ-001", CriticDomain.FUNCTIONAL_COMPLETENESS, FindingSeverity.MAJOR, title="Conflict A")
    fb = _make_finding("FB", "REQ-001", CriticDomain.FUNCTIONAL_COMPLETENESS, FindingSeverity.MAJOR, title="Conflict B")
    assert _findings_conflict(fa, fb, {}) is False

    # Arbitration degradation: (a) raw JSON with extra key (parsed=None) salvages resolution,
    # (b) raw JSON without resolution key falls back to default stakeholder-decision text.
    disagree_a = _make_finding(
        "DA",
        "REQ-001",
        CriticDomain.FUNCTIONAL_COMPLETENESS,
        FindingSeverity.BLOCKER,
        title="Automated flow",
        default="Use automated mode.",
    )
    disagree_b = _make_finding(
        "DB",
        "REQ-001",
        CriticDomain.PROCESS_OPERATIONAL,
        FindingSeverity.BLOCKER,
        title="Manual flow",
        default="Use manual mode.",
    )
    # (a) JSON with extra field fails strict schema -> parsed=None -> json.loads salvages resolution
    panel_salvage = CriticPanel(
        ProviderRouter(
            MockProvider(
                "m-arb-salvage",
                default_response=json.dumps({"resolution": "Salvaged arbitration text", "extra_field": 1}),
            ),
            strict_local=True,
            max_local_retries=0,
            enable_cache=False,
        )
    )
    res_salvage = panel_salvage._arbitrate_disagreement(
        req_id="REQ-001",
        critics=[CriticDomain.FUNCTIONAL_COMPLETENESS, CriticDomain.PROCESS_OPERATIONAL],
        involved=[disagree_a, disagree_b],
        summary="Conflict",
        glossary_header="",
    )
    assert res_salvage.resolution == "Salvaged arbitration text"

    # (b) JSON with no resolution key -> falls back to explicit stakeholder decision message
    panel_default = CriticPanel(
        ProviderRouter(
            MockProvider("m-arb-empty", default_response='{"status": "ok"}'),
            strict_local=True,
            max_local_retries=0,
            enable_cache=False,
        )
    )
    res_default = panel_default._arbitrate_disagreement(
        req_id="REQ-001",
        critics=[CriticDomain.FUNCTIONAL_COMPLETENESS, CriticDomain.PROCESS_OPERATIONAL],
        involved=[disagree_a, disagree_b],
        summary="Conflict",
        glossary_header="",
    )
    assert "Requires explicit stakeholder decision on REQ-001" in res_default.resolution


def test_engine_direct_req_answers_missing_node_guard_and_assumption_sha_fallback(
    tmp_path: Path,
) -> None:
    """Cover direct req_id answering, turn_count=0 empty stale fallback, missing node guard, and sha fallback."""
    node_with_trace = _make_node("REQ-001", "Auth", "Authenticate users.", sha=_SHA_A)
    node_without_trace = RequirementNode(
        req_id="REQ-002",
        title="Audit",
        statement="Log events.",
    )
    graph = RequirementGraph(graph_id="g-edge", nodes=[node_with_trace, node_without_trace])
    event_log = SessionEventLog(tmp_path / "sessions")
    event_log.create_session("sess-edge")

    local = MockProvider("m-edge", default_response=json.dumps({"findings": []}))
    engine = ElicitationEngine(
        ProviderRouter(local, strict_local=True, max_local_retries=0, enable_cache=False),
        event_log,
    )

    # 1. turn_count=0 with empty stale_req_ids evaluates all active graph nodes.
    state = ElicitationState(session_id="sess-edge", graph=graph, stale_req_ids=[])
    state = engine.run_turn(state)
    assert state.turn_count == 1
    assert len(local.calls) == 5

    # 2. Direct answer using question_id="REQ-001" + question referencing missing req_id ("REQ-GHOST").
    state.pending_questions = [
        ElicitationQuestion(
            question_id="Q-GHOST",
            prompt="Clarify ghost?",
            req_ids=["REQ-GHOST", "REQ-001"],
            finding_ids=[],
            severity=FindingSeverity.MINOR,
            info_gain_score=0.3,
        )
    ]
    state = engine.run_turn(
        state,
        answers=[
            {"question_id": "REQ-001", "answer": "Use WebAuthn passkeys."},
            {"question_id": "Q-GHOST", "answer": "Ignore ghost safely."},
        ],
    )
    assert any("WebAuthn" in ac for ac in state.graph.get_node("REQ-001").acceptance_criteria)  # type: ignore[union-attr]

    # 3. Revise turn-002 using direct question_id="REQ-001"
    revised = engine.apply_revision(
        state,
        "turn-002",
        updated_answers=[{"question_id": "REQ-001", "answer": "Use TOTP + passkeys."}],
        note="Add TOTP backup.",
    )
    assert any("TOTP" in ac for ac in revised.graph.get_node("REQ-001").acceptance_criteria)  # type: ignore[union-attr]

    # 4. finalize(accept_defaults=True) with findings whose source_sha256=None:
    #    - REQ-001 falls back to node.trace_links[0].source_sha256 (_SHA_A)
    #    - REQ-002 (no trace_links) falls back to deterministic _fallback_sha256(...)
    revised.findings = [
        CriticFinding(
            finding_id="F-NO-SHA-1",
            req_id="REQ-001",
            critic=CriticDomain.FUNCTIONAL_COMPLETENESS,
            severity=FindingSeverity.BLOCKER,
            title="Gap 1",
            description="Missing detail 1",
            suggested_question="Detail 1?",
            recommended_default="Default 1",
            source_sha256=None,
        ),
        CriticFinding(
            finding_id="F-NO-SHA-2",
            req_id="REQ-002",
            critic=CriticDomain.NON_FUNCTIONAL_QUALITY,
            severity=FindingSeverity.MAJOR,
            title="Gap 2",
            description="Missing detail 2",
            suggested_question="Detail 2?",
            recommended_default="",
            source_sha256=None,
        ),
    ]
    finalized = engine.finalize(revised, accept_defaults=True)
    assert len(finalized.assumptions) == 2
    by_req = {a.req_ids[0]: a for a in finalized.assumptions}
    assert by_req["REQ-001"].source_sha256 == _SHA_A
    assert len(by_req["REQ-002"].source_sha256) == 64
    assert by_req["REQ-002"].source_sha256 != _SHA_A
    assert "Assume default resolution for REQ-002" in by_req["REQ-002"].statement


def test_chunk_fallback_filters_pdf_frontmatter_noise_and_bounds_extraction() -> None:
    """Verify DEF-P4B-002: offline chunk fallback filters cover/TOC/code noise and bounds output."""
    from reqhq.elicitation.decomposer import (
        MAX_FALLBACK_NODES_TOTAL,
        _extract_chunk_fallback_clauses,
        _split_clauses_deterministic,
    )

    # Zero-budget call returns empty immediately; tiny non-empty text triggers _split_clauses fallback.
    assert _extract_chunk_fallback_clauses("The system shall log events.", max_clauses=0) == []
    assert _split_clauses_deterministic("OK") == [("OK", 0, 2)]

    noisy_srs = (
        "E-GOVERNANCE\n"
        "FUNCTIONAL REQUIREMENTS SPECIFICATION\n"
        "Team Members:\n"
        "Table of Contents\n"
        "1. Introduction .................................................... 4\n"
        "Ministry of Home Affairs          Draft Core Scope          Page 2 of 19\n"
        "\x0cE-Governance Project Running Header\n"
        "| Col A | Col B |\n"
        "| --- | --- |\n"
        "```python\n"
        "def should_be_ignored():\n"
        "    return True\n"
        "```\n"
        "= * Non-alphanumeric grammar entry that must be ignored.\n"
        "Plain descriptive remark about weather in summer afternoons.\n\n"
        "               Once the audit trail functionality has been activated, the System must track\n"
        "     2.        events without manual intervention and store audit metadata.\n\n"
        "               The System must maintain the audit trail for as long as required, which will be at\n"
        "     3.\n"
        "               least for the life of the case to which it refers.\n\n"
        "The portal enables authorized operators to inspect case history securely.\n\n"
        "The system must\n"
        "verify cryptographic signatures on all exported bundles.\n"
    )
    clauses = _extract_chunk_fallback_clauses(noisy_srs, max_clauses=5)
    assert len(clauses) == 4
    joined_stmts = "\n".join(c[0] for c in clauses)
    assert "E-GOVERNANCE" not in joined_stmts
    assert "Table of Contents" not in joined_stmts
    assert "should_be_ignored" not in joined_stmts
    assert "Running Header" not in joined_stmts
    assert "weather in summer" not in joined_stmts
    assert "events without manual intervention" in joined_stmts
    assert "least for the life of the case" in joined_stmts
    assert "enables authorized operators" in joined_stmts
    assert "verify cryptographic signatures" in joined_stmts

    # Verify multi-chunk document fallback is bounded cleanly to <= MAX_FALLBACK_NODES_TOTAL.
    many_reqs = "\n\n".join(
        f"## Section {i}\n"
        + "\n".join(
            f"The subsystem S{i:02d} shall enforce rule R{i:02d}-{j:02d} with deterministic auditability."
            for j in range(1, 15)
        )
        for i in range(1, 7)
    )
    doc_many = NormalizedDocument(
        source_path=Path("many.md"),
        source_sha256=hashlib.sha256(many_reqs.encode("utf-8")).hexdigest(),
        media_type="text/markdown",
        parser_name="markdown-passthrough",
        markdown=many_reqs,
        char_count=len(many_reqs),
        anchors=[],
    )
    chunks_many = HierarchicalChunker(max_chunk_chars=1200, overlap_chars=0).chunk(doc_many)
    plain_router = ProviderRouter(
        MockProvider("m-plain-many", default_response="plain"),
        strict_local=True,
        max_local_retries=0,
        enable_cache=False,
    )
    bounded_graph = RequirementDecomposer(plain_router).decompose_chunks(
        chunks_many,
        session_id="sess-bounded",
    )
    assert 1 <= len(bounded_graph.nodes) <= MAX_FALLBACK_NODES_TOTAL
    assert bounded_graph.validate_traceability(require_all=True) == []

    # Safety net: a non-empty document with zero modal/action cues still emits 1 traceable node.
    plain_only = "Alpha beta gamma delta epsilon zeta eta theta iota kappa."
    doc_plain = NormalizedDocument(
        source_path=Path("plain.md"),
        source_sha256=hashlib.sha256(plain_only.encode("utf-8")).hexdigest(),
        media_type="text/markdown",
        parser_name="markdown-passthrough",
        markdown=plain_only,
        char_count=len(plain_only),
        anchors=[],
    )
    chunks_plain = HierarchicalChunker(max_chunk_chars=500, overlap_chars=0).chunk(doc_plain)
    safety_graph = RequirementDecomposer(plain_router).decompose_chunks(
        chunks_plain,
        session_id="sess-safety",
    )
    assert len(safety_graph.nodes) == 1
    assert safety_graph.validate_traceability(require_all=True) == []




