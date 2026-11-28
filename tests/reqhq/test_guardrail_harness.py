"""Unit and deterministic Hypothesis PBT tests for reqhq.harness."""

from __future__ import annotations

import ast
import csv
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from hypothesis import example, given, settings, strategies as st
from pydantic import ValidationError
import pytest

from reqhq.harness import (
    RESULTS_CSV_FIELDNAMES,
    ASTMutantEngine,
    AgentRunResult,
    CodingAgent,
    ExperimentCondition,
    ExperimentHarness,
    HandoffBundleManifest,
    MockCodingAgent,
    MutantOutcome,
    MutantRecord,
    MutationReport,
    MutationRunner,
    MutmutRunner,
    TrialResultRow,
    append_trial_result_csv,
    build_handoff_bundle,
    compute_mutation_score,
    generate_mutants,
    read_trial_results_csv,
    select_mutation_runner,
)
from reqhq.harness.ast_mutator import _replace_child_in_parent
from reqhq.harness.handoff import _render_handoff_instructions
from reqhq.llm.quota_ledger import QuotaLedger
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
from reqhq.synthesis.models import TriageItem, TriagePlan, TriageTrack

_VALID_SHA = "a" * 64


def _sample_graph_plan_spec() -> tuple[RequirementGraph, TriagePlan, str]:
    graph = RequirementGraph(
        graph_id="graph-p3-harness",
        nodes=[
            RequirementNode(
                req_id="REQ-001",
                title="Quota boundary check",
                statement="The service MUST allow requests when usage is within the configured limit and the account is active.",
                kind=RequirementKind.FUNCTIONAL,
                status=RequirementStatus.VERIFIED,
                priority="P0",
                trace_links=[
                    TraceLink(
                        source_sha256=_VALID_SHA,
                        source_uri="prd/quota.md",
                        anchor="section-1",
                        excerpt="usage <= limit and active",
                    )
                ],
            ),
            RequirementNode(
                req_id="REQ-002",
                title="Operator review checklist",
                statement="Support operators MUST review escalations weekly.",
                kind=RequirementKind.PROCESS,
                status=RequirementStatus.ACCEPTED,
                priority="P1",
                trace_links=[
                    TraceLink(
                        source_sha256=_VALID_SHA,
                        source_uri="prd/quota.md",
                        anchor="section-2",
                        excerpt="weekly escalation review",
                    )
                ],
            ),
        ],
    )
    plan = TriagePlan(
        session_id="sess-p3-001",
        items=[
            TriageItem(
                req_id="REQ-001",
                track=TriageTrack.SOFTWARE,
                rationale="Enforced in quota_gate module.",
                software_module="quota_gate",
            ),
            TriageItem(
                req_id="REQ-002",
                track=TriageTrack.PROCESS,
                rationale="Human operational cadence.",
                human_roles=["Support Operator"],
            ),
        ],
    )
    spec_markdown = (
        "# Quota Gate Specification\n\n"
        "```alloy\n"
        "module quota_gate\n"
        "// @trace REQ-001\n"
        "sig Account {}\n"
        "assert QuotaBound { some Account }\n"
        "check QuotaBound for 3\n"
        "```\n"
    )
    return graph, plan, spec_markdown


def _sample_pbt_doc() -> Any:
    from reqhq.pbt.models import (
        GuardrailEnforcement,
        PBTContractDocument,
        PBTPropertyContract,
        PropertyKind,
    )

    spec = PBTSpec(
        pbt_spec_id="pbt-quota-001",
        target_module="quota_gate",
        req_ids=["REQ-001"],
        invariants=[
            PropertyInvariant(
                invariant_id="PROP-001",
                req_id="REQ-001",
                title="Quota stays within limit",
                precondition="limit >= 0",
                assertion_expr="used <= limit",
                strategies=[
                    HypothesisStrategySpec(
                        parameter_name="used",
                        strategy_expr="st.integers(min_value=0, max_value=100)",
                    ),
                    HypothesisStrategySpec(
                        parameter_name="limit",
                        strategy_expr="st.integers(min_value=0, max_value=100)",
                    ),
                ],
                max_examples=200,
            )
        ],
        guardrails=[
            PydanticGuardrail(
                guardrail_id="GR-001",
                model_name="QuotaRequest",
                field_name="used",
                constraint_type="range",
                rule="ge=0",
                error_message="used must be non-negative",
            )
        ],
        alloy_model_ref="assert:QuotaBound",
        seed=42,
    )
    return PBTContractDocument(
        spec=spec,
        session_id="sess-p3-001",
        source_sha256=_VALID_SHA,
        properties=[
            PBTPropertyContract(
                property_id="PROP-001",
                req_id="REQ-001",
                adr_id="ADR-R001",
                source_sha256=_VALID_SHA,
                alloy_ref="assert:QuotaBound",
                target_symbol="check_quota",
                kind=PropertyKind.INVARIANT,
                title="Quota stays within limit",
                precondition="limit >= 0",
                assertion_expr="used <= limit or not (used <= limit)",
                input_strategies=[
                    HypothesisStrategySpec(
                        parameter_name="used",
                        strategy_expr="st.integers(min_value=0, max_value=100)",
                    ),
                    HypothesisStrategySpec(
                        parameter_name="limit",
                        strategy_expr="st.integers(min_value=0, max_value=100)",
                    ),
                ],
                guardrail_enforcement=GuardrailEnforcement.BOTH,
                max_examples=200,
            )
        ],
        seed=42,
    )


# ---------------------------------------------------------------------------
# Models unit tests
# ---------------------------------------------------------------------------


def test_models_validation_and_extra_forbid(tmp_path: Path) -> None:
    assert compute_mutation_score(0, 0, 0, 0) == 0.0
    assert compute_mutation_score(0, 0, 0, 3) == 0.0
    assert compute_mutation_score(3, 1, 0, 0) == 0.75
    with pytest.raises(ValueError, match="non-negative"):
        compute_mutation_score(-1, 0, 0, 0)

    rec = MutantRecord(
        mutant_id="MUT-0001",
        file_path="app.py",
        line_no=3,
        operator="rel_lt_to_lte",
        original_snippet="a < b",
        mutated_snippet="a <= b",
        outcome=MutantOutcome.KILLED,
        elapsed_ms=12.5,
    )
    assert rec.outcome is MutantOutcome.KILLED

    with pytest.raises(ValidationError):
        MutantRecord(
            mutant_id="   ",
            file_path="app.py",
            line_no=1,
            operator="op",
            original_snippet="a",
            mutated_snippet="b",
            outcome=MutantOutcome.KILLED,
        )
    with pytest.raises(ValidationError):
        MutantRecord(
            mutant_id="MUT-1",
            file_path="app.py",
            line_no=1,
            operator="op",
            original_snippet="a",
            mutated_snippet="b",
            outcome=MutantOutcome.KILLED,
            extra_field="nope",  # type: ignore[call-arg]
        )

    report = MutationReport(
        runner_name="ast_mutant_engine",
        target_files=["app.py"],
        total_mutants=1,
        killed=1,
        survived=0,
        timeout=0,
        errors=0,
        mutation_score=1.0,
        mutants=[rec],
    )
    assert report.total_mutants == 1

    with pytest.raises(ValidationError, match="runner_name"):
        MutationReport(
            runner_name="  ",
            total_mutants=0,
            killed=0,
            survived=0,
            timeout=0,
            mutation_score=0.0,
        )
    with pytest.raises(ValidationError, match="Mutant count mismatch"):
        MutationReport(
            runner_name="ast",
            total_mutants=2,
            killed=1,
            survived=0,
            timeout=0,
            mutation_score=1.0,
        )
    with pytest.raises(ValidationError, match="Length of mutants"):
        MutationReport(
            runner_name="ast",
            total_mutants=2,
            killed=1,
            survived=1,
            timeout=0,
            mutation_score=0.5,
            mutants=[rec],
        )

    with pytest.raises(ValidationError, match="session_id"):
        HandoffBundleManifest(
            session_id="   ",
            out_dir=tmp_path,
            instructions_md=tmp_path / "HANDOFF_INSTRUCTIONS.md",
            spec_md=tmp_path / "Spec.md",
            design_md=tmp_path / "Design.md",
            process_training_md=tmp_path / "Process_and_Training.md",
            pbt_spec_yaml=tmp_path / "PBT_Spec.yaml",
            traceability_matrix_json=tmp_path / "traceability_matrix.json",
            lock_file=tmp_path / "pbt_lock.sha256",
        )

    row = TrialResultRow(
        trial_id="t-1",
        case_id="case-1",
        condition=ExperimentCondition.COND_C,
        seed=42,
        hidden_pass_at_1=True,
        pbt_pass_rate=1.0,
        mutation_score=0.875,
        lock_verified=True,
        stall_count=0,
        wall_clock_seconds=1.25,
        quota_tokens_used=120,
        quota_requests_used=2,
        artifacts_dir="trials/t-1",
    )
    assert TrialResultRow.csv_fieldnames() == RESULTS_CSV_FIELDNAMES
    assert tuple(TrialResultRow.model_fields.keys()) == RESULTS_CSV_FIELDNAMES
    csv_dict = row.to_csv_dict()
    assert tuple(csv_dict.keys()) == RESULTS_CSV_FIELDNAMES
    assert csv_dict["hidden_pass_at_1"] == "true"
    assert csv_dict["lock_verified"] == "true"

    # Normalize -0.0 cleanly to 0.000000 in CSV output
    neg_zero_row = row.model_copy(
        update={"pbt_pass_rate": -0.0, "mutation_score": -0.0, "wall_clock_seconds": -0.0}
    )
    neg_zero_csv = neg_zero_row.to_csv_dict()
    assert neg_zero_csv["pbt_pass_rate"] == "0.000000"
    assert neg_zero_csv["mutation_score"] == "0.000000"
    assert neg_zero_csv["wall_clock_seconds"] == "0.000000"

    with pytest.raises(ValidationError):
        TrialResultRow(
            trial_id="  ",
            case_id="case-1",
            condition=ExperimentCondition.COND_A,
            seed=1,
            hidden_pass_at_1=False,
            pbt_pass_rate=0.0,
            mutation_score=0.0,
            lock_verified=False,
            wall_clock_seconds=0.1,
            artifacts_dir="trials/x",
        )


# ---------------------------------------------------------------------------
# AST Mutant Engine unit & real-subprocess tests
# ---------------------------------------------------------------------------


def test_generate_mutants_covers_all_operators_and_caps() -> None:
    source = (
        "def evaluate(a: int, b: int, flag: bool) -> bool:\n"
        "    if a < b and not flag:\n"
        "        return True\n"
        "    if a <= b or a > b:\n"
        "        return False\n"
        "    if a >= b and a == 0 and a != 5:\n"
        "        x = a + b - 1\n"
        "        return x > 0\n"
        "    return False\n"
    )
    mutants = generate_mutants(source, "sample.py")
    ops = {m.operator for m in mutants}
    assert {
        "rel_lt_to_lte",
        "rel_lte_to_lt",
        "rel_gt_to_gte",
        "rel_gte_to_gt",
        "rel_eq_to_neq",
        "rel_neq_to_eq",
        "bool_and_to_or",
        "bool_or_to_and",
        "bool_remove_not",
        "arith_add_to_sub",
        "arith_sub_to_add",
        "return_flip_bool",
        "return_to_none",
        "const_zero_to_one",
        "const_int_inc",
    }.issubset(ops)

    # Determinism check
    mutants_again = generate_mutants(source, "sample.py")
    assert mutants == mutants_again

    # Cap checks and empty/no-site source check
    assert generate_mutants("pass\n", "empty.py") == []
    assert generate_mutants(source, "sample.py", max_mutants=0) == []
    capped = generate_mutants(source, "sample.py", max_mutants=3)
    assert len(capped) == 3
    assert [m.mutant_id for m in capped] == ["MUT-0001", "MUT-0002", "MUT-0003"]

    # Direct check of _replace_child_in_parent for attribute child, list child, and missing node
    attr_tree = ast.parse("x = not a\n")
    attr_unary = next(n for n in ast.walk(attr_tree) if isinstance(n, ast.UnaryOp))
    assert _replace_child_in_parent(attr_tree, attr_unary, attr_unary.operand) is True
    assert ast.unparse(attr_tree) == "x = a"

    tree = ast.parse("x = [not a]\n")
    unary = next(n for n in ast.walk(tree) if isinstance(n, ast.UnaryOp))
    assert _replace_child_in_parent(tree, unary, unary.operand) is True
    assert _replace_child_in_parent(tree, ast.Constant(value=999), ast.Constant(value=0)) is False


def test_ast_mutant_engine_kills_real_injected_bugs(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    module_file = src_dir / "pricing.py"
    module_file.write_text(
        "def compute_total(price: int, discount: int, member: bool) -> int:\n"
        "    if member and price >= 10:\n"
        "        return price - discount\n"
        "    return price + 1\n",
        encoding="utf-8",
        newline="\n",
    )
    test_file = tmp_path / "test_pricing.py"
    test_file.write_text(
        "from pricing import compute_total\n\n"
        "def test_member_threshold_and_arithmetic() -> None:\n"
        "    assert compute_total(10, 3, True) == 7\n"
        "    assert compute_total(9, 3, True) == 10\n"
        "    assert compute_total(10, 3, False) == 11\n",
        encoding="utf-8",
        newline="\n",
    )

    engine = ASTMutantEngine(timeout_seconds=20.0, max_mutants=20)
    assert isinstance(engine, MutationRunner)
    report = engine.run(
        tmp_path,
        [module_file],
        test_paths=["test_pricing.py"],
    )
    assert report.runner_name == "ast_mutant_engine"
    assert report.total_mutants >= 6
    assert report.killed == report.total_mutants
    assert report.survived == 0
    assert report.timeout == 0
    assert report.errors == 0
    assert report.mutation_score == 1.0
    # Original file remains untouched
    assert "price >= 10" in module_file.read_text(encoding="utf-8")


def test_ast_mutant_engine_error_timeout_and_edge_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        ASTMutantEngine(timeout_seconds=0.0)
    with pytest.raises(ValueError, match="max_mutants"):
        ASTMutantEngine(max_mutants=-1)

    engine = ASTMutantEngine(timeout_seconds=5.0, max_mutants=5)
    with pytest.raises(FileNotFoundError):
        engine.run(tmp_path / "does_not_exist", ["a.py"])

    mod_a = tmp_path / "a.py"
    mod_b = tmp_path / "b.py"
    mod_a.write_text("def f(x: int) -> bool:\n    return x > 0\n", encoding="utf-8")
    mod_b.write_text("def g(x: int) -> bool:\n    return x < 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="timeout_seconds"):
        engine.run(tmp_path, ["a.py"], timeout_seconds=-1.0)
    with pytest.raises(ValueError, match="max_mutants"):
        engine.run(tmp_path, ["a.py"], max_mutants=-2)
    with pytest.raises(ValueError, match="escapes workspace"):
        engine.run(tmp_path, [tmp_path.parent / "outside.py"])
    with pytest.raises(FileNotFoundError):
        engine.run(tmp_path, ["missing.py"])

    # Exercise max_mutants cap across multiple files + existing PYTHONPATH + Timeout/OSError/exitcode=2/survived
    monkeypatch.setenv("PYTHONPATH", "existing_path_entry")
    outcomes_queue: list[Any] = [
        subprocess.TimeoutExpired(cmd=["pytest"], timeout=5.0),
        OSError("spawn failed"),
        subprocess.CompletedProcess(args=["pytest"], returncode=2, stdout="", stderr="err"),
        subprocess.CompletedProcess(args=["pytest"], returncode=0, stdout="", stderr=""),
    ]

    def _fake_run(*args: Any, **kwargs: Any) -> Any:
        item = outcomes_queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(subprocess, "run", _fake_run)
    report = engine.run(tmp_path, ["a.py", "b.py"], max_mutants=3)
    assert report.total_mutants == 3
    assert report.timeout == 1
    assert report.errors == 2
    assert report.survived == 0
    assert report.mutation_score == 0.0

    # Zero max_mutants hits early break on second loop iteration too
    zero_report = engine.run(tmp_path, ["a.py", "b.py"], max_mutants=0)
    assert zero_report.total_mutants == 0
    assert zero_report.mutation_score == 0.0


# ---------------------------------------------------------------------------
# MutmutRunner POSIX guard & select_mutation_runner tests
# ---------------------------------------------------------------------------


def test_mutmut_runner_windows_guard_and_selector_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        MutmutRunner(timeout_seconds=0.0)
    with pytest.raises(ValueError, match="max_mutants"):
        MutmutRunner(max_mutants=-1)

    monkeypatch.setattr(sys, "platform", "win32")
    supported, reason = MutmutRunner.is_supported()
    assert supported is False
    assert "Windows" in reason

    runner = MutmutRunner()
    assert isinstance(runner, MutationRunner)
    with pytest.raises(RuntimeError, match="POSIX fork"):
        runner.run(tmp_path, ["app.py"])

    selected_default = select_mutation_runner()
    assert isinstance(selected_default, ASTMutantEngine)

    selected_win = select_mutation_runner(prefer_mutmut=True)
    assert isinstance(selected_win, ASTMutantEngine)


def test_mutmut_runner_mocked_posix_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    # Missing mutmut on POSIX -> RuntimeError and selector fallback
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    supported, reason = MutmutRunner.is_supported()
    assert supported is False
    assert "not installed" in reason
    assert isinstance(select_mutation_runner(prefer_mutmut=True), ASTMutantEngine)

    # Available mutmut on POSIX -> selector picks MutmutRunner
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    selected = select_mutation_runner(prefer_mutmut=True, timeout_seconds=12.0, max_mutants=25)
    assert isinstance(selected, MutmutRunner)

    with pytest.raises(FileNotFoundError):
        selected.run(tmp_path / "missing_ws", ["app.py"])
    with pytest.raises(ValueError, match="timeout_seconds"):
        selected.run(tmp_path, ["app.py"], timeout_seconds=0.0)

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=a[0],
            returncode=0,
            stdout="Killed: 6 Survived: 2 Timeout: 0 Suspicious: 1\n",
            stderr="",
        ),
    )
    report = selected.run(tmp_path, ["app.py"], test_paths=["tests"])
    assert report.runner_name == "mutmut"
    assert report.killed == 6
    assert report.survived == 2
    assert report.timeout == 0
    assert report.errors == 1
    assert report.total_mutants == 9
    assert report.mutation_score == 0.75

    # Non-zero exit with unparseable output -> errors=1
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=a[0],
            returncode=2,
            stdout="",
            stderr="mutmut crashed",
        ),
    )
    err_report = selected.run(tmp_path, ["app.py"])
    assert err_report.errors == 1
    assert err_report.total_mutants == 1
    assert err_report.mutation_score == 0.0

    # Timeout handling
    def _raise_timeout(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd=["mutmut"], timeout=12.0)

    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    to_report = selected.run(tmp_path, ["app.py"])
    assert to_report.timeout == 1
    assert to_report.total_mutants == 1
    assert to_report.mutation_score == 0.0


# ---------------------------------------------------------------------------
# Handoff bundle integration tests (with real reqhq.pbt)
# ---------------------------------------------------------------------------


def test_build_handoff_bundle_integrity_and_docs_adr_refusal(tmp_path: Path) -> None:
    from reqhq.pbt import verify_contract_lock

    graph, plan, spec_markdown = _sample_graph_plan_spec()
    pbt_doc = _sample_pbt_doc()

    # Refusal to write into repo docs/adr/
    forbidden_dir = tmp_path / "docs" / "adr" / "sub"
    with pytest.raises(ValueError, match="Nygard ADR directory"):
        build_handoff_bundle(
            forbidden_dir,
            session_id="sess-p3-001",
            graph=graph,
            plan=plan,
            spec_markdown=spec_markdown,
            pbt_doc=pbt_doc,
        )

    # Blank session_id rejected
    with pytest.raises(ValueError, match="session_id"):
        build_handoff_bundle(
            tmp_path / "bad",
            session_id="   ",
            graph=graph,
            plan=plan,
            spec_markdown=spec_markdown,
            pbt_doc=pbt_doc,
        )

    out_dir = tmp_path / "handoff_bundle"
    manifest = build_handoff_bundle(
        out_dir,
        session_id="sess-p3-001",
        graph=graph,
        plan=plan,
        spec_markdown=spec_markdown,
        pbt_doc=pbt_doc,
        alloy_refs={"REQ-001": ["assert:QuotaBound"]},
    )

    assert manifest.instructions_md.is_file()
    assert manifest.spec_md.is_file()
    assert manifest.design_md.is_file()
    assert manifest.process_training_md.is_file()
    assert manifest.pbt_spec_yaml.is_file()
    assert manifest.traceability_matrix_json.is_file()
    assert manifest.lock_file.is_file()
    assert len(manifest.adr_files) >= 2
    assert len(manifest.guardrail_files) >= 1
    assert len(manifest.pbt_test_files) >= 1

    # Verify contract lock matches exact files written by export_pbt_bundle
    locked_hashes = verify_contract_lock(out_dir)
    expected_locked = {
        "PBT_Spec.yaml",
        *(p.relative_to(out_dir).as_posix() for p in manifest.guardrail_files),
        *(p.relative_to(out_dir).as_posix() for p in manifest.pbt_test_files),
    }
    assert set(locked_hashes.keys()) == expected_locked

    # Verify LF-only line endings across all emitted handoff text artifacts
    all_text_artifacts = [
        manifest.instructions_md,
        manifest.spec_md,
        manifest.design_md,
        manifest.process_training_md,
        manifest.pbt_spec_yaml,
        manifest.traceability_matrix_json,
        manifest.lock_file,
        *manifest.adr_files,
        *manifest.guardrail_files,
        *manifest.pbt_test_files,
    ]
    for artifact in all_text_artifacts:
        assert b"\r\n" not in artifact.read_bytes(), f"CRLF found in {artifact}"

    # Verify traceability_matrix.json includes pbt_refs from pbt_doc.to_pbt_refs_by_req()
    matrix_data = json.loads(manifest.traceability_matrix_json.read_text(encoding="utf-8"))
    req1_row = next(r for r in matrix_data["rows"] if r["req_id"] == "REQ-001")
    assert req1_row["pbt_refs"] == ["PROP-001"]
    assert req1_row["alloy_refs"] == ["assert:QuotaBound"]

    # Verify HANDOFF_INSTRUCTIONS.md encodes Contract-First Verification Gate with LF endings
    instructions_text = manifest.instructions_md.read_text(encoding="utf-8")
    assert "Contract-First Verification Gate" in instructions_text
    assert "verify_contract_lock()" in instructions_text
    assert "guardrails/*.py" in instructions_text
    assert "docs/adr/" in instructions_text
    assert "PROP-001" in instructions_text

    # Empty inventory fallback branch check
    empty_md = _render_handoff_instructions(
        session_id="empty",
        target_dir=out_dir,
        adr_files=[],
        guardrail_files=[],
        pbt_test_files=[],
        lock_file=manifest.lock_file,
        pbt_refs_by_req={},
        seed=7,
    )
    assert "_(none emitted)_" in empty_md
    assert "_(no mapped PBT properties)_" in empty_md


# ---------------------------------------------------------------------------
# Condition A/B/C ExperimentHarness + Lock Tamper + QuotaLedger tests
# ---------------------------------------------------------------------------


def test_experiment_harness_cond_abc_quota_and_artifacts(tmp_path: Path) -> None:
    graph, plan, spec_markdown = _sample_graph_plan_spec()
    pbt_doc = _sample_pbt_doc()
    bundle_dir = tmp_path / "bundle_case_1"
    build_handoff_bundle(
        bundle_dir,
        session_id="sess-p3-001",
        graph=graph,
        plan=plan,
        spec_markdown=spec_markdown,
        pbt_doc=pbt_doc,
    )

    ledger = QuotaLedger(tmp_path / "quota.db")
    base_agent = MockCodingAgent()
    assert isinstance(base_agent, CodingAgent)

    class _LedgerRecordingAgent:
        def run_trial(self, **kwargs: Any) -> AgentRunResult:
            ledger.record_usage("gemini", prompt_tokens=30, completion_tokens=20)
            return base_agent.run_trial(**kwargs)

    harness = ExperimentHarness(
        agent=_LedgerRecordingAgent(),
        mutation_runner=ASTMutantEngine(timeout_seconds=20.0, max_mutants=10),
        quota_ledger=ledger,
        timeout_seconds=20.0,
    )
    rows = harness.run_experiment(
        case_ids=["case-01"],
        out_dir=tmp_path / "exp_out",
        seeds=(42,),
        bundle_dirs={"case-01": bundle_dir},
    )
    assert len(rows) == 3
    by_cond = {r.condition: r for r in rows}

    assert by_cond[ExperimentCondition.COND_A].hidden_pass_at_1 is False
    assert by_cond[ExperimentCondition.COND_A].lock_verified is False
    assert by_cond[ExperimentCondition.COND_A].stall_count == 1

    assert by_cond[ExperimentCondition.COND_B].hidden_pass_at_1 is True
    assert by_cond[ExperimentCondition.COND_B].lock_verified is False

    cond_c = by_cond[ExperimentCondition.COND_C]
    assert cond_c.hidden_pass_at_1 is True
    assert cond_c.pbt_pass_rate == 1.0
    assert cond_c.lock_verified is True
    assert cond_c.mutation_score == 1.0
    # Ledger recorded 50 tokens + 1 request on top of agent telemetry
    assert cond_c.quota_tokens_used == 1000
    assert cond_c.quota_requests_used == 5

    # Verify per-trial artifacts and results.csv
    for row in rows:
        art_dir = Path(row.artifacts_dir)
        assert (art_dir / "diff.patch").is_file()
        assert (art_dir / "log.txt").is_file()
        assert (art_dir / "test_output.txt").is_file()
        assert (art_dir / "pbt_output.txt").is_file()
        mut_json = art_dir / "mutation_report.json"
        assert mut_json.is_file()
        parsed_mut = MutationReport.model_validate(json.loads(mut_json.read_text(encoding="utf-8")))
        assert parsed_mut.mutation_score == row.mutation_score

    csv_rows = read_trial_results_csv(tmp_path / "exp_out" / "results.csv")
    assert len(csv_rows) == 3
    assert [r.condition for r in csv_rows] == [
        ExperimentCondition.COND_A,
        ExperimentCondition.COND_B,
        ExperimentCondition.COND_C,
    ]


def test_experiment_harness_cond_c_lock_tamper_rejection(tmp_path: Path) -> None:
    from reqhq.pbt import ContractTamperError

    graph, plan, spec_markdown = _sample_graph_plan_spec()
    pbt_doc = _sample_pbt_doc()

    # Case 1: fail_on_tamper=False records rejected row with lock_verified=False
    bundle_1 = tmp_path / "bundle_tamper_1"
    build_handoff_bundle(
        bundle_1,
        session_id="sess-tamper-1",
        graph=graph,
        plan=plan,
        spec_markdown=spec_markdown,
        pbt_doc=pbt_doc,
    )
    harness_soft = ExperimentHarness(
        agent=MockCodingAgent(tamper_lock=True),
        run_mutation=False,
    )
    row = harness_soft.run_trial(
        case_id="case-tamper",
        condition=ExperimentCondition.COND_C,
        seed=42,
        out_dir=tmp_path / "out_soft",
        bundle_dir=bundle_1,
        fail_on_tamper=False,
    )
    assert row.lock_verified is False
    assert row.hidden_pass_at_1 is False
    assert row.pbt_pass_rate == 0.0
    assert row.mutation_score == 0.0
    assert "CONTRACT TAMPER REJECTED" in (Path(row.artifacts_dir) / "log.txt").read_text(encoding="utf-8")

    # Case 2: fail_on_tamper=True raises ContractTamperError after recording audit trail
    bundle_2 = tmp_path / "bundle_tamper_2"
    build_handoff_bundle(
        bundle_2,
        session_id="sess-tamper-2",
        graph=graph,
        plan=plan,
        spec_markdown=spec_markdown,
        pbt_doc=pbt_doc,
    )
    with pytest.raises(ContractTamperError):
        harness_soft.run_trial(
            case_id="case-tamper-strict",
            condition=ExperimentCondition.COND_C,
            seed=42,
            out_dir=tmp_path / "out_strict",
            bundle_dir=bundle_2,
            fail_on_tamper=True,
        )
    strict_rows = read_trial_results_csv(tmp_path / "out_strict" / "results.csv")
    assert len(strict_rows) == 1
    assert strict_rows[0].lock_verified is False


def test_experiment_harness_timeout_missing_bundle_and_csv_errors(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        ExperimentHarness(timeout_seconds=0.0)

    harness = ExperimentHarness(
        agent=MockCodingAgent(simulate_timeout=True),
        run_mutation=False,
    )
    with pytest.raises(ValueError, match="timeout_seconds"):
        harness.run_trial(
            case_id="c1",
            condition=ExperimentCondition.COND_A,
            seed=1,
            out_dir=tmp_path / "bad_to",
            timeout_seconds=0.0,
        )

    # Timeout + Cond C missing bundle_dir (fail-closed lock_verified=False)
    row = harness.run_trial(
        case_id="c-timeout",
        condition=ExperimentCondition.COND_C,
        seed=7,
        out_dir=tmp_path / "out_timeout",
        bundle_dir=None,
    )
    assert row.hidden_pass_at_1 is False
    assert row.lock_verified is False
    assert row.stall_count == 1
    assert "TIMEOUT" in (Path(row.artifacts_dir) / "log.txt").read_text(encoding="utf-8")

    # Cond A with run_mutation=False hits non-mutation branch
    harness_nomut = ExperimentHarness(agent=MockCodingAgent(), run_mutation=False)
    row_nomut = harness_nomut.run_trial(
        case_id="c-nomut",
        condition=ExperimentCondition.COND_A,
        seed=1,
        out_dir=tmp_path / "out_nomut",
    )
    assert row_nomut.mutation_score == 0.0

    # Invalid CSV header rejected
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text("col_a,col_b\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid results.csv header"):
        read_trial_results_csv(bad_csv)


# ---------------------------------------------------------------------------
# Mandatory Deterministic Hypothesis Property-Based Tests
# ---------------------------------------------------------------------------


@settings(derandomize=True, max_examples=50, deadline=None)
@example(killed=0, survived=0, timeout=0, errors=0)
@example(killed=1, survived=0, timeout=0, errors=0)
@example(killed=0, survived=1, timeout=0, errors=0)
@example(killed=0, survived=0, timeout=1, errors=0)
@example(killed=0, survived=0, timeout=0, errors=1)
@given(
    killed=st.integers(min_value=0, max_value=500),
    survived=st.integers(min_value=0, max_value=500),
    timeout=st.integers(min_value=0, max_value=500),
    errors=st.integers(min_value=0, max_value=500),
)
def test_pbt_mutation_score_bounds_and_arithmetic_consistency(
    killed: int,
    survived: int,
    timeout: int,
    errors: int,
) -> None:
    total = killed + survived + timeout + errors
    score = compute_mutation_score(killed, survived, timeout, errors)

    assert 0.0 <= score <= 1.0
    if killed == 0:
        assert score == 0.0
    if killed > 0 and survived == 0 and timeout == 0:
        assert score == 1.0

    report = MutationReport(
        runner_name="ast_mutant_engine",
        target_files=["module.py"],
        total_mutants=total,
        killed=killed,
        survived=survived,
        timeout=timeout,
        errors=errors,
        mutation_score=score,
    )
    assert report.killed + report.survived + report.timeout + report.errors == report.total_mutants
    assert 0.0 <= report.mutation_score <= 1.0

    with pytest.raises(ValidationError):
        MutationReport(
            runner_name="ast_mutant_engine",
            total_mutants=total + 1,
            killed=killed,
            survived=survived,
            timeout=timeout,
            errors=errors,
            mutation_score=score,
        )


_SAFE_ID = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-",
    min_size=1,
    max_size=16,
)

_TRIAL_ROW_STRATEGY = st.builds(
    TrialResultRow,
    trial_id=_SAFE_ID,
    case_id=_SAFE_ID,
    condition=st.sampled_from(list(ExperimentCondition)),
    seed=st.integers(min_value=0, max_value=10_000),
    hidden_pass_at_1=st.booleans(),
    pbt_pass_rate=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    mutation_score=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    lock_verified=st.booleans(),
    stall_count=st.integers(min_value=0, max_value=50),
    wall_clock_seconds=st.floats(min_value=0.0, max_value=3600.0, allow_nan=False, allow_infinity=False),
    quota_tokens_used=st.integers(min_value=0, max_value=100_000),
    quota_requests_used=st.integers(min_value=0, max_value=1_000),
    artifacts_dir=_SAFE_ID.map(lambda s: f"trials/{s}"),
)


@settings(derandomize=True, max_examples=50, deadline=None)
@example(
    batch=[
        TrialResultRow(
            trial_id="t0",
            case_id="c0",
            condition=ExperimentCondition.COND_A,
            seed=0,
            hidden_pass_at_1=False,
            pbt_pass_rate=0.0,
            mutation_score=0.0,
            lock_verified=False,
            stall_count=0,
            wall_clock_seconds=0.0,
            quota_tokens_used=0,
            quota_requests_used=0,
            artifacts_dir="trials/t0",
        )
    ]
)
@example(
    batch=[
        TrialResultRow(
            trial_id="t1",
            case_id="c1",
            condition=ExperimentCondition.COND_C,
            seed=1,
            hidden_pass_at_1=True,
            pbt_pass_rate=1.0,
            mutation_score=1.0,
            lock_verified=True,
            stall_count=0,
            wall_clock_seconds=1.0,
            quota_tokens_used=1,
            quota_requests_used=1,
            artifacts_dir="trials/t1",
        )
    ]
)
@given(batch=st.lists(_TRIAL_ROW_STRATEGY, min_size=1, max_size=8))
def test_pbt_results_csv_schema_roundtrip_integrity(batch: list[TrialResultRow]) -> None:
    with tempfile.TemporaryDirectory(
        prefix="reqhq_csv_pbt_",
        ignore_cleanup_errors=True,
    ) as tmp_dir:
        csv_path = Path(tmp_dir) / "results.csv"
        for row in batch:
            append_trial_result_csv(csv_path, row)

        assert b"\r\n" not in csv_path.read_bytes()

        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            header = tuple(next(reader))
            data_rows = list(reader)

        assert header == RESULTS_CSV_FIELDNAMES
        assert len(data_rows) == len(batch)
        assert all(len(r) == len(RESULTS_CSV_FIELDNAMES) for r in data_rows)

        parsed = read_trial_results_csv(csv_path)
        assert len(parsed) == len(batch)
        for orig, restored in zip(batch, parsed, strict=True):
            assert restored.trial_id == orig.trial_id
            assert restored.case_id == orig.case_id
            assert restored.condition == orig.condition
            assert restored.seed == orig.seed
            assert restored.hidden_pass_at_1 == orig.hidden_pass_at_1
            assert abs(restored.pbt_pass_rate - orig.pbt_pass_rate) <= 1e-6
            assert abs(restored.mutation_score - orig.mutation_score) <= 1e-6
            assert restored.lock_verified == orig.lock_verified
            assert restored.stall_count == orig.stall_count
            assert abs(restored.wall_clock_seconds - orig.wall_clock_seconds) <= 1e-6
            assert restored.quota_tokens_used == orig.quota_tokens_used
            assert restored.quota_requests_used == orig.quota_requests_used
            assert restored.artifacts_dir == orig.artifacts_dir


@settings(derandomize=True, max_examples=50, deadline=None)
@example(rel_op="<", bool_op="and", arith_op="+", threshold=0, negate=False)
@example(rel_op="==", bool_op="or", arith_op="-", threshold=1, negate=True)
@given(
    rel_op=st.sampled_from(["<", "<=", ">", ">=", "==", "!="]),
    bool_op=st.sampled_from(["and", "or"]),
    arith_op=st.sampled_from(["+", "-"]),
    threshold=st.integers(min_value=-50, max_value=50),
    negate=st.booleans(),
)
def test_pbt_ast_mutator_produces_valid_deterministic_python(
    rel_op: str,
    bool_op: str,
    arith_op: str,
    threshold: int,
    negate: bool,
) -> None:
    guard = "not flag" if negate else "flag"
    source = (
        "def check(x: int, y: int, flag: bool) -> bool:\n"
        f"    val = x {arith_op} y\n"
        f"    return (val {rel_op} {threshold}) {bool_op} ({guard})\n"
    )
    mutants_a = generate_mutants(source, "generated.py")
    mutants_b = generate_mutants(source, "generated.py")

    assert mutants_a == mutants_b
    assert len(mutants_a) >= 4
    orig_dump = ast.dump(ast.parse(source))
    for cand in mutants_a:
        parsed_mut = ast.parse(cand.mutated_source)
        assert ast.dump(parsed_mut) != orig_dump
        assert cand.line_no >= 1
        assert cand.original_snippet != cand.mutated_snippet
