"""Comprehensive offline + opt-in live tests for Stage 4b Alloy 6 emitter & verification gate."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

from pydantic import ValidationError
import pytest

from reqhq.alloy import (
    ALLOY6_TEMPORAL_KEYWORDS,
    AlloyAnalyzer,
    AlloyCompatMode,
    AlloyRunResult,
    AlloyRunStatus,
    AlloySpecEmitter,
    AlloyVerificationController,
    AlloyVerificationOutcome,
    ConflictQuestion,
    CounterexamplePrompt,
    ExtractedAlloyBlock,
    HeadlessAlloyRunner,
    MockAlloyAnalyzer,
    build_req_to_alloy_refs,
    extract_alloy_blocks,
    inject_vacuity_witnesses,
    lint_core_domain_version_agnostic,
    map_unsat_to_conflicts,
    parse_alloy_output,
    replace_alloy_block,
    translate_counterexample,
    write_alloy_files,
)
from reqhq.alloy.emitter import _sanitize_alloy_ident
from reqhq.alloy.repair_loop import _extract_repaired_modules, _pick_violated_assertion
from reqhq.llm import (
    EscalationReason,
    MockProvider,
    ProviderError,
    ProviderRouter,
)
from reqhq.schemas.requirements import (
    RequirementGraph,
    RequirementKind,
    RequirementNode,
    RequirementStatus,
    TraceLink,
)
from reqhq.schemas.routing_adr import RoutingADR, RoutingDecisionKind
from reqhq.store.event_log import EventKind, SessionEventLog

_SHA = "a" * 64


def _sample_graph() -> RequirementGraph:
    return RequirementGraph(
        graph_id="expense-approval-v2",
        version="1.2.0",
        nodes=[
            RequirementNode(
                req_id="REQ-001",
                title="Dual approval for high-value expenses",
                statement="Expenses above $5,000 require approval from an authorized finance owner.",
                kind=RequirementKind.FUNCTIONAL,
                status=RequirementStatus.ACCEPTED,
                priority="P0",
                acceptance_criteria=["Approval recorded before disbursement"],
                trace_links=[TraceLink(source_sha256=_SHA, source_uri="prd.md", anchor="L10")],
            ),
            RequirementNode(
                req_id="REQ-002",
                title="Latency and encryption budget",
                statement="Approval API p95 latency <= 200ms with TLS 1.3 enforced.",
                kind=RequirementKind.SECURITY,
                status=RequirementStatus.ACCEPTED,
                priority="P0",
                trace_links=[TraceLink(source_sha256=_SHA, source_uri="prd.md", anchor="L22")],
            ),
            RequirementNode(
                req_id="REQ-003",
                title="Quarterly auditor onboarding workshop",
                statement="All finance reviewers must complete annual compliance training.",
                kind=RequirementKind.PROCESS,
                status=RequirementStatus.ACCEPTED,
                priority="P1",
                trace_links=[TraceLink(source_sha256=_SHA, source_uri="prd.md", anchor="L35")],
            ),
            RequirementNode(
                req_id="REQ-004",
                title="Legacy fax submission",
                statement="Deprecated fax workflow.",
                kind=RequirementKind.INTERFACE,
                status=RequirementStatus.DEPRECATED,
                priority="P3",
            ),
        ],
    )


def _sample_triage_and_adrs() -> tuple[SimpleNamespace, list[SimpleNamespace]]:
    triage_plan = SimpleNamespace(
        session_id="sess-test",
        items=[
            SimpleNamespace(
                req_id="REQ-001",
                track=SimpleNamespace(value="software"),
                rationale="Enforced in core domain state machine.",
                human_roles=["FinanceApprover"],
                prerequisites=[],
                software_module="approvals",
            ),
            SimpleNamespace(
                req_id="REQ-002",
                track="hybrid",
                rationale="TLS in software plus security operations review.",
                human_roles=["SecurityAuditor"],
                prerequisites=["Security sign-off"],
                software_module="gateway",
            ),
            SimpleNamespace(
                req_id="REQ-003",
                track="training",
                rationale="Pure organizational training curriculum.",
                human_roles=["ComplianceTrainer"],
                prerequisites=["LMS module published"],
                software_module="",
            ),
        ],
    )
    routing_adr = RoutingADR(
        adr_id="ADR-R001",
        title="Route compliance workshop to Training track",
        decision=RoutingDecisionKind.TRAINING,
        rationale="Human training cannot be enforced by software alone.",
        req_ids=["REQ-003"],
    )
    adrs = [
        SimpleNamespace(
            adr_id="ADR-R001",
            filename="ADR-R001.md",
            routing_adr=routing_adr,
        ),
        {
            "adr_id": "ADR-R002",
            "filename": "ADR-R002.md",
            "decision": "Software",
            "title": "Enforce dual approval in core engine",
            "frontmatter": {"requirement_ids": ["REQ-001", "REQ-002"]},
        },
    ]
    return triage_plan, adrs


# ---------------------------------------------------------------------------
# 1. Models & schema strictness
# ---------------------------------------------------------------------------


def test_models_enforce_extra_forbid_and_helpers() -> None:
    with pytest.raises(ValidationError):
        ExtractedAlloyBlock(
            module_name="core_domain",
            code="module core_domain",
            start_line=1,
            end_line=1,
            extra_field="boom",  # type: ignore[call-arg]
        )

    with pytest.raises(ValidationError):
        ExtractedAlloyBlock(
            module_name="core_domain",
            code="module core_domain",
            start_line=10,
            end_line=5,
        )

    block = ExtractedAlloyBlock(
        module_name="core_domain",
        code="module core_domain",
        start_line=1,
        end_line=3,
        line_to_req_ids={2: ["REQ-001", "REQ-002"], 3: ["REQ-002", "REQ-003"]},
    )
    assert block.req_ids_for_lines([2, 3, 99]) == ["REQ-001", "REQ-002", "REQ-003"]
    assert block.all_req_ids() == ["REQ-001", "REQ-002", "REQ-003"]

    with pytest.raises(ValidationError):
        AlloyRunResult(status=AlloyRunStatus.VERIFIED, unexpected=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ConflictQuestion(conflict_id="C1", prompt="Resolve?", nope=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        CounterexamplePrompt(assertion_name="A1", question="Guardrail?", nope=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        AlloyVerificationOutcome(
            status=AlloyRunStatus.VERIFIED,
            compat_mode=AlloyCompatMode.ALLOY6,
            spec_markdown="",
            core_domain_als="",
            behavior_als="",
            nope=True,  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# 2. Layer 1 version-agnostic static linter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("keyword", list(ALLOY6_TEMPORAL_KEYWORDS))
def test_lint_rejects_every_alloy6_temporal_keyword(keyword: str) -> None:
    bad_als = f"module core_domain\nsig A {{}}\nfact Bad {{ {keyword} some A }}\n"
    errors = lint_core_domain_version_agnostic(bad_als)
    assert len(errors) == 1
    assert f"'{keyword}'" in errors[0]
    assert "Line 3" in errors[0]


def test_lint_rejects_prime_operator_outside_comments() -> None:
    bad_als = "module core_domain\nsig A { f: set A }\nfact PrimeBad { f' = f }\n"
    errors = lint_core_domain_version_agnostic(bad_als)
    assert len(errors) == 1
    assert "prime operator" in errors[0]
    assert "Line 3" in errors[0]


def test_lint_allows_temporal_keywords_and_primes_inside_comments_and_strings() -> None:
    clean_als = """\
module core_domain
// var always eventually after until once historically since triggered releases before x'
-- var always x' in dash comment
/* multi-line block comment:
   var x' always eventually until once
*/
sig HumanActor {}
sig Note { label: String }
fact StringLiteralOk {
  // string literal with escaped quote and keywords
  "always eventually var x' \\"until\\" once" != ""
  some HumanActor
}
"""
    assert lint_core_domain_version_agnostic(clean_als) == []


# ---------------------------------------------------------------------------
# 3. Two-Layer Spec.md emitter (Alloy 6 & Legacy)
# ---------------------------------------------------------------------------


def test_emitter_alloy6_structure_and_traceability() -> None:
    graph = _sample_graph()
    triage_plan, adrs = _sample_triage_and_adrs()
    emitter = AlloySpecEmitter()

    spec_md = emitter.emit_spec_md(
        graph,
        triage_plan=triage_plan,
        adrs=adrs,
        compat=AlloyCompatMode.ALLOY6,
        max_scope=4,
    )

    assert "## System Boundary" in spec_md
    assert "## Quantified NFR Budgets" in spec_md
    assert "## Traceability IDs" in spec_md
    assert "## API Contracts" in spec_md
    assert "## Out-of-Scope / Non-Goals" in spec_md
    assert "ADR-R001" in spec_md
    assert "ADRs/ADR-R001.md" in spec_md
    assert "REQ-004" in spec_md  # listed under deprecated

    blocks = extract_alloy_blocks(spec_md)
    assert set(blocks.keys()) == {"core_domain", "behavior"}

    core_als = blocks["core_domain"].code
    behavior_als = blocks["behavior"].code

    assert lint_core_domain_version_agnostic(core_als) == []
    assert "sig HumanActor" in core_als
    assert "sig TrainedRole" in core_als
    assert "sig FinanceApprover extends TrainedRole" in core_als
    assert "// @trace REQ-001" in core_als
    assert "// @trace REQ-002" in core_als
    assert "// @trace REQ-003" in core_als

    assert "open core_domain" in behavior_als
    assert "var activeEntities" in behavior_als
    assert "'" in behavior_als
    assert "always" in behavior_als
    assert "eventually" in behavior_als
    assert "for 4 but 4 int" in behavior_als

    refs = build_req_to_alloy_refs(blocks)
    assert "REQ-001" in refs
    assert "REQ-002" in refs
    assert "REQ-003" in refs


def test_emitter_legacy_compat_uses_ordering_time_and_zero_alloy6_keywords() -> None:
    graph = _sample_graph()
    emitter = AlloySpecEmitter()

    spec_md = emitter.emit_spec_md(
        graph,
        compat=AlloyCompatMode.LEGACY,
        max_scope=3,
    )

    blocks = extract_alloy_blocks(spec_md)
    core_als = blocks["core_domain"].code
    behavior_als = blocks["behavior"].code

    assert lint_core_domain_version_agnostic(core_als) == []
    assert lint_core_domain_version_agnostic(behavior_als) == []
    assert "open core_domain" in behavior_als
    assert "open util/ordering[Time]" in behavior_als
    assert "sig Time {}" in behavior_als
    assert "Trace.state[t.next]" in behavior_als
    assert "for 3 but 3 int" in behavior_als


def test_emitter_empty_graph_dict_triage_and_llm_notes() -> None:
    empty_graph = RequirementGraph(graph_id="empty-sys", nodes=[])
    local = MockProvider(responses=["- Enforce actor ownership\n- Bound state space to 4"])
    router = ProviderRouter(local)
    emitter = AlloySpecEmitter(router=router)

    spec_md = emitter.emit_spec_md(
        empty_graph,
        triage_plan={
            "items": [
                {"req_id": "", "track": "software"},
                {
                    "req_id": "REQ-9",
                    "track": "process",
                    "human_roles": ["OpsLead"],
                    "rationale": "Manual sign-off",
                },
            ]
        },
        adrs=[
            SimpleNamespace(
                adr_id="ADR-A001",
                kind="assumption",
                title="Default assumption",
                frontmatter={"requirement_ids": ["REQ-9"]},
            )
        ],
        include_llm_notes=True,
    )
    assert "BaselineDomainIntegrity" in spec_md
    assert "Enforce actor ownership" in spec_md
    assert "sig OpsLead extends TrainedRole" in spec_md

    # Callable scripted hook on MockAlloyAnalyzer
    hook_analyzer = MockAlloyAnalyzer(
        scripted_results=[
            lambda _c, _b, _s, v: AlloyRunResult(
                status=AlloyRunStatus.VERIFIED, vacuity_passed=v
            )
        ]
    )
    hook_res = hook_analyzer.run_check("module core_domain", "module behavior", work_dir=Path("."))
    assert hook_res.status == AlloyRunStatus.VERIFIED
    assert hook_res.vacuity_passed is True

    # Router failure in optional notes gracefully falls back without crashing.
    failing_router = ProviderRouter(MockProvider(responses=[ProviderError("offline", provider="local")]))
    spec_fallback = AlloySpecEmitter(router=failing_router).emit_spec_md(
        empty_graph, include_llm_notes=True
    )
    assert "BaselineDomainIntegrity" in spec_fallback

    with pytest.raises(ValueError, match="max_scope"):
        emitter.emit_spec_md(empty_graph, max_scope=0)


# ---------------------------------------------------------------------------
# 4. Extractor, writer & replacer
# ---------------------------------------------------------------------------


def test_extractor_line_maps_writer_and_replacer(tmp_path: Path) -> None:
    spec_md = """\
# Demo Spec

```alloy module=core_domain
module core_domain
// @trace REQ-010, REQ-011
fact DualConstraint {
  some HumanActor
}
sig HumanActor {} // @trace CUSTOM_ID
```

```alloy module=behavior
module behavior
open core_domain
// @trace REQ-010
assert Safe { some HumanActor }
check Safe for 4 but 4 int
```
"""
    blocks = extract_alloy_blocks(spec_md)
    core = blocks["core_domain"]
    assert core.start_line == 4
    assert core.line_to_req_ids[2] == ["REQ-010", "REQ-011"]
    assert core.line_to_req_ids[3] == ["REQ-010", "REQ-011"]
    assert core.line_to_req_ids[4] == ["REQ-010", "REQ-011"]
    assert core.line_to_req_ids[5] == ["REQ-010", "REQ-011"]
    assert core.line_to_req_ids[6] == ["CUSTOM_ID"]

    written = write_alloy_files(spec_md, tmp_path / "als")
    assert (tmp_path / "als" / "core_domain.als").is_file()
    assert (tmp_path / "als" / "behavior.als").is_file()
    assert set(written.keys()) == {"core_domain", "behavior"}

    updated = replace_alloy_block(spec_md, "core_domain", "module core_domain\nsig NewSig {}")
    assert "sig NewSig {}" in updated
    assert "open core_domain" in updated

    with pytest.raises(ValueError, match="not found"):
        replace_alloy_block(spec_md, "nonexistent", "module nonexistent")

    with pytest.raises(ValueError, match="Duplicate"):
        extract_alloy_blocks(
            "```alloy module=core_domain\nmodule core_domain\n```\n"
            "```alloy module=core_domain\nmodule core_domain\n```\n"
        )

    with pytest.raises(ValueError, match="Unclosed"):
        extract_alloy_blocks("```alloy module=core_domain\nmodule core_domain\n")


# ---------------------------------------------------------------------------
# 5. Anti-vacuity witness injection
# ---------------------------------------------------------------------------


def test_inject_vacuity_witnesses_for_all_assertions_and_idempotency() -> None:
    behavior = """\
module behavior
open core_domain
assert A1 { some univ }
check A1 for 4 but 4 int
assert A2 { some univ }
check A2 for 4 but 4 int
"""
    augmented, names = inject_vacuity_witnesses(behavior, max_scope=4)
    assert names == ["__vacuity_witness_A1", "__vacuity_witness_A2"]
    assert "pred __vacuity_witness_A1" in augmented
    assert "run __vacuity_witness_A1 for 4 but 4 int" in augmented
    assert "pred __vacuity_witness_A2" in augmented
    assert "run __vacuity_witness_A2 for 4 but 4 int" in augmented

    # Second call is idempotent.
    augmented_twice, names_twice = inject_vacuity_witnesses(augmented, max_scope=4)
    assert augmented_twice == augmented
    assert names_twice == names

    # Model with no assert/check gets default witness.
    no_assert_aug, fallback_names = inject_vacuity_witnesses("module behavior\nsig X {}", max_scope=3)
    assert fallback_names == ["__vacuity_witness_model"]
    assert "run __vacuity_witness_model for 3 but 3 int" in no_assert_aug

    with pytest.raises(ValueError, match="max_scope"):
        inject_vacuity_witnesses(behavior, max_scope=0)


# ---------------------------------------------------------------------------
# 6. HeadlessAlloyRunner (mocked subprocess.run) & output parser
# ---------------------------------------------------------------------------


def test_headless_runner_subprocess_verified_and_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_jar = tmp_path / "alloy.jar"
    fake_jar.write_bytes(b"fake-jar")
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured_cmd.extend(cmd)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="run __vacuity_witness_Safe: SAT\ncheck Safe: No counterexample found.\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = HeadlessAlloyRunner(jar_path=fake_jar, timeout_seconds=15.0, bitwidth=4)
    assert isinstance(runner, AlloyAnalyzer)

    result = runner.run_check(
        "module core_domain\nsig A {}",
        "module behavior\nopen core_domain\nassert Safe { some A }\ncheck Safe for 4",
        work_dir=tmp_path / "run1",
        max_scope=4,
        check_vacuity=True,
    )

    assert result.status == AlloyRunStatus.VERIFIED
    assert result.vacuity_passed is True
    assert "-jar" in captured_cmd
    assert str(fake_jar) in captured_cmd
    assert "-Dalloy.maxScope=4" in captured_cmd
    assert "-Dalloy.bitwidth=4" in captured_cmd
    written_behavior = (tmp_path / "run1" / "behavior.als").read_text(encoding="utf-8")
    assert "__vacuity_witness_Safe" in written_behavior


def test_headless_runner_compile_error_vacuity_unsat_counterexample_timeout_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_jar = tmp_path / "alloy.jar"
    fake_jar.write_bytes(b"fake-jar")
    runner = HeadlessAlloyRunner(jar_path=fake_jar, timeout_seconds=5.0, bitwidth=4)

    # 1) Compile error
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 1, "", "Syntax error in behavior.als at line 7: unexpected token"
        ),
    )
    res_compile = runner.run_check("module core_domain", "module behavior", work_dir=tmp_path / "c1")
    assert res_compile.status == AlloyRunStatus.COMPILE_ERROR
    assert res_compile.exit_code == 1
    assert 7 in res_compile.unsat_lines

    # 2) Vacuous UNSAT with UNSAT core
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd,
            0,
            "run __vacuity_witness_Safe: UNSAT\nUNSAT core: [Inv_REQ_001, Inv_REQ_002]\ncore_domain.als:12\n",
            "",
        ),
    )
    res_unsat = runner.run_check("module core_domain", "module behavior", work_dir=tmp_path / "c2")
    assert res_unsat.status == AlloyRunStatus.VACUOUS_UNSAT
    assert res_unsat.vacuity_passed is False
    assert 12 in res_unsat.unsat_lines
    assert "Inv_REQ_001" in res_unsat.unsat_facts
    assert "Inv_REQ_002" in res_unsat.unsat_facts

    # 3) Counterexample trace
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd,
            0,
            "run __vacuity_witness_Safe: SAT\n"
            "Counterexample found for assertion SafetyAuthorizedOwnership:\n"
            "Step 0 (Init): actor=HumanActor$0, activeEntities=none\n"
            "Step 1 (Escalate): actor=HumanActor$1, activeEntities=SystemEntity$0\n",
            "",
        ),
    )
    res_ce = runner.run_check("module core_domain", "module behavior", work_dir=tmp_path / "c3")
    assert res_ce.status == AlloyRunStatus.COUNTEREXAMPLE_FOUND
    assert res_ce.vacuity_passed is True
    assert len(res_ce.counterexample_trace) == 2
    assert res_ce.counterexample_trace[1]["actor"] == "HumanActor$1"

    # 4) Timeout
    def raise_timeout(cmd, **kw):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=5.0, output="", stderr="slow solver")

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    res_timeout = runner.run_check("module core_domain", "module behavior", work_dir=tmp_path / "c4")
    assert res_timeout.status == AlloyRunStatus.COMPILE_ERROR
    assert "timed out" in res_timeout.stderr

    # 5) OSError (java missing)
    def raise_oserror(cmd, **kw):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("java not found")

    monkeypatch.setattr(subprocess, "run", raise_oserror)
    res_os = runner.run_check("module core_domain", "module behavior", work_dir=tmp_path / "c5")
    assert res_os.status == AlloyRunStatus.COMPILE_ERROR
    assert "Failed to invoke" in res_os.stderr

    # 6) Unconfigured jar_path + validation + is_available
    monkeypatch.delenv("REQHQ_ALLOY_JAR_PATH", raising=False)
    unconfigured = HeadlessAlloyRunner(jar_path=None)
    assert unconfigured.is_available() is False
    res_no_jar = unconfigured.run_check("module core_domain", "module behavior", work_dir=tmp_path / "c6")
    assert res_no_jar.status == AlloyRunStatus.COMPILE_ERROR
    with pytest.raises(ValueError, match="Alloy JAR"):
        unconfigured.build_command(tmp_path / "b.als", tmp_path / "o")

    monkeypatch.setattr(shutil, "which", lambda _bin: "/usr/bin/java")
    assert runner.is_available() is True

    with pytest.raises(ValueError, match="timeout_seconds"):
        HeadlessAlloyRunner(timeout_seconds=0)
    with pytest.raises(ValueError, match="bitwidth"):
        HeadlessAlloyRunner(bitwidth=8)


def test_parse_alloy_output_instance_relations() -> None:
    parsed = parse_alloy_output(
        "---INSTANCE---\nthis/HumanActor={HumanActor$0}\nthis/SystemEntity={SystemEntity$0}\n",
        "",
        0,
        check_vacuity=True,
    )
    assert parsed.status == AlloyRunStatus.COUNTEREXAMPLE_FOUND
    assert len(parsed.counterexample_trace) == 1
    assert parsed.counterexample_trace[0]["HumanActor"] == "HumanActor$0"


# ---------------------------------------------------------------------------
# 7. UNSAT core -> ConflictQuestion mapper
# ---------------------------------------------------------------------------


def test_unsat_core_maps_via_trace_to_exact_req_ids_and_conflict_question() -> None:
    graph = _sample_graph()
    spec_md = AlloySpecEmitter().emit_spec_md(graph, compat=AlloyCompatMode.ALLOY6)
    blocks = extract_alloy_blocks(spec_md)

    # Locate exact line numbers of REQ-001 and REQ-002 facts in core_domain.
    core_lines = blocks["core_domain"].code.splitlines()
    req1_line = next(i for i, ln in enumerate(core_lines, start=1) if "fact Inv_REQ_001" in ln)
    req2_line = next(i for i, ln in enumerate(core_lines, start=1) if "fact Inv_REQ_002" in ln)

    unsat_result = AlloyRunResult(
        status=AlloyRunStatus.VACUOUS_UNSAT,
        vacuity_passed=False,
        unsat_lines=[req1_line, req2_line],
        unsat_facts=["Inv_REQ_001", "Inv_REQ_002"],
    )

    conflicts = map_unsat_to_conflicts(unsat_result, blocks, graph)
    assert len(conflicts) == 1
    q = conflicts[0]
    assert q.req_ids == ["REQ-001", "REQ-002"]
    assert q.unsat_facts == ["Inv_REQ_001", "Inv_REQ_002"]
    assert "REQ-001" in q.prompt and "REQ-002" in q.prompt
    assert "Dual approval" in q.prompt
    assert len(q.suggested_resolutions) >= 2

    # Single-requirement UNSAT and unmapped fallback UNSAT
    single_res = AlloyRunResult(
        status=AlloyRunStatus.VACUOUS_UNSAT,
        unsat_facts=["Inv_REQ_003"],
    )
    single_q = map_unsat_to_conflicts(single_res, blocks, graph)
    assert single_q[0].req_ids == ["REQ-003"]

    unmapped_res = AlloyRunResult(status=AlloyRunStatus.VACUOUS_UNSAT, unsat_lines=[999])
    unmapped_q = map_unsat_to_conflicts(unmapped_res, blocks, graph)
    assert len(unmapped_q) == 1
    assert unmapped_q[0].req_ids == []

    # Verified result produces no conflicts.
    verified_res = AlloyRunResult(status=AlloyRunStatus.VERIFIED, vacuity_passed=True)
    assert map_unsat_to_conflicts(verified_res, blocks, graph) == []


# ---------------------------------------------------------------------------
# 8. Counterexample -> CounterexamplePrompt translator
# ---------------------------------------------------------------------------


def test_counterexample_translates_to_step_by_step_user_scenario() -> None:
    graph = _sample_graph()
    spec_md = AlloySpecEmitter().emit_spec_md(graph, compat=AlloyCompatMode.ALLOY6)
    blocks = extract_alloy_blocks(spec_md)

    ce_result = AlloyRunResult(
        status=AlloyRunStatus.COUNTEREXAMPLE_FOUND,
        vacuity_passed=True,
        stdout="Counterexample found for assertion SafetyAuthorizedOwnership",
        counterexample_trace=[
            {"step": 0, "state": "Init", "actor": "HumanActor$0", "activeEntities": "none"},
            {"step": 1, "state": "UnauthorizedActivate", "actor": "HumanActor$1", "entity": "SystemEntity$0"},
        ],
    )
    prompt = translate_counterexample(ce_result, "SafetyAuthorizedOwnership", blocks)
    assert prompt.assertion_name == "SafetyAuthorizedOwnership"
    assert "REQ-001" in prompt.req_ids
    assert len(prompt.scenario_steps) == 2
    assert "Step 1 (Init)" in prompt.scenario_steps[0]
    assert "UnauthorizedActivate" in prompt.scenario_steps[1]
    assert "missing guardrail" in prompt.question.lower()
    assert "intended exception" in prompt.question.lower()

    # Empty trace falls back cleanly.
    empty_ce = AlloyRunResult(
        status=AlloyRunStatus.COUNTEREXAMPLE_FOUND,
        stdout="Assertion CustomAssert violated",
        counterexample_trace=[],
    )
    fallback_prompt = translate_counterexample(empty_ce, "CustomAssert", blocks)
    assert len(fallback_prompt.scenario_steps) == 1
    assert "CustomAssert violated" in fallback_prompt.scenario_steps[0]


# ---------------------------------------------------------------------------
# 9. Closed-loop AlloyVerificationController (vacuity, counterexample, repairs, escalation)
# ---------------------------------------------------------------------------


def test_controller_verified_and_logs_event(tmp_path: Path) -> None:
    graph = _sample_graph()
    spec_md = AlloySpecEmitter().emit_spec_md(graph, compat=AlloyCompatMode.ALLOY6)
    router = ProviderRouter(MockProvider())
    analyzer = MockAlloyAnalyzer(default_status=AlloyRunStatus.VERIFIED)

    event_log = SessionEventLog(sessions_dir=tmp_path / "sessions")
    session_id = event_log.create_session("sess-alloy-ok")

    controller = AlloyVerificationController(router, analyzer, max_local_repairs=2, max_scope=4)
    outcome = controller.verify_spec(
        spec_md,
        graph,
        compat=AlloyCompatMode.ALLOY6,
        event_log=event_log,
        session_id=session_id,
    )

    assert outcome.status == AlloyRunStatus.VERIFIED
    assert outcome.vacuity_checked is True
    assert outcome.repair_attempts == 0
    assert outcome.escalated is False
    assert outcome.cloud_verified is False
    assert len(analyzer.calls) == 1
    assert "__vacuity_witness_SafetyAuthorizedOwnership" in analyzer.calls[0]["behavior_als"]
    assert "REQ-001" in outcome.req_to_alloy_refs

    events = event_log.load_events(session_id)
    ver_events = [e for e in events if e.kind == EventKind.VERIFICATION_RECORDED]
    assert len(ver_events) == 1
    assert ver_events[0].payload["status"] == "VERIFIED"
    assert ver_events[0].payload["vacuity_passed"] is True


def test_controller_catches_vacuous_truth_and_fails_gate() -> None:
    graph = _sample_graph()
    spec_md = AlloySpecEmitter().emit_spec_md(graph, compat=AlloyCompatMode.ALLOY6)
    router = ProviderRouter(MockProvider())

    # Even if analyzer reports VACUOUS_UNSAT directly, gate fails with ConflictQuestions.
    analyzer = MockAlloyAnalyzer(
        scripted_results=[
            AlloyRunResult(
                status=AlloyRunStatus.VACUOUS_UNSAT,
                vacuity_passed=False,
                unsat_facts=["Inv_REQ_001", "Inv_REQ_002"],
            )
        ]
    )
    outcome = AlloyVerificationController(router, analyzer).verify_spec(spec_md, graph)
    assert outcome.status == AlloyRunStatus.VACUOUS_UNSAT
    assert outcome.vacuity_checked is True
    assert len(outcome.conflict_questions) == 1
    assert outcome.conflict_questions[0].req_ids == ["REQ-001", "REQ-002"]

    # Hard anti-vacuity guard: analyzer claiming VERIFIED with vacuity_passed=False is downgraded!
    sneaky_analyzer = MockAlloyAnalyzer(
        scripted_results=[
            AlloyRunResult(status=AlloyRunStatus.VERIFIED, vacuity_passed=False)
        ]
    )
    sneaky_outcome = AlloyVerificationController(router, sneaky_analyzer).verify_spec(spec_md, graph)
    assert sneaky_outcome.status == AlloyRunStatus.VACUOUS_UNSAT
    assert len(sneaky_outcome.conflict_questions) == 1


def test_controller_counterexample_generates_user_prompt() -> None:
    graph = _sample_graph()
    spec_md = AlloySpecEmitter().emit_spec_md(graph, compat=AlloyCompatMode.ALLOY6)
    router = ProviderRouter(MockProvider())
    analyzer = MockAlloyAnalyzer(scripted_results=[AlloyRunStatus.COUNTEREXAMPLE_FOUND])

    outcome = AlloyVerificationController(router, analyzer).verify_spec(spec_md, graph)
    assert outcome.status == AlloyRunStatus.COUNTEREXAMPLE_FOUND
    assert len(outcome.counterexample_prompts) == 1
    assert outcome.counterexample_prompts[0].assertion_name == "SafetyAuthorizedOwnership"
    assert len(outcome.counterexample_prompts[0].scenario_steps) >= 1


def test_controller_two_local_failures_trigger_alloy_repair_escalation_and_cloud_succeeds(
    tmp_path: Path,
) -> None:
    graph = _sample_graph()
    spec_md = AlloySpecEmitter().emit_spec_md(graph, compat=AlloyCompatMode.ALLOY6)
    blocks = extract_alloy_blocks(spec_md)
    repaired_behavior = blocks["behavior"].code.replace("Stutter", "StutterStep")

    local_provider = MockProvider(
        name="ollama-local",
        is_local=True,
        responses=[
            # Local repair attempt 1 (still broken in analyzer)
            f"```alloy module=behavior\n{blocks['behavior'].code}\n```",
            # Local repair attempt 2 (still broken in analyzer)
            f"```alloy module=behavior\n{blocks['behavior'].code}\n```",
            # Router calls local once before escalating on explicit escalation_reason
            f"```alloy module=behavior\n{blocks['behavior'].code}\n```",
        ],
    )
    cloud_provider = MockProvider(
        name="gemini-free",
        is_local=False,
        responses=[
            f"```alloy module=behavior\n{repaired_behavior}\n```",
        ],
    )
    router = ProviderRouter(local_provider, [cloud_provider])

    # Analyzer fails compile initially + after local repair 1 + after local repair 2,
    # then succeeds after cloud repair!
    analyzer = MockAlloyAnalyzer(
        scripted_results=[
            AlloyRunStatus.COMPILE_ERROR,
            AlloyRunStatus.COMPILE_ERROR,
            AlloyRunStatus.COMPILE_ERROR,
            AlloyRunStatus.VERIFIED,
        ]
    )

    event_log = SessionEventLog(sessions_dir=tmp_path / "sessions")
    session_id = event_log.create_session("sess-alloy-escalate")

    controller = AlloyVerificationController(
        router, analyzer, max_local_repairs=2, max_scope=4, work_dir=tmp_path / "work"
    )
    outcome = controller.verify_spec(
        spec_md,
        graph,
        compat=AlloyCompatMode.ALLOY6,
        event_log=event_log,
        session_id=session_id,
    )

    assert outcome.status == AlloyRunStatus.VERIFIED
    assert outcome.repair_attempts == 2
    assert outcome.escalated is True
    assert outcome.cloud_verified is True
    assert outcome.vacuity_checked is True
    assert "StutterStep" in outcome.behavior_als
    assert len(outcome.run_results) == 4
    assert len(cloud_provider.calls) == 1

    events = event_log.load_events(session_id)
    esc_events = [e for e in events if e.kind == EventKind.ESCALATION_TRIGGERED]
    assert len(esc_events) == 1
    assert esc_events[0].payload["reason"] == EscalationReason.ALLOY_REPAIR_EXHAUSTED.value
    assert esc_events[0].payload["local_repairs_attempted"] == 2


def test_controller_repairs_layer1_lint_violation_locally() -> None:
    graph = _sample_graph()
    spec_md = AlloySpecEmitter().emit_spec_md(graph, compat=AlloyCompatMode.ALLOY6)
    blocks = extract_alloy_blocks(spec_md)
    clean_core = blocks["core_domain"].code

    # Inject a forbidden Alloy 6 `var` into core_domain.
    bad_core = clean_core.replace("sig HumanActor {}", "var sig HumanActor {}")
    broken_spec = replace_alloy_block(spec_md, "core_domain", bad_core)

    local_provider = MockProvider(
        name="ollama-local",
        is_local=True,
        responses=[f"```alloy module=core_domain\n{clean_core}\n```"],
    )
    router = ProviderRouter(local_provider)
    analyzer = MockAlloyAnalyzer(default_status=AlloyRunStatus.VERIFIED)

    outcome = AlloyVerificationController(router, analyzer, max_local_repairs=2).verify_spec(
        broken_spec, graph, compat=AlloyCompatMode.ALLOY6
    )

    assert outcome.status == AlloyRunStatus.VERIFIED
    assert outcome.repair_attempts == 1
    assert outcome.escalated is False
    assert lint_core_domain_version_agnostic(outcome.core_domain_als) == []


def test_controller_validation_legacy_lint_and_repair_parsers() -> None:
    graph = _sample_graph()
    router = ProviderRouter(MockProvider())
    analyzer = MockAlloyAnalyzer()

    with pytest.raises(ValueError, match="max_local_repairs"):
        AlloyVerificationController(router, analyzer, max_local_repairs=-1)
    with pytest.raises(ValueError, match="max_scope"):
        AlloyVerificationController(router, analyzer, max_scope=0)
    with pytest.raises(ValueError, match="both 'core_domain' and 'behavior'"):
        AlloyVerificationController(router, analyzer).verify_spec("# Missing blocks", graph)

    # Legacy compat catches Alloy 6 temporal keywords inside behavior block.
    alloy6_spec = AlloySpecEmitter().emit_spec_md(graph, compat=AlloyCompatMode.ALLOY6)
    outcome_legacy_fail = AlloyVerificationController(
        router, analyzer, max_local_repairs=0
    ).verify_spec(alloy6_spec, graph, compat=AlloyCompatMode.LEGACY)
    assert outcome_legacy_fail.status == AlloyRunStatus.COMPILE_ERROR
    assert "forbidden Alloy 6" in outcome_legacy_fail.run_results[0].stderr

    # Helper parser edge cases: JSON payload, raw module text, generic alloy fence.
    assert _extract_repaired_modules(
        json.dumps({"core_domain": "module core_domain\nsig A {}"}),
        default_target="core_domain",
    ) == {"core_domain": "module core_domain\nsig A {}"}
    assert _extract_repaired_modules(
        "module behavior\nopen core_domain",
        default_target="core_domain",
    ) == {"behavior": "module behavior\nopen core_domain"}
    assert _extract_repaired_modules(
        "```alloy\nmodule core_domain\nsig B {}\n```",
        default_target="behavior",
    ) == {"core_domain": "module core_domain\nsig B {}"}
    assert _extract_repaired_modules(
        "```alloy\nsig Fallback {}\n```",
        default_target="behavior",
    ) == {"behavior": "sig Fallback {}"}
    assert _extract_repaired_modules("", default_target="behavior") == {}
    assert _extract_repaired_modules("```alloy\n   \n```", default_target="behavior") == {}
    assert _extract_repaired_modules(
        "```alloy\nmodule behavior\nopen core_domain\n```",
        default_target="core_domain",
    ) == {"behavior": "module behavior\nopen core_domain"}
    assert _extract_repaired_modules("{not valid json", default_target="behavior") == {}
    assert _extract_repaired_modules(
        "module core_domain\nsig RawCore {}",
        default_target="behavior",
    ) == {"core_domain": "module core_domain\nsig RawCore {}"}

    # _pick_violated_assertion fallbacks when stdout/stderr omit the assertion name
    silent_ce = AlloyRunResult(status=AlloyRunStatus.COUNTEREXAMPLE_FOUND)
    assert _pick_violated_assertion(silent_ce, "module behavior\nassert FirstAssert { some univ }") == "FirstAssert"
    assert _pick_violated_assertion(silent_ce, "module behavior\nsig X {}") == "ModelAssertion"


def test_edge_cases_for_100_percent_branch_coverage(tmp_path: Path) -> None:
    # 1. emitter.py: multi-line string literal inside linter, identifier sanitization,
    #    empty triage items, ADR dict frontmatter decision + empty adr_id + object frontmatter title,
    #    and _optional_llm_notes with router=None and JSON/empty router response.
    multiline_str_als = 'module core_domain\nsig Note { label: String }\nfact S { "line1\nvar always" != "" }\n'
    assert lint_core_domain_version_agnostic(multiline_str_als) == []

    assert _sanitize_alloy_ident("123-role", prefix="Role") == "Role_123_role"
    assert _sanitize_alloy_ident("---", prefix="Role") == "Role"
    assert _sanitize_alloy_ident("always", prefix="Role") == "Role_always"
    assert _sanitize_alloy_ident("sig", prefix="Item") == "Item_sig"

    empty_graph = RequirementGraph(graph_id="edge-sys", nodes=[])
    spec_no_router = AlloySpecEmitter(router=None).emit_spec_md(
        empty_graph,
        triage_plan={"items": []},
        adrs=[
            {"adr_id": "", "title": "Ignored"},
            {
                "adr_id": "ADR-R099",
                "frontmatter": {"decision": "Process", "requirement_ids": ["REQ-X"]},
            },
            SimpleNamespace(
                adr_id="ADR-R100",
                kind="routing",
                title="",
                frontmatter={"title": "Frontmatter Derived Title", "requirement_ids": ["REQ-Y"]},
            ),
        ],
        include_llm_notes=True,
    )
    assert "ADR-R099" in spec_no_router
    assert "Frontmatter Derived Title" in spec_no_router

    # Router returning JSON or whitespace skips optional notes cleanly
    json_router = ProviderRouter(MockProvider(responses=['{"status": "ok"}']))
    spec_json_note = AlloySpecEmitter(router=json_router).emit_spec_md(
        empty_graph, include_llm_notes=True
    )
    assert "### Verification Boundary Notes" not in spec_json_note

    # 2. extractor.py: intervening description comment preserves @trace across block,
    #    nested comments inside traced block (both plain comment and extra @trace),
    #    single-line statement (no braces) after @trace comment,
    #    and inline declaration @trace maps directly to module::symbol.
    spec_nested = """\
```alloy module=core_domain
module core_domain
// @trace REQ-001
// Intervening description comment must preserve pending @trace
fact OuterFact {
  // plain comment inside traced block
  // @trace REQ-002
  some HumanActor
}
fact InlineFact { some HumanActor } // @trace REQ-003
sig HumanActor {}
// @trace REQ-004
check InlineCheck for 4 but 4 int
```
"""
    nested_blocks = extract_alloy_blocks(spec_nested)
    core_map = nested_blocks["core_domain"].line_to_req_ids
    assert core_map[4] == ["REQ-001"]  # fact OuterFact {
    assert core_map[5] == ["REQ-001"]  # plain comment inside traced block
    assert core_map[7] == ["REQ-001", "REQ-002"]  # body line after inner @trace
    assert core_map[12] == ["REQ-004"]  # single-line check after @trace
    refs = build_req_to_alloy_refs(nested_blocks)
    assert refs["REQ-001"] == ["core_domain.als::OuterFact"]
    assert refs["REQ-003"] == ["core_domain.als::InlineFact"]

    # 3. unsat_mapper.py: line-only UNSAT mapping (no unsat_facts) resolves via @trace,
    #    REQ-ID not present in graph falls back cleanly to raw REQ-ID string,
    #    and unannotated fact name matching sanitized REQ-ID resolves via fallback.
    graph_partial = RequirementGraph(
        graph_id="partial-graph",
        nodes=[
            RequirementNode(
                req_id="REQ-001",
                title="Known requirement",
                statement="Must hold.",
            )
        ],
    )
    line_only_unsat = AlloyRunResult(
        status=AlloyRunStatus.VACUOUS_UNSAT,
        unsat_lines=[7],  # traced to REQ-001 (in graph) and REQ-002 (missing from graph)
    )
    conflicts = map_unsat_to_conflicts(line_only_unsat, nested_blocks, graph_partial)
    assert conflicts[0].req_ids == ["REQ-001", "REQ-002"]
    assert "REQ-002" in conflicts[0].prompt

    unannotated_blocks = extract_alloy_blocks(
        "```alloy module=core_domain\nmodule core_domain\nfact Inv_REQ_001 { no univ }\n```\n"
    )
    fact_name_fallback = AlloyRunResult(
        status=AlloyRunStatus.VACUOUS_UNSAT,
        unsat_facts=["Inv_REQ_001"],
    )
    fallback_conflicts = map_unsat_to_conflicts(
        fact_name_fallback, unannotated_blocks, graph_partial
    )
    assert fallback_conflicts[0].req_ids == ["REQ-001"]

    # 4. counterexample.py: step dict with only step/state (no extra fields).
    minimal_step_ce = AlloyRunResult(
        status=AlloyRunStatus.COUNTEREXAMPLE_FOUND,
        counterexample_trace=[{"step": 0, "state": "Idle"}],
    )
    ce_prompt = translate_counterexample(minimal_step_ce, "Safe", nested_blocks)
    assert ce_prompt.scenario_steps == ["Step 1 (Idle): state transition observed"]

    # 5. mock_analyzer.py: AlloyRunStatus.VACUOUS_UNSAT enum scripted directly.
    vac_mock = MockAlloyAnalyzer(scripted_results=[AlloyRunStatus.VACUOUS_UNSAT])
    vac_res = vac_mock.run_check("module core_domain", "module behavior", work_dir=tmp_path)
    assert vac_res.status == AlloyRunStatus.VACUOUS_UNSAT
    assert vac_res.vacuity_passed is False

    # 6. runner.py: single `fact Name` UNSAT format & prose-only counterexample step detail.
    single_fact_unsat = parse_alloy_output(
        "run __vacuity_witness_Safe: UNSAT in fact ContradictoryFact at line 4",
        "",
        0,
        check_vacuity=True,
    )
    assert single_fact_unsat.status == AlloyRunStatus.VACUOUS_UNSAT
    assert single_fact_unsat.unsat_facts == ["ContradictoryFact"]

    prose_step_ce = parse_alloy_output(
        "Counterexample found:\nStep 0 (Init): unauthorized actor entered vault\n",
        "",
        0,
        check_vacuity=True,
    )
    assert prose_step_ce.status == AlloyRunStatus.COUNTEREXAMPLE_FOUND
    assert prose_step_ce.counterexample_trace == [
        {"step": 0, "state": "Init", "detail": "unauthorized actor entered vault"}
    ]

    # 7. repair_loop.py: cloud escalation repairing core_domain (lines 313-314).
    graph = _sample_graph()
    spec_md = AlloySpecEmitter().emit_spec_md(graph, compat=AlloyCompatMode.ALLOY6)
    blocks = extract_alloy_blocks(spec_md)
    repaired_core = blocks["core_domain"].code + "// cloud repaired\n"
    local_p = MockProvider(
        name="local",
        is_local=True,
        responses=[f"```alloy module=core_domain\n{blocks['core_domain'].code}\n```"],
    )
    cloud_p = MockProvider(
        name="cloud",
        is_local=False,
        responses=[f"```alloy module=core_domain\n{repaired_core}\n```"],
    )
    esc_router = ProviderRouter(local_p, [cloud_p])
    core_err = AlloyRunResult(
        status=AlloyRunStatus.COMPILE_ERROR,
        exit_code=1,
        stderr="Syntax error in core_domain.als at line 3",
    )
    esc_analyzer = MockAlloyAnalyzer(scripted_results=[core_err, AlloyRunStatus.VERIFIED])
    esc_outcome = AlloyVerificationController(
        esc_router, esc_analyzer, max_local_repairs=0
    ).verify_spec(spec_md, graph)
    assert esc_outcome.status == AlloyRunStatus.VERIFIED
    assert esc_outcome.escalated is True
    assert esc_outcome.cloud_verified is True
    assert "// cloud repaired" in esc_outcome.core_domain_als


# ---------------------------------------------------------------------------
# 10. Opt-in live Alloy Analyzer test (cleanly skipped when Java/JAR absent)
# ---------------------------------------------------------------------------


@pytest.mark.alloy_live
def test_live_headless_alloy_execution(tmp_path: Path) -> None:
    jar_env = os.environ.get("REQHQ_ALLOY_JAR_PATH", "").strip()
    if not shutil.which("java") or not jar_env or not Path(jar_env).is_file():
        pytest.skip("java or REQHQ_ALLOY_JAR_PATH is not available in this environment")

    graph = _sample_graph()
    spec_md = AlloySpecEmitter().emit_spec_md(graph, compat=AlloyCompatMode.ALLOY6, max_scope=3)
    blocks = extract_alloy_blocks(spec_md)

    runner = HeadlessAlloyRunner(jar_path=Path(jar_env), timeout_seconds=30.0, bitwidth=4)
    result = runner.run_check(
        blocks["core_domain"].code,
        blocks["behavior"].code,
        work_dir=tmp_path / "live_alloy",
        max_scope=3,
        check_vacuity=True,
    )
    assert result.status == AlloyRunStatus.VERIFIED
    assert result.vacuity_passed is True
