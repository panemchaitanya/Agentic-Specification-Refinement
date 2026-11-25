"""Offline tests for Stage 4a/5a Socio-Technical Triage, Routing/Assumption ADRs, and Emitters."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any

from pydantic import ValidationError
import pytest

from reqhq.llm import MockProvider, ProviderRouter
from reqhq.schemas.requirements import (
    EdgeKind,
    RequirementEdge,
    RequirementGraph,
    RequirementKind,
    RequirementNode,
    RequirementStatus,
    TraceLink,
)
from reqhq.schemas.routing_adr import RoutingADR, RoutingDecisionKind
from reqhq.synthesis import (
    AssumptionLike,
    EmittedADRFile,
    SocioTechnicalTriager,
    SynthesisBundleResult,
    TraceabilityMatrix,
    TraceabilityRow,
    TriageItem,
    TriagePlan,
    TriageTrack,
    build_assumption_adrs,
    build_routing_adrs,
    build_traceability_matrix,
    dump_yaml_frontmatter,
    emit_design_md,
    emit_process_and_training_md,
    parse_emitted_adr,
    parse_yaml_frontmatter,
    validate_assumption_adr_markdown,
    validate_routing_adr_markdown,
    write_synthesis_bundle,
)
from reqhq.synthesis.design_emitter import _safe_mermaid_id

_SHA_A = hashlib.sha256(b"prd-source-doc-v1").hexdigest()
_SHA_B = hashlib.sha256(b"ops-manual-v2").hexdigest()
_SHA_DIGITS_ONLY = "0" * 64


def _make_four_track_graph() -> RequirementGraph:
    """Build a multi-track RequirementGraph spanning all 4 tracks plus deprecated/superseded nodes."""
    return RequirementGraph(
        graph_id="graph-clinic-v2",
        nodes=[
            RequirementNode(
                req_id="REQ-001",
                title="Automated Prescription Dose Validation API",
                statement="The system shall validate prescription dosage ranges via a deterministic API before persistence.",
                kind=RequirementKind.FUNCTIONAL,
                status=RequirementStatus.ACCEPTED,
                priority="P0",
                acceptance_criteria=[
                    "Reject any dosage outside therapeutic bounds with deterministic error code.",
                    "Persist validated prescription state atomically.",
                ],
                trace_links=[
                    TraceLink(
                        source_sha256=_SHA_A,
                        source_uri="file://prd.md",
                        anchor="section-2.1#L10-L18",
                        excerpt="dosage ranges must be validated",
                    )
                ],
                tags=["api", "safety"],
            ),
            RequirementNode(
                req_id="REQ-002",
                title="Controlled Substance Dual-Signoff Governance SOP",
                statement="A licensed pharmacist and duty manager must perform manual approval and sign-off for Schedule II overrides.",
                kind=RequirementKind.PROCESS,
                status=RequirementStatus.ACCEPTED,
                priority="P0",
                acceptance_criteria=[
                    "Two-person manual approval logged before dispensing override.",
                    "Escalate unresolved overrides to Chief Pharmacist within 30 minutes.",
                ],
                trace_links=[
                    TraceLink(
                        source_sha256=_SHA_B,
                        source_uri="file://ops.md",
                        anchor="section-4.2#L40-L55",
                        excerpt="manual approval for Schedule II overrides",
                    )
                ],
                tags=["sop", "compliance"],
            ),
            RequirementNode(
                req_id="REQ-003",
                title="Pharmacist Onboarding and Safety Certification Curriculum",
                statement="All dispensing pharmacists must complete annual training and competency certification prior to production access.",
                kind=RequirementKind.NON_FUNCTIONAL,
                status=RequirementStatus.PROPOSED,
                priority="P1",
                acceptance_criteria=[
                    "Complete hands-on lab covering emergency override workflows.",
                    "Score >= 90% on competency assessment.",
                ],
                trace_links=[
                    TraceLink(
                        source_sha256=_SHA_B,
                        source_uri="file://ops.md",
                        anchor="section-5.1#L1-L14",
                        excerpt="complete annual training and competency certification",
                    )
                ],
                tags=["training", "onboarding"],
            ),
            RequirementNode(
                req_id="REQ-004",
                title="High-Risk Allergy Alert & Clinical Override Workflow",
                statement=(
                    "The automated alert engine shall flag severe drug-allergy conflicts via API, "
                    "triggering a mandatory clinician manual approval SOP and annual refresher training."
                ),
                kind=RequirementKind.SECURITY,
                status=RequirementStatus.VERIFIED,
                priority="P0",
                acceptance_criteria=[
                    "Software engine blocks dispensing until override token is recorded.",
                    "Attending clinician completes documented override checklist.",
                ],
                trace_links=[
                    TraceLink(
                        source_sha256=_SHA_A,
                        source_uri="file://prd.md",
                        anchor="section-3.4#L88-L102",
                        excerpt="flag severe drug-allergy conflicts and require clinician override",
                    )
                ],
                tags=["hybrid", "clinical-safety"],
            ),
            RequirementNode(
                req_id="REQ-005",
                title="Legacy Fax Ingestion",
                statement="Legacy fax ingestion is deprecated.",
                kind=RequirementKind.INTERFACE,
                status=RequirementStatus.DEPRECATED,
                priority="P3",
                trace_links=[
                    TraceLink(
                        source_sha256=_SHA_A,
                        source_uri="file://prd.md",
                        anchor="appendix-a",
                    )
                ],
            ),
            RequirementNode(
                req_id="REQ-006",
                title="Old Paper Ledger",
                statement="Superseded by electronic prescription ledger.",
                kind=RequirementKind.DATA,
                status=RequirementStatus.SUPERSEDED,
                priority="P3",
            ),
        ],
        edges=[
            RequirementEdge(
                source_id="REQ-001",
                target_id="REQ-004",
                relation=EdgeKind.DEPENDS_ON,
                rationale="Allergy override builds on prescription validation.",
            )
        ],
    )


def _scripted_triage_batches() -> list[str]:
    """Return two sequential TIER_0 batch JSON payloads covering REQ-001..REQ-004."""
    batch_1 = {
        "items": [
            {
                "req_id": "REQ-001",
                "track": "software",
                "rationale": "Deterministic dosage validation is pure domain logic ideal for Functional Core.",
                "rejected_alternatives": ["Manual pharmacist lookup table rejected due to error risk."],
                "prerequisites": [],
                "component_breakdown": {},
                "human_roles": [],
                "software_module": "prescription_validator",
            },
            {
                "req_id": "REQ-002",
                "track": "process",
                "rationale": "Dual-signoff regulatory accountability requires human governance SOP.",
                "rejected_alternatives": ["Auto-approving Schedule II overrides violates DEA rules."],
                "prerequisites": ["Chief Pharmacist approves Schedule II override SOP."],
                "component_breakdown": {},
                "human_roles": ["Licensed Pharmacist", "Duty Manager"],
                "software_module": "",
            },
        ]
    }
    batch_2 = {
        "items": [
            {
                "req_id": "REQ-003",
                "track": "training",
                "rationale": "Competency and certification require role-based curriculum rather than code.",
                "rejected_alternatives": ["In-app tooltip popups rejected as non-verifiable."],
                "prerequisites": ["LMS curriculum published and signed off by Clinical Educator."],
                "component_breakdown": {},
                "human_roles": ["Dispensing Pharmacist", "Clinical Educator"],
                "software_module": "",
            },
            {
                "req_id": "REQ-004",
                "track": "hybrid",
                "rationale": "Requires automated allergy conflict detection plus human clinical override SOP and training.",
                "rejected_alternatives": [
                    "Pure software hard-block rejected because emergency clinical overrides are necessary.",
                    "Pure manual check rejected because latency and recall risk are unacceptable.",
                ],
                "prerequisites": [
                    "Clinical Override SOP signed off by Medical Director.",
                    "Clinicians complete allergy-override refresher module.",
                ],
                "component_breakdown": {
                    "Software": "Pure Functional Core allergy conflict detector and override token state machine.",
                    "Process": "Mandatory attending clinician two-step override review and escalation SOP.",
                    "Training": "Annual clinical override decision-making refresher module.",
                },
                "human_roles": ["Attending Clinician", "Operations Lead"],
                "software_module": "allergy_override_engine",
            },
        ]
    }
    return [json.dumps(batch_1), json.dumps(batch_2)]


# ---------------------------------------------------------------------------
# 1. Models & Validation
# ---------------------------------------------------------------------------


class TestSynthesisModels:
    def test_triage_track_conversions(self) -> None:
        assert TriageTrack.SOFTWARE.to_routing_kind() == RoutingDecisionKind.SOFTWARE
        assert TriageTrack.PROCESS.to_routing_kind() == RoutingDecisionKind.PROCESS
        assert TriageTrack.TRAINING.to_routing_kind() == RoutingDecisionKind.TRAINING
        assert TriageTrack.HYBRID.to_routing_kind() == RoutingDecisionKind.HYBRID
        for kind in RoutingDecisionKind:
            assert TriageTrack.from_routing_kind(kind).to_routing_kind() == kind

    def test_triage_item_strict_validation_and_normalization(self) -> None:
        item = TriageItem(
            req_id=" REQ-010 ",
            track=TriageTrack.HYBRID,
            rationale=" Needs both code and SOP ",
            rejected_alternatives=["  manual only ", ""],
            prerequisites=[" SOP approved ", "   "],
            component_breakdown={
                "software": "Pure validator",  # type: ignore[dict-item]
                TriageTrack.PROCESS: "Manual escalation",  # type: ignore[dict-item]
            },
            human_roles=[" Ops Lead "],
            software_module=" validator_core ",
        )
        assert item.req_id == "REQ-010"
        assert item.rejected_alternatives == ["manual only"]
        assert item.prerequisites == ["SOP approved"]
        assert item.human_roles == ["Ops Lead"]
        assert item.software_module == "validator_core"
        assert set(item.component_breakdown.keys()) == {
            RoutingDecisionKind.SOFTWARE,
            RoutingDecisionKind.PROCESS,
        }

        with pytest.raises(ValidationError):
            TriageItem(
                req_id="REQ-011",
                track=TriageTrack.SOFTWARE,
                rationale="ok",
                unknown_field="forbidden",  # type: ignore[call-arg]
            )

        with pytest.raises(ValidationError):
            TriageItem(req_id="   ", track=TriageTrack.SOFTWARE, rationale="ok")

        with pytest.raises(ValidationError):
            TriageItem(
                req_id="REQ-012",
                track=TriageTrack.HYBRID,
                rationale="bad breakdown",
                component_breakdown={"hybrid": "not concrete"},  # type: ignore[dict-item]
            )

        with pytest.raises(ValidationError):
            TriageItem(
                req_id="REQ-013",
                track=TriageTrack.HYBRID,
                rationale="only one track",
                component_breakdown={"software": "only software"},  # type: ignore[dict-item]
            )

        with pytest.raises(ValidationError):
            TriageItem(
                req_id="REQ-014",
                track=TriageTrack.SOFTWARE,
                rationale="bad key",
                component_breakdown={"bogus": "desc"},  # type: ignore[dict-item]
            )

        with pytest.raises(ValidationError):
            TriageItem(
                req_id="REQ-015",
                track=TriageTrack.SOFTWARE,
                rationale="blank desc",
                component_breakdown={"software": "   "},  # type: ignore[dict-item]
            )

        # None component_breakdown normalizes cleanly to {}
        item_none_bd = TriageItem(
            req_id="REQ-017",
            track=TriageTrack.SOFTWARE,
            rationale="none breakdown",
            component_breakdown=None,  # type: ignore[arg-type]
        )
        assert item_none_bd.component_breakdown == {}

        with pytest.raises(ValidationError):
            TriageItem(
                req_id="REQ-016",
                track=TriageTrack.SOFTWARE,
                rationale="not dict",
                component_breakdown=["bad"],  # type: ignore[arg-type]
            )

    def test_triage_plan_helpers_and_uniqueness(self) -> None:
        item1 = TriageItem(req_id="REQ-1", track=TriageTrack.SOFTWARE, rationale="sw")
        item2 = TriageItem(req_id="REQ-2", track=TriageTrack.PROCESS, rationale="proc")
        plan = TriagePlan(session_id="sess-1", items=[item1, item2])
        assert plan.by_track(TriageTrack.SOFTWARE) == [item1]
        assert plan.by_track(TriageTrack.TRAINING) == []
        assert plan.for_req("REQ-2") == item2
        assert plan.for_req("REQ-999") is None

        with pytest.raises(ValidationError):
            TriagePlan(session_id="sess-1", items=[item1, item1])
        with pytest.raises(ValidationError):
            TriagePlan(session_id="   ", items=[])

    def test_traceability_and_bundle_models_validation(self) -> None:
        row = TraceabilityRow(
            req_id="REQ-1",
            source_sha256=_SHA_A.upper(),
            span="L1-L5",
            track="software",
            adr_ids=["ADR-R001"],
        )
        assert row.source_sha256 == _SHA_A

        matrix = TraceabilityMatrix(session_id="s1", rows=[row], orphaned_req_ids=[])
        assert matrix.is_complete() is True

        matrix_orphan = TraceabilityMatrix(
            session_id="s1", rows=[row], orphaned_req_ids=["REQ-2"]
        )
        assert matrix_orphan.is_complete() is False

        with pytest.raises(ValidationError):
            TraceabilityRow(
                req_id="REQ-1",
                source_sha256="not-a-sha",
                track="software",
            )
        with pytest.raises(ValidationError, match="64-character hex SHA-256"):
            TraceabilityRow(
                req_id="REQ-1",
                source_sha256="z" * 64,
                track="software",
            )
        with pytest.raises(ValidationError):
            TraceabilityRow(req_id="   ", source_sha256=_SHA_A, track="software")
        with pytest.raises(ValidationError):
            TraceabilityMatrix(session_id="  ", rows=[], orphaned_req_ids=[])
        with pytest.raises(ValidationError):
            EmittedADRFile(
                adr_id="ADR-R001",
                kind="routing",
                filename="ADR-R001.md",
                markdown="   ",
            )
        with pytest.raises(ValidationError):
            SynthesisBundleResult(
                session_id="  ",
                out_dir=Path("out"),
                design_md=Path("out/Design.md"),
                process_training_md=Path("out/Process_and_Training.md"),
                traceability_matrix_json=Path("out/traceability_matrix.json"),
            )


# ---------------------------------------------------------------------------
# 2. Pure-Python YAML Frontmatter Round-Trip
# ---------------------------------------------------------------------------


class TestPurePythonYamlFrontmatter:
    def test_round_trip_complex_types_and_edge_scalars(self) -> None:
        payload: dict[str, Any] = {
            "adr_id": "ADR-R001",
            "type": "routing",
            "title": "Route REQ-001: Validate \"Quotes\" & Colons",
            "user_confirmed": False,
            "cloud_verified": True,
            "retries": 3,
            "score": 0.95,
            "optional_ref": None,
            "source_sha256": _SHA_DIGITS_ONLY,
            "reserved_word": "true",
            "empty_str": "",
            "padded_str": " padded value ",
            "leading_dash": "- item-like",
            "multiline": "line1\nline2",
            "empty_list": [],
            "empty_dict": {},
            "list_with_empty_and_nested_dict": [{}, {"nested_list": [1, 2], "nested_map": {"a": 1}}],
            "tags": ["alpha", "beta: gamma", "123"],
            "component_breakdown": {
                "Software": "Pure domain state machine",
                "Process": "Manual approval: 2-person rule",
            },
            "traceability": [
                {
                    "source_sha256": _SHA_DIGITS_ONLY,
                    "req_id": "REQ-001",
                    "adr_id": "ADR-R001",
                    "alloy_model_ref": "core_domain::Prescription",
                    "pbt_spec_id": None,
                },
                {
                    "source_sha256": _SHA_A,
                    "req_id": "REQ-001",
                    "adr_id": "ADR-R001",
                    "alloy_model_ref": None,
                    "pbt_spec_id": "PBT-001",
                },
            ],
        }
        yaml_block = dump_yaml_frontmatter(payload)
        assert "\r\n" not in yaml_block
        markdown = yaml_block + "\n# Heading\n\nBody text.\n"
        parsed, body = parse_emitted_adr(markdown)
        assert parsed == payload
        assert isinstance(parsed["source_sha256"], str)
        assert parsed["user_confirmed"] is False
        assert parsed["cloud_verified"] is True
        assert body.startswith("\n# Heading")

    def test_parse_yaml_single_quotes_inline_containers_and_comments(self) -> None:
        yaml_text = (
            "# Comment line\n"
            "title: 'It''s quoted: safely'\n"
            "\"quoted:key\": \"val\\\"escaped\"\n"
            "\"escaped\\\"key\": ok_escaped\n"
            "'single:key': ok\n"
            "items: [\"a\", \"b\"]\n"
            "meta: {\"k\": \"v\"}\n"
            "empty_key:\n"
            "null_list:\n"
            "  -\n"
            "  - a\n"
            "empty_first_map_list:\n"
            "  - empty_first:\n"
            "  - next_key: 1\n"
            "nested_list:\n"
            "  - simple\n"
            "  - 'single'\n"
            "  -\n"
            "    nested_k: nested_v\n"
            "  - outer:\n"
            "      inner: 42\n"
            "    sibling:\n"
            "      deep: true\n"
            "    empty_sub:\n"
        )
        parsed = parse_yaml_frontmatter(yaml_text)
        assert parsed["title"] == "It's quoted: safely"
        assert parsed["quoted:key"] == 'val"escaped'
        assert parsed["escaped\"key"] == "ok_escaped"
        assert parsed["single:key"] == "ok"
        assert parsed["items"] == ["a", "b"]
        assert parsed["meta"] == {"k": "v"}
        assert parsed["empty_key"] is None
        assert parsed["null_list"] == [None, "a"]
        assert parsed["empty_first_map_list"] == [{"empty_first": None}, {"next_key": 1}]
        assert parsed["nested_list"] == [
            "simple",
            "single",
            {"nested_k": "nested_v"},
            {"outer": {"inner": 42}, "sibling": {"deep": True}, "empty_sub": None},
        ]
        assert parse_yaml_frontmatter("   \n# only comment\n") == {}

    def test_parse_emitted_adr_errors(self) -> None:
        with pytest.raises(ValueError, match="must begin with"):
            parse_emitted_adr("# No frontmatter here")
        with pytest.raises(ValueError, match="missing closing"):
            parse_emitted_adr("---\nadr_id: ADR-R001\n")
        with pytest.raises(ValueError, match="Tabs are not permitted"):
            parse_yaml_frontmatter("a:\n\tb: 1\n")
        with pytest.raises(ValueError, match="Expected 'key: value'"):
            parse_yaml_frontmatter("just_a_bare_line_without_colon\n")
        with pytest.raises(ValueError, match="Empty YAML key"):
            parse_yaml_frontmatter(": value\n")
        with pytest.raises(ValueError, match="Invalid double-quoted"):
            parse_yaml_frontmatter('bad: "unterminated\n')
        with pytest.raises(ValueError, match="Unterminated single-quoted"):
            parse_yaml_frontmatter("bad: 'unterminated\n")
        with pytest.raises(ValueError, match="Invalid inline YAML list"):
            parse_yaml_frontmatter("bad: [1, 2,\n")
        with pytest.raises(ValueError, match="Invalid inline YAML list"):
            parse_yaml_frontmatter("bad: [invalid]\n")
        with pytest.raises(ValueError, match="Invalid inline YAML mapping"):
            parse_yaml_frontmatter("bad: {a: 1}\n")
        with pytest.raises(ValueError, match="Invalid inline YAML mapping"):
            parse_yaml_frontmatter("bad: {unclosed\n")
        with pytest.raises(ValueError, match="Unexpected mapping indentation"):
            parse_yaml_frontmatter("a: 1\n  b: 2\n")
        with pytest.raises(ValueError, match="Unexpected list indentation"):
            parse_yaml_frontmatter("items:\n  - a\n    - b\n")
        with pytest.raises(ValueError, match="Unexpected mapping indentation"):
            parse_yaml_frontmatter("items:\n  - a\n  other: 1\n")
        with pytest.raises(ValueError, match="Unexpected indentation in list-item mapping"):
            parse_yaml_frontmatter("items:\n  - a: 1\n      b: 2\n")
        with pytest.raises(ValueError, match="Unparsed trailing"):
            parse_yaml_frontmatter("a: 1\n- stray_list_item\n")
        with pytest.raises(ValueError, match="Unparsed trailing"):
            parse_yaml_frontmatter("  a: 1\nb: 2\n")

        fm, body = parse_emitted_adr("---\nadr_id: ADR-R001\n---")
        assert fm == {"adr_id": "ADR-R001"}
        assert body == ""


# ---------------------------------------------------------------------------
# 3. Socio-Technical Triager (MockProvider + TIER_0 Batching + Fallbacks)
# ---------------------------------------------------------------------------


class TestSocioTechnicalTriager:
    def test_triage_all_four_tracks_sequential_tier0_batches(self) -> None:
        graph = _make_four_track_graph()
        mock = MockProvider(responses=_scripted_triage_batches())
        router = ProviderRouter(local_provider=mock, strict_local=True, enable_cache=False)

        triager = SocioTechnicalTriager(router, batch_size=2, max_prompt_chars=4000)
        plan = triager.triage(graph, session_id="sess-clinic-01")

        # 4 active requirements (REQ-005 deprecated & REQ-006 superseded skipped)
        assert [item.req_id for item in plan.items] == [
            "REQ-001",
            "REQ-002",
            "REQ-003",
            "REQ-004",
        ]
        assert len(mock.calls) == 2
        for call in mock.calls:
            assert len(call.prompt) < 6000
            assert call.response_schema is not None

        assert len(plan.by_track(TriageTrack.SOFTWARE)) == 1
        assert len(plan.by_track(TriageTrack.PROCESS)) == 1
        assert len(plan.by_track(TriageTrack.TRAINING)) == 1
        assert len(plan.by_track(TriageTrack.HYBRID)) == 1

        hybrid = plan.for_req("REQ-004")
        assert hybrid is not None
        assert hybrid.software_module == "allergy_override_engine"
        assert set(hybrid.component_breakdown.keys()) == {
            RoutingDecisionKind.SOFTWARE,
            RoutingDecisionKind.PROCESS,
            RoutingDecisionKind.TRAINING,
        }
        assert len(hybrid.rejected_alternatives) == 2
        assert len(hybrid.prerequisites) == 2

    def test_triage_heuristic_fallback_when_mock_returns_generic_json(self) -> None:
        graph = _make_four_track_graph()
        # Add a pure Process + Training hybrid requirement to exercise that branch too
        graph = graph.model_copy(
            update={
                "nodes": [
                    *graph.nodes,
                    RequirementNode(
                        req_id="REQ-007",
                        title="Shift Handover Policy and Staff Workshop",
                        statement="Nurses shall follow shift handover SOP and complete onboarding workshop.",
                        kind=RequirementKind.PROCESS,
                        status=RequirementStatus.PROPOSED,
                    ),
                ]
            }
        )
        # Default MockProvider returns '{"status": "ok"}', which fails schema validation
        # and degrades cleanly in strict_local mode -> triager falls back deterministically.
        mock = MockProvider()
        router = ProviderRouter(
            local_provider=mock,
            strict_local=True,
            max_local_retries=0,
            enable_cache=False,
        )
        triager = SocioTechnicalTriager(router, batch_size=3)
        plan = triager.triage(graph, session_id="sess-fallback")

        assert plan.for_req("REQ-001").track == TriageTrack.SOFTWARE  # type: ignore[union-attr]
        assert plan.for_req("REQ-002").track == TriageTrack.PROCESS  # type: ignore[union-attr]
        assert plan.for_req("REQ-003").track == TriageTrack.TRAINING  # type: ignore[union-attr]
        assert plan.for_req("REQ-004").track == TriageTrack.HYBRID  # type: ignore[union-attr]
        assert plan.for_req("REQ-007").track == TriageTrack.HYBRID  # type: ignore[union-attr]
        assert plan.for_req("REQ-005") is None
        assert plan.for_req("REQ-006") is None

    def test_triage_normalizes_minimal_llm_entries_and_handles_edge_graphs(self) -> None:
        # Test normalization when LLM omits optional defaults (software_module, roles, hybrid breakdown)
        minimal_payload = json.dumps(
            {
                "items": [
                    {
                        "req_id": "REQ-001",
                        "track": "software",
                        "rationale": "Core logic",
                    },
                    {
                        "req_id": "REQ-002",
                        "track": "process",
                        "rationale": "Manual governance",
                    },
                    {
                        "req_id": "REQ-003",
                        "track": "training",
                        "rationale": "Enablement",
                    },
                    {
                        "req_id": "REQ-004",
                        "track": "hybrid",
                        "rationale": "Split needed",
                        "component_breakdown": {"Hybrid": "ignored"},
                    },
                ]
            }
        )
        graph = _make_four_track_graph()
        mock = MockProvider(responses=[minimal_payload])
        router = ProviderRouter(local_provider=mock, strict_local=True, enable_cache=False)
        triager = SocioTechnicalTriager(router, batch_size=10)
        plan = triager.triage(graph, session_id="sess-norm")

        assert plan.for_req("REQ-001").software_module != ""  # type: ignore[union-attr]
        assert plan.for_req("REQ-002").human_roles == ["Operations Lead"]  # type: ignore[union-attr]
        assert plan.for_req("REQ-003").prerequisites  # type: ignore[union-attr]
        hybrid = plan.for_req("REQ-004")
        assert hybrid is not None
        assert len(hybrid.component_breakdown) >= 2
        assert hybrid.software_module != ""

        # Hybrid LLM response with >=2 concrete breakdown entries (including Software) but blank software_module
        hybrid_no_mod_payload = json.dumps(
            {
                "items": [
                    {
                        "req_id": "REQ-004",
                        "track": "hybrid",
                        "rationale": "Split with explicit breakdown but omitted module name.",
                        "component_breakdown": {
                            "Software": "Automated alert validator.",
                            "Process": "Clinician sign-off SOP.",
                        },
                        "software_module": "",
                    }
                ]
            }
        )
        single_hybrid_graph = RequirementGraph(
            graph_id="g-hybrid-nomod",
            nodes=[graph.get_node("REQ-004")],  # type: ignore[list-item]
        )
        hybrid_nomod_router = ProviderRouter(
            local_provider=MockProvider(responses=[hybrid_no_mod_payload]),
            strict_local=True,
            enable_cache=False,
        )
        hybrid_nomod_plan = SocioTechnicalTriager(hybrid_nomod_router).triage(
            single_hybrid_graph, session_id="sess-nomod"
        )
        assert hybrid_nomod_plan.for_req("REQ-004").software_module != ""  # type: ignore[union-attr]

        # Empty active graph
        empty_graph = RequirementGraph(graph_id="g-empty", nodes=[])
        empty_plan = triager.triage(empty_graph, session_id="sess-empty")
        assert empty_plan.items == []

        # Cyclic graph + digit-leading title + char-budget batch splitting
        cyclic_graph = RequirementGraph(
            graph_id="g-cyclic",
            nodes=[
                RequirementNode(
                    req_id="REQ-B",
                    title="2FA Token Validation",
                    statement="The API shall validate 2FA tokens within 50ms. " + ("detail " * 30),
                ),
                RequirementNode(
                    req_id="REQ-A",
                    title="3DS Verification",
                    statement="The service shall validate 3DS challenges. " + ("detail " * 30),
                ),
            ],
            edges=[
                RequirementEdge(source_id="REQ-A", target_id="REQ-B", relation=EdgeKind.DEPENDS_ON),
                RequirementEdge(source_id="REQ-B", target_id="REQ-A", relation=EdgeKind.DEPENDS_ON),
            ],
        )
        char_router = ProviderRouter(
            local_provider=MockProvider(),
            strict_local=True,
            max_local_retries=0,
            enable_cache=False,
        )
        char_triager = SocioTechnicalTriager(char_router, batch_size=10, max_prompt_chars=520)
        cyclic_plan = char_triager.triage(cyclic_graph, session_id="sess-cyclic")
        assert [i.req_id for i in cyclic_plan.items] == ["REQ-A", "REQ-B"]
        assert cyclic_plan.for_req("REQ-B").software_module.startswith("mod_2fa")  # type: ignore[union-attr]

        # Constructor guards
        with pytest.raises(ValueError, match="batch_size"):
            SocioTechnicalTriager(router, batch_size=0)
        with pytest.raises(ValueError, match="max_prompt_chars"):
            SocioTechnicalTriager(router, max_prompt_chars=100)


# ---------------------------------------------------------------------------
# 4. Routing & Assumption ADR Emitters + Strict Round-Trip Validation
# ---------------------------------------------------------------------------


class TestRoutingAndAssumptionADRs:
    def test_routing_adrs_round_trip_pydantic_validation(self) -> None:
        graph = _make_four_track_graph()
        mock = MockProvider(responses=_scripted_triage_batches())
        router = ProviderRouter(local_provider=mock, strict_local=True, enable_cache=False)
        plan = SocioTechnicalTriager(router, batch_size=2).triage(graph, session_id="sess-adr")

        alloy_refs = {
            "REQ-001": ["core_domain::Prescription", "behavior::DoseValid"],
            "REQ-004": ["core_domain::AllergyAlert", "behavior::OverrideSafe"],
        }
        pbt_refs = {
            "REQ-001": ["PBT-DOSE-001"],
            "REQ-004": ["PBT-ALLERGY-004"],
        }

        emitted = build_routing_adrs(
            graph, plan, alloy_refs=alloy_refs, pbt_refs=pbt_refs
        )
        assert [e.filename for e in emitted] == [
            "ADR-R001.md",
            "ADR-R002.md",
            "ADR-R003.md",
            "ADR-R004.md",
        ]

        for adr_file in emitted:
            assert adr_file.kind == "routing"
            assert adr_file.routing_adr is not None
            parsed_fm, body = parse_emitted_adr(adr_file.markdown)
            assert parsed_fm["type"] == "routing"
            assert parsed_fm["adr_id"] == adr_file.adr_id
            assert len(parsed_fm["source_sha256"]) == 64
            assert parsed_fm["span"]
            assert parsed_fm["requirement_ids"] == adr_file.routing_adr.req_ids
            assert "## Traceability Chain" in body

            round_tripped: RoutingADR = validate_routing_adr_markdown(adr_file.markdown)
            assert round_tripped == adr_file.routing_adr
            assert len(round_tripped.traceability) >= 1
            assert round_tripped.traceability[0].adr_id == adr_file.adr_id

        # Check specific Alloy & PBT refs round-tripped cleanly
        r1 = emitted[0]
        assert r1.frontmatter["alloy_refs"] == [
            "core_domain::Prescription",
            "behavior::DoseValid",
        ]
        assert r1.frontmatter["pbt_refs"] == ["PBT-DOSE-001"]
        assert r1.routing_adr is not None
        assert r1.routing_adr.traceability[0].is_complete() is True

    def test_routing_adr_unlinked_node_and_error_paths(self) -> None:
        graph = RequirementGraph(
            graph_id="g-unlinked",
            nodes=[
                RequirementNode(
                    req_id="REQ-100",
                    title="Simple Rule",
                    statement="System shall log requests.",
                )
            ],
        )
        plan = TriagePlan(
            session_id="s-unlinked",
            items=[
                TriageItem(
                    req_id="REQ-100",
                    track=TriageTrack.SOFTWARE,
                    rationale="Pure logging logic.",
                    software_module="request_logger",
                )
            ],
        )
        adrs = build_routing_adrs(graph, plan)
        assert len(adrs) == 1
        assert adrs[0].frontmatter["span"] == "synthetic:REQ-100"
        assert len(adrs[0].frontmatter["source_sha256"]) == 64

        bad_plan = TriagePlan(
            session_id="s-bad",
            items=[
                TriageItem(
                    req_id="REQ-MISSING",
                    track=TriageTrack.SOFTWARE,
                    rationale="Missing from graph.",
                )
            ],
        )
        with pytest.raises(ValueError, match="does not exist in RequirementGraph"):
            build_routing_adrs(graph, bad_plan)

        # Missing required traceability fields & malformed routing frontmatter
        bad_md = dump_yaml_frontmatter({"adr_id": "ADR-R001", "type": "routing"}) + "\n# X\n"
        with pytest.raises(ValueError, match="missing required traceability field"):
            validate_routing_adr_markdown(bad_md)

        base_fm: dict[str, Any] = {
            "adr_id": "ADR-R001",
            "type": "routing",
            "title": "Route",
            "decision": "Software",
            "rationale": "ok",
            "source_sha256": _SHA_A,
            "span": "L1",
            "requirement_ids": [],
            "alloy_refs": [],
            "pbt_refs": [],
        }
        with pytest.raises(ValueError, match="requirement_ids"):
            validate_routing_adr_markdown(dump_yaml_frontmatter(base_fm) + "\n# X\n")

        bad_breakdown_fm = {**base_fm, "requirement_ids": ["REQ-1"], "component_breakdown": ["bad"]}
        with pytest.raises(ValueError, match="component_breakdown"):
            validate_routing_adr_markdown(dump_yaml_frontmatter(bad_breakdown_fm) + "\n# X\n")

        bad_trace_fm = {**base_fm, "requirement_ids": ["REQ-1"], "traceability": {"bad": 1}}
        with pytest.raises(ValueError, match="traceability"):
            validate_routing_adr_markdown(dump_yaml_frontmatter(bad_trace_fm) + "\n# X\n")

    def test_assumption_adrs_duck_typed_and_dict_inputs(self) -> None:
        duck_assumption = SimpleNamespace(
            assumption_id="ADR-A001",
            title="Default Override Timeout 30 Minutes",
            statement="Unacknowledged clinical overrides escalate after 30 minutes.",
            req_ids=["REQ-002", "REQ-004"],
            finding_ids=["FND-001"],
            source_sha256=_SHA_B,
            rationale="Standard hospital SLA default accepted via --accept-defaults.",
            type="assumption",
            user_confirmed=False,
        )
        assert isinstance(duck_assumption, AssumptionLike)

        dict_assumption = {
            "assumption_id": "",  # triggers monotonic assignment
            "title": "Annual Training Refresher Window",
            "statement": "Staff have a 14-day grace period for annual recertification.",
            "req_ids": ["REQ-003"],
            "finding_ids": ["FND-002"],
            "source_sha256": "",  # deterministic fallback SHA-256
            "rationale": "",
        }
        dup_id_assumption = {
            "assumption_id": "ADR-A001",  # duplicate -> re-numbered cleanly
            "title": "Audit Log Retention 7 Years",
            "statement": "Operational audit logs are retained for 7 years.",
            "req_ids": ["REQ-002"],
            "finding_ids": [],
            "source_sha256": _SHA_A,
            "rationale": "HIPAA baseline.",
        }

        emitted = build_assumption_adrs(
            [duck_assumption, dict_assumption, dup_id_assumption]
        )
        assert [e.adr_id for e in emitted] == ["ADR-A001", "ADR-A002", "ADR-A003"]
        for adr_file in emitted:
            assert adr_file.kind == "assumption"
            assert adr_file.routing_adr is None
            fm, body = parse_emitted_adr(adr_file.markdown)
            assert fm["type"] == "assumption"
            assert fm["user_confirmed"] is False
            assert len(fm["source_sha256"]) == 64
            assert "UNCONFIRMED ASSUMPTION" in body
            # Must NOT validate as a routing ADR
            with pytest.raises(ValueError, match="type='routing'"):
                validate_routing_adr_markdown(adr_file.markdown)

        # Duplicate ID collision where f"ADR-A{idx:03d}" is already taken
        colliding = build_assumption_adrs(
            [
                {"assumption_id": "ADR-A002", "title": "First", "statement": "One."},
                {"assumption_id": "ADR-A002", "title": "Second", "statement": "Two."},
            ]
        )
        assert [c.adr_id for c in colliding] == ["ADR-A002", "ADR-A003"]

        # Tampered assumption ADR markdown fails validate_assumption_adr_markdown
        tampered = dump_yaml_frontmatter(
            {"adr_id": "ADR-A001", "type": "assumption", "user_confirmed": True}
        ) + "\n# Tampered\n"
        with pytest.raises(ValueError, match="type='assumption' and user_confirmed=false"):
            validate_assumption_adr_markdown(tampered)

        # Reject user_confirmed=True on assumptions
        with pytest.raises(ValueError, match="user_confirmed=False"):
            build_assumption_adrs(
                [{"title": "Bad", "statement": "Confirmed", "user_confirmed": True}]
            )
        with pytest.raises(ValueError, match="non-blank title"):
            build_assumption_adrs([{"title": " ", "statement": "x"}])
        with pytest.raises(ValueError, match="non-blank statement"):
            build_assumption_adrs([{"title": "x", "statement": " "}])
        with pytest.raises(ValueError, match="invalid source_sha256"):
            build_assumption_adrs(
                [{"title": "x", "statement": "y", "source_sha256": "short"}]
            )


# ---------------------------------------------------------------------------
# 5. Design.md & Process_and_Training.md Emitters
# ---------------------------------------------------------------------------


def _extract_mermaid_blocks(markdown: str) -> list[str]:
    return re.findall(r"```mermaid\n(.*?)\n```", markdown, flags=re.DOTALL)


class TestDesignAndProcessTrainingEmitters:
    def test_design_md_functional_core_imperative_shell_and_mermaid(self) -> None:
        graph = _make_four_track_graph()
        mock = MockProvider(responses=_scripted_triage_batches())
        router = ProviderRouter(local_provider=mock, strict_local=True, enable_cache=False)
        plan = SocioTechnicalTriager(router, batch_size=2).triage(graph, session_id="sess-des")
        r_adrs = build_routing_adrs(graph, plan)
        a_adrs = build_assumption_adrs(
            [
                {
                    "assumption_id": "ADR-A001",
                    "title": "Default SLA",
                    "statement": "30 minute escalation SLA.",
                    "req_ids": ["REQ-002"],
                    "finding_ids": ["F-1"],
                    "source_sha256": _SHA_B,
                    "rationale": "Default",
                }
            ]
        )

        design_md = emit_design_md(graph, plan, [*r_adrs, *a_adrs])
        assert "Functional Core, Imperative Shell" in design_md
        assert "Phase 3 PBT Targets" in design_md
        assert "Imperative Shell Adapters" in design_md
        assert "Module Interface Contracts" in design_md
        assert "Socio-Technical Boundaries (Process & Training Exclusions)" in design_md
        assert "Unconfirmed Assumptions Requiring Sign-Off" in design_md
        assert "prescription_validator" in design_md
        assert "allergy_override_engine" in design_md
        assert "REQ-002" in design_md
        assert "REQ-003" in design_md
        assert "ADR-A001" in design_md

        blocks = _extract_mermaid_blocks(design_md)
        assert len(blocks) == 2
        flowchart, sequence = blocks
        assert flowchart.splitlines()[0].strip() == "flowchart TD"
        assert "Imperative Shell" in flowchart
        assert "Functional Core" in flowchart
        assert "Socio-Technical Boundary" in flowchart
        assert sequence.splitlines()[0].strip() == "sequenceDiagram"
        assert "participant Shell" in sequence
        assert "participant Core" in sequence
        assert "HumanActor" in sequence

    def test_design_md_pure_software_and_pure_non_software_edge_cases(self) -> None:
        sw_graph = RequirementGraph(
            graph_id="g-sw",
            nodes=[
                RequirementNode(
                    req_id="REQ-1", title="Pure Calc", statement="Calculate totals."
                )
            ],
        )
        sw_plan = TriagePlan(
            session_id="s-sw",
            items=[
                TriageItem(
                    req_id="REQ-1",
                    track=TriageTrack.SOFTWARE,
                    rationale="Pure math.",
                    software_module="calc_core",
                )
            ],
        )
        sw_design = emit_design_md(sw_graph, sw_plan, build_routing_adrs(sw_graph, sw_plan))
        assert "no Process or Training exclusions apply" in sw_design
        assert len(_extract_mermaid_blocks(sw_design)) == 2

        proc_graph = RequirementGraph(
            graph_id="g-proc",
            nodes=[
                RequirementNode(
                    req_id="REQ-2", title="Manual Audit", statement="Monthly audit review."
                )
            ],
        )
        proc_plan = TriagePlan(
            session_id="s-proc",
            items=[
                TriageItem(
                    req_id="REQ-2",
                    track=TriageTrack.PROCESS,
                    rationale="Human governance.",
                    human_roles=["Compliance Officer"],
                )
            ],
        )
        proc_design = emit_design_md(
            proc_graph, proc_plan, build_routing_adrs(proc_graph, proc_plan)
        )
        assert "No pure software modules were routed" in proc_design
        assert len(_extract_mermaid_blocks(proc_design)) == 2

        # Mermaid ID sanitizer edge cases + Hybrid without breakdown + plan item absent from graph
        assert _safe_mermaid_id("!!!") == "node"
        assert _safe_mermaid_id("2fa_auth") == "n_2fa_auth"

        ghost_plan = TriagePlan(
            session_id="s-ghost",
            items=[
                TriageItem(
                    req_id="REQ-GHOST",
                    track=TriageTrack.HYBRID,
                    rationale="Hybrid with empty breakdown and missing graph node.",
                    software_module="ghost_core",
                    human_roles=["Ops Lead"],
                )
            ],
        )
        ghost_design = emit_design_md(RequirementGraph(graph_id="g-empty", nodes=[]), ghost_plan, [])
        assert "ghost_core" in ghost_design
        assert "REQ-GHOST" in ghost_design

    def test_process_and_training_md_sops_and_role_outlines(self) -> None:
        graph = _make_four_track_graph()
        mock = MockProvider(responses=_scripted_triage_batches())
        router = ProviderRouter(local_provider=mock, strict_local=True, enable_cache=False)
        plan = SocioTechnicalTriager(router, batch_size=2).triage(graph, session_id="sess-pt")
        r_adrs = build_routing_adrs(graph, plan)
        a_adrs = build_assumption_adrs(
            [
                {
                    "assumption_id": "ADR-A001",
                    "title": "Grace Period",
                    "statement": "14 day refresher window.",
                    "req_ids": ["REQ-003"],
                    "finding_ids": ["F-2"],
                    "source_sha256": _SHA_B,
                    "rationale": "Default",
                }
            ]
        )

        pt_md = emit_process_and_training_md(graph, plan, [*r_adrs, *a_adrs])
        assert "## Operational SOP Workflows" in pt_md
        assert "### SOP-001:" in pt_md
        assert "### SOP-002:" in pt_md
        assert "**Trigger**:" in pt_md
        assert "**Responsible Human Role**:" in pt_md
        assert "#### Checklist Steps" in pt_md
        assert "**SLA / Escalation Path**:" in pt_md
        assert "**Software Handoff Point**:" in pt_md
        assert "allergy_override_engine" in pt_md

        assert "## Role-Based End-User Training Outlines" in pt_md
        assert "### Target Role Matrix" in pt_md
        assert "### TRN-001:" in pt_md
        assert "### TRN-002:" in pt_md
        assert "**Target Role**:" in pt_md
        assert "#### Prerequisites" in pt_md
        assert "#### Training Modules" in pt_md
        assert "#### Go-Live Sign-Off Criteria" in pt_md
        assert "ADR-A001" in pt_md

        # Pure-software fallback path
        sw_only_plan = TriagePlan(
            session_id="s-sw-only",
            items=[plan.for_req("REQ-001")],  # type: ignore[list-item]
        )
        sw_pt_md = emit_process_and_training_md(graph, sw_only_plan, [r_adrs[0]])
        assert "No pure Process or Hybrid-Process SOP workflows" in sw_pt_md
        assert "No pure Training or Hybrid-Training outlines" in sw_pt_md

        # SOP/Training fallback branches: empty acceptance_criteria, missing graph node, and Hybrid without breakdown
        minimal_graph = RequirementGraph(
            graph_id="g-min-pt",
            nodes=[
                RequirementNode(
                    req_id="REQ-P",
                    title="Bare Process",
                    statement="Perform monthly manual reconciliation.",
                    kind=RequirementKind.PROCESS,
                ),
                RequirementNode(
                    req_id="REQ-T",
                    title="Bare Training",
                    statement="Complete security awareness briefing.",
                ),
            ],
        )
        minimal_plan = TriagePlan(
            session_id="s-min-pt",
            items=[
                TriageItem(
                    req_id="REQ-P",
                    track=TriageTrack.PROCESS,
                    rationale="Manual reconciliation.",
                ),
                TriageItem(
                    req_id="REQ-T",
                    track=TriageTrack.TRAINING,
                    rationale="Security briefing.",
                ),
                TriageItem(
                    req_id="REQ-MISSING-NODE",
                    track=TriageTrack.HYBRID,
                    rationale="Hybrid with empty breakdown and absent graph node.",
                ),
            ],
        )
        min_pt_md = emit_process_and_training_md(minimal_graph, minimal_plan, [])
        assert "Perform monthly manual reconciliation." in min_pt_md
        assert "Execute documented operational procedure for `REQ-MISSING-NODE`." in min_pt_md
        assert "Module 3 — Practical Assessment & Escalation Drill" in min_pt_md


# ---------------------------------------------------------------------------
# 6. Full Synthesis Bundle Writer, Traceability Completeness & Orphan Detection
# ---------------------------------------------------------------------------


class TestSynthesisBundleAndTraceability:
    def test_write_full_bundle_zero_orphans_and_round_trip(self, tmp_path: Path) -> None:
        graph = _make_four_track_graph()
        mock = MockProvider(responses=_scripted_triage_batches())
        router = ProviderRouter(local_provider=mock, strict_local=True, enable_cache=False)
        plan = SocioTechnicalTriager(router, batch_size=2).triage(
            graph, session_id="sess-bundle-01"
        )
        assumptions = [
            SimpleNamespace(
                assumption_id="ADR-A001",
                title="Default Escalation Timeout",
                statement="Escalate unacknowledged clinical overrides after 30 minutes.",
                req_ids=["REQ-002"],
                finding_ids=["FND-101"],
                source_sha256=_SHA_B,
                rationale="Auto-accepted via finalize --accept-defaults.",
                type="assumption",
                user_confirmed=False,
            )
        ]

        out_dir = tmp_path / "export_bundle"
        result = write_synthesis_bundle(
            out_dir,
            session_id="sess-bundle-01",
            graph=graph,
            plan=plan,
            assumptions=assumptions,
            alloy_refs={"REQ-001": ["core_domain::Prescription"]},
            pbt_refs={"REQ-001": ["PBT-001"]},
            spec_markdown="# Verified Spec\n\n```alloy module=core_domain\nsig Rx {}\n```\n",
            raise_on_orphans=True,
        )

        assert result.orphaned_req_ids == []
        assert result.design_md.is_file()
        assert result.process_training_md.is_file()
        assert result.traceability_matrix_json.is_file()
        assert result.spec_md is not None and result.spec_md.is_file()

        adr_names = sorted(p.name for p in result.adr_files)
        assert adr_names == [
            "ADR-A001.md",
            "ADR-R001.md",
            "ADR-R002.md",
            "ADR-R003.md",
            "ADR-R004.md",
        ]

        # Verify LF line endings and strict round-trip parsing for every written ADR file
        for adr_path in result.adr_files:
            raw_bytes = adr_path.read_bytes()
            assert b"\r\n" not in raw_bytes
            text = raw_bytes.decode("utf-8")
            fm, _ = parse_emitted_adr(text)
            if adr_path.name.startswith("ADR-R"):
                validated = validate_routing_adr_markdown(text)
                assert validated.adr_id == fm["adr_id"]
            else:
                assert fm["type"] == "assumption"
                assert fm["user_confirmed"] is False

        # Verify traceability_matrix.json completeness
        matrix_data = json.loads(result.traceability_matrix_json.read_text(encoding="utf-8"))
        matrix = TraceabilityMatrix.model_validate(matrix_data)
        assert matrix.is_complete() is True
        assert [row.req_id for row in matrix.rows] == [
            "REQ-001",
            "REQ-002",
            "REQ-003",
            "REQ-004",
        ]
        by_req = {row.req_id: row for row in matrix.rows}
        assert by_req["REQ-001"].user_confirmed is True
        assert by_req["REQ-001"].alloy_refs == ["core_domain::Prescription"]
        assert by_req["REQ-001"].pbt_refs == ["PBT-001"]
        # REQ-002 is linked to unconfirmed ADR-A001 -> user_confirmed=False
        assert by_req["REQ-002"].user_confirmed is False
        assert "ADR-A001" in by_req["REQ-002"].adr_ids
        assert "ADR-R002" in by_req["REQ-002"].adr_ids

    def test_orphan_detection_reported_and_strict_mode_raises(self, tmp_path: Path) -> None:
        graph = _make_four_track_graph()
        # Intentionally omit REQ-003 and REQ-004 from TriagePlan
        partial_plan = TriagePlan(
            session_id="sess-orphan",
            items=[
                TriageItem(
                    req_id="REQ-001",
                    track=TriageTrack.SOFTWARE,
                    rationale="Core API",
                    software_module="rx_core",
                ),
                TriageItem(
                    req_id="REQ-002",
                    track=TriageTrack.PROCESS,
                    rationale="Dual signoff SOP",
                    human_roles=["Pharmacist"],
                ),
            ],
        )

        # Direct matrix builder check
        adrs = build_routing_adrs(graph, partial_plan)
        matrix = build_traceability_matrix(
            session_id="sess-orphan", graph=graph, plan=partial_plan, adrs=adrs
        )
        assert matrix.is_complete() is False
        assert matrix.orphaned_req_ids == ["REQ-003", "REQ-004"]

        # Bundle writer reports orphans by default
        res = write_synthesis_bundle(
            tmp_path / "orphan_out",
            session_id="sess-orphan",
            graph=graph,
            plan=partial_plan,
        )
        assert res.orphaned_req_ids == ["REQ-003", "REQ-004"]
        assert res.spec_md is None

        # Strict mode raises ValueError
        with pytest.raises(ValueError, match="orphaned active requirements"):
            write_synthesis_bundle(
                tmp_path / "orphan_strict",
                session_id="sess-orphan",
                graph=graph,
                plan=partial_plan,
                raise_on_orphans=True,
            )

        # Synthetic source_sha256 fallback in bundle traceability matrix when node has no trace_links
        no_trace_graph = RequirementGraph(
            graph_id="g-no-trace",
            nodes=[
                RequirementNode(
                    req_id="REQ-NT",
                    title="Untraced Draft",
                    statement="System shall emit metrics.",
                )
            ],
        )
        no_trace_plan = TriagePlan(
            session_id="sess-nt",
            items=[
                TriageItem(
                    req_id="REQ-NT",
                    track=TriageTrack.SOFTWARE,
                    rationale="Pure metrics emitter.",
                    software_module="metrics_core",
                )
            ],
        )
        nt_res = write_synthesis_bundle(
            tmp_path / "nt_out",
            session_id="sess-nt",
            graph=no_trace_graph,
            plan=no_trace_plan,
            raise_on_orphans=True,
        )
        nt_matrix = TraceabilityMatrix.model_validate(
            json.loads(nt_res.traceability_matrix_json.read_text(encoding="utf-8"))
        )
        assert nt_matrix.rows[0].span == "synthetic:REQ-NT"
        assert len(nt_matrix.rows[0].source_sha256) == 64

    def test_refuses_to_write_into_repo_docs_adr_directory(self, tmp_path: Path) -> None:
        graph = _make_four_track_graph()
        plan = TriagePlan(session_id="sess-guard", items=[])
        forbidden_dir = tmp_path / "docs" / "adr"
        with pytest.raises(ValueError, match="Refusing to write target-system synthesis bundle"):
            write_synthesis_bundle(
                forbidden_dir,
                session_id="sess-guard",
                graph=graph,
                plan=plan,
            )
