"""Unit & deterministic Hypothesis PBT tests for Stage A & Stage B large-scale evaluation extensions."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any

from hypothesis import given, settings, strategies as st
from pydantic import ValidationError
import pytest
from typer.testing import CliRunner

from reqhq.alloy.mock_analyzer import MockAlloyAnalyzer
from reqhq.alloy.models import AlloyRunStatus
from reqhq.benchmark import (
    EXTENDED_CROSS_DOMAIN_PATH,
    ExtendedCrossDomainCase,
    ExtendedCrossDomainSuite,
    load_extended_cross_domain_suite,
    load_frozen_promise_nfr_gold,
)
from reqhq.cli import app
from reqhq.evaluation import (
    ABLATION_ARMS,
    AblationItemRecord,
    AblationRunner,
    CorpusBenchmarkRunner,
    CorpusDocRecord,
    EvalOrchestrator,
    EvalRunConfig,
    EXPECTED_TASK_IDS,
    EXTENDED_CODING_TASKS_DIR,
    EXTENDED_TASK_IDS,
    LiveExecutionUnavailableError,
    RunMode,
    build_critics_for_arm,
    load_coding_suite,
    summarize_ablation_records,
    summarize_corpus_records,
)
from reqhq.evaluation.corpus_bench import _percentile
from reqhq.evaluation.runner import _build_offline_router
from reqhq.harness.live_agent import LiveLLMCodingAgent
from reqhq.harness.models import ExperimentCondition

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_FROZEN_ROOT = REPO_ROOT / "data" / "frozen"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _create_synthetic_frozen_root(root: Path, *, include_gold: bool = True, include_promise: bool = True) -> Path:
    """Create a minimal synthetic dev+test frozen tree (never touches real data/frozen/*/test/)."""
    if include_gold:
        for sp in ("dev", "test"):
            gdir = root / "gold" / sp
            gdir.mkdir(parents=True, exist_ok=True)
            gold_payload = {
                "item_id": f"synth-pure-{sp}-01",
                "prd_path": f"data/frozen/pure/text/{sp}/synth.txt",
                "provenance": {
                    "status": "FROZEN - human-verified",
                    "verified_by": ["annotator-a", "annotator-b"],
                    "implied_categories_verified": True,
                    "source_document_sha256": _sha256_bytes(f"synth-{sp}".encode("utf-8")),
                },
                "implied_categories": ["SE", "PE"],
                "gold_requirements": [
                    {
                        "id": "R1",
                        "kind": "FR",
                        "text": "The system shall authenticate users via multi-factor credentials.",
                        "source": "human-added:annotator-a",
                    },
                    {
                        "id": "R2",
                        "kind": "SE",
                        "text": "The system shall encrypt all audit logs with AES-256.",
                        "source": "llm-suggestion+human-verify",
                    },
                    {
                        "id": "R3",
                        "kind": "PE",
                        "text": "The system shall respond within 200ms at p95 under 500 concurrent requests.",
                        "source": "human-added:annotator-b",
                    },
                    {
                        "id": "R4",
                        "kind": "OP",
                        "text": "The operations team shall run monthly disaster recovery failover drills.",
                        "source": "human-added:annotator-a",
                    },
                ],
            }
            (gdir / f"synth_{sp}.gold.json").write_text(
                json.dumps(gold_payload, indent=2) + "\n", encoding="utf-8", newline="\n"
            )

    if include_promise:
        pdir = root / "promise_nfr"
        pdir.mkdir(parents=True, exist_ok=True)
        dev_csv = (
            "project_id,text,category\n"
            "1,The portal shall authenticate operators with MFA.,SE\n"
            "1,The portal shall respond within 500ms at p95.,PE\n"
            "1,The portal shall export daily audit trails.,FR\n"
            "1,   ,FR\n"
        )
        test_csv = (
            "project_id,text,category\n"
            "12,The service shall encrypt payment tokens at rest.,SE\n"
            "12,The service shall recover within 60 seconds after crash.,FT\n"
            "12,The service shall issue idempotent receipts.,FR\n"
            "12,,US\n"
        )
        dev_path = pdir / "dev.csv"
        test_path = pdir / "test.csv"
        dev_path.write_text(dev_csv, encoding="utf-8", newline="\n")
        test_path.write_text(test_csv, encoding="utf-8", newline="\n")
        freeze = {
            "dataset": "synthetic-promise-nfr",
            "files": {
                "dev": {"sha256": _sha256_bytes(dev_path.read_bytes())},
                "test": {"sha256": _sha256_bytes(test_path.read_bytes())},
            },
            "stats": {"dev_rows": 3, "test_rows": 3},
            "split_rule": "synthetic",
            "discipline": "dev-only-until-blinded-test",
        }
        (pdir / "FREEZE.json").write_text(json.dumps(freeze, indent=2) + "\n", encoding="utf-8", newline="\n")
    return root


class TestPromiseNFRGoldLoader:
    """Tests for read-only SHA-256 verified PROMISE NFR gold project loader."""

    def test_load_real_promise_nfr_dev_split(self) -> None:
        docs = load_frozen_promise_nfr_gold(REAL_FROZEN_ROOT, split="dev", verify_hashes=True)
        assert len(docs) == 11
        assert sum(len(d.requirements) for d in docs) == 437
        assert all(d.split == "dev" for d in docs)
        assert all(d.item_id.startswith("promise-nfr-proj-") for d in docs)
        assert all(len(d.source_sha256) == 64 for d in docs)

    def test_load_synthetic_promise_nfr_test_and_blank_row_skip(self, tmp_path: Path) -> None:
        root = _create_synthetic_frozen_root(tmp_path, include_gold=False, include_promise=True)
        dev_docs = load_frozen_promise_nfr_gold(root, split="dev", verify_hashes=True)
        test_docs = load_frozen_promise_nfr_gold(root, split="test", verify_hashes=True)

        assert len(dev_docs) == 1
        assert dev_docs[0].item_id == "promise-nfr-proj-01"
        assert len(dev_docs[0].requirements) == 3  # blank text row skipped
        assert dev_docs[0].implied_categories == ["PE", "SE"]

        assert len(test_docs) == 1
        assert test_docs[0].item_id == "promise-nfr-proj-12"
        assert test_docs[0].split == "test"
        assert len(test_docs[0].requirements) == 3
        assert test_docs[0].implied_categories == ["FT", "SE"]

    def test_load_promise_nfr_invalid_split_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Unsupported split"):
            load_frozen_promise_nfr_gold(tmp_path, split="invalid")  # type: ignore[arg-type]


class TestExtendedCrossDomainSuite:
    """Tests for the 48-case (8-domain) extended cross-domain benchmark suite."""

    def test_load_real_extended_cross_domain_suite(self) -> None:
        assert EXTENDED_CROSS_DOMAIN_PATH.is_file()
        assert b"\r\n" not in EXTENDED_CROSS_DOMAIN_PATH.read_bytes()

        suite = load_extended_cross_domain_suite()
        assert len(suite.cases) == 48
        domains = sorted({c.domain for c in suite.cases})
        assert len(domains) == 8
        for dom in domains:
            assert sum(1 for c in suite.cases if c.domain == dom) == 6
        assert sum(1 for c in suite.cases if c.input_kind == "one_liner") == 24
        assert sum(1 for c in suite.cases if c.input_kind == "multi_paragraph") == 24

    def test_extended_suite_validation_errors(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Extended cross-domain benchmark suite not found"):
            load_extended_cross_domain_suite(tmp_path / "missing.json")

        base_cases = [
            ExtendedCrossDomainCase(
                case_id=f"EXT-{i:02d}",
                domain="fintech",
                input_kind="one_liner" if i % 2 == 0 else "multi_paragraph",
                title=f"Case {i}",
                prompt_text=f"Prompt text {i}",
                gold_requirements=["R1: Enforce idempotency"],
                expected_routes={"R1": "software"},
                adr_rubric_anchors=["ADR-R001"],
            )
            for i in range(16)
        ]

        # Duplicate case_id rejected
        dup_cases = list(base_cases)
        dup_cases[1] = dup_cases[1].model_copy(update={"case_id": "EXT-00"})
        with pytest.raises(ValidationError, match="case_id values must be unique"):
            ExtendedCrossDomainSuite(version="1.0", cases=dup_cases)

        # Imbalanced one_liner / multi_paragraph rejected
        imbalanced = list(base_cases)
        imbalanced[1] = imbalanced[1].model_copy(update={"input_kind": "one_liner"})
        with pytest.raises(ValidationError, match="must balance one_liner"):
            ExtendedCrossDomainSuite(version="1.0", cases=imbalanced)


class TestExtendedCodingSuiteAndIsolation:
    """Tests for 12-task extended Layer 3 coding suite & zero hidden-test leakage."""

    def test_load_coding_suite_canonical_extended_and_invalid(self) -> None:
        canonical = load_coding_suite("canonical")
        assert [t.task_id for t in canonical] == list(EXPECTED_TASK_IDS)

        extended = load_coding_suite("extended")
        assert len(extended) == 12
        assert [t.task_id for t in extended[:6]] == list(EXPECTED_TASK_IDS)
        assert set(t.task_id for t in extended[6:]) == set(EXTENDED_TASK_IDS)

        capped = load_coding_suite("extended", max_tasks=8)
        assert len(capped) == 8

        with pytest.raises(ValueError, match="Unsupported coding_suite"):
            load_coding_suite("unknown")

    def test_extended_coding_tasks_have_valid_ast_and_lf_endings(self) -> None:
        assert EXTENDED_CODING_TASKS_DIR.is_dir()
        for task_id in EXTENDED_TASK_IDS:
            tdir = EXTENDED_CODING_TASKS_DIR / task_id
            assert tdir.is_dir()
            for rel in ("task_meta.json", "raw_prd.md", "single_pass_nl_spec.md", "hidden_tests/test_acceptance.py"):
                fpath = tdir / rel
                raw = fpath.read_bytes()
                assert b"\r\n" not in raw, f"CRLF in {fpath}"
            ast.parse((tdir / "hidden_tests" / "test_acceptance.py").read_text(encoding="utf-8"))

    def test_live_coding_agent_indexes_all_12_tasks_and_blocks_hidden_leakage(self) -> None:
        agent = LiveLLMCodingAgent(router=_build_offline_router())
        for task_id in (*EXPECTED_TASK_IDS, *EXTENDED_TASK_IDS):
            assert task_id in agent._task_index

        for task_id in EXTENDED_TASK_IDS:
            hidden_src = (
                EXTENDED_CODING_TASKS_DIR / task_id / "hidden_tests" / "test_acceptance.py"
            ).read_text(encoding="utf-8")
            hidden_fn_names = [
                node.name
                for node in ast.walk(ast.parse(hidden_src))
                if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
            ]
            assert hidden_fn_names
            for cond in (
                ExperimentCondition.COND_A,
                ExperimentCondition.COND_B,
                ExperimentCondition.COND_C,
            ):
                _sys_p, user_p = agent.build_condition_prompt(
                    case_id=task_id,
                    condition=cond,
                    bundle_dir=None,
                )
                assert "test_acceptance.py" not in user_p
                for fn_name in hidden_fn_names:
                    assert fn_name not in user_p


class TestCorpusBenchmarkRunnerAndPBT:
    """Tests & Hypothesis PBT for the 61-doc PURE dev (79-doc all) corpus benchmark."""

    def test_corpus_bench_mock_run_resume_and_relative_out_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        progress_lines: list[str] = []
        runner = CorpusBenchmarkRunner(
            Path("rel_corpus_out"),
            mode=RunMode.MOCK,
            split="dev",
            frozen_root=REAL_FROZEN_ROOT,
            max_docs=2,
            seed=42,
            progress_callback=progress_lines.append,
        )
        summary = runner.run()
        assert summary.total_docs == 2
        assert summary.zero_truncation_rate == 1.0
        assert summary.alloy_verified_rate == 1.0
        assert summary.alloy_non_vacuous_rate == 1.0
        assert summary.lock_verified_rate == 1.0
        assert len(progress_lines) == 4  # 2 prep + 2 completion lines
        assert progress_lines[0].startswith("[CORPUS 1/2]")
        recs = runner._load_existing_records()
        assert all(r.smart_frontend_enabled and r.cleaned_char_count < r.char_count for r in recs)
        assert (tmp_path / "rel_corpus_out" / "corpus_summary.json").is_file()
        assert b"\r\n" not in (tmp_path / "rel_corpus_out" / "corpus_summary.json").read_bytes()

        # Resume must skip both completed documents (zero duplicates, zero new progress lines)
        resume_progress: list[str] = []
        resumed = CorpusBenchmarkRunner(
            Path("rel_corpus_out"),
            mode=RunMode.MOCK,
            split="dev",
            frozen_root=REAL_FROZEN_ROOT,
            max_docs=2,
            seed=42,
            progress_callback=resume_progress.append,
        ).run()
        assert resumed.total_docs == 2
        assert resume_progress == []
        lines = [
            ln for ln in (tmp_path / "rel_corpus_out" / "corpus_bench.jsonl").read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        assert len(lines) == 2

    def test_corpus_bench_validation_empty_and_live_fail_closed(self, tmp_path: Path) -> None:
        assert _percentile([], 50.0) == 0.0
        empty_summary = summarize_corpus_records([], run_mode=RunMode.MOCK, split="dev")
        assert empty_summary.total_docs == 0
        assert empty_summary.zero_truncation_rate == 0.0

        with pytest.raises(ValueError, match="max_docs must be >= 1"):
            CorpusBenchmarkRunner(tmp_path / "bad", max_docs=0)

        with pytest.raises(LiveExecutionUnavailableError, match="without both live_router and live_alloy_analyzer"):
            CorpusBenchmarkRunner(tmp_path / "live_fail", mode=RunMode.LIVE).run()

        with pytest.raises(FileNotFoundError, match="PURE text split directory not found"):
            CorpusBenchmarkRunner(tmp_path / "no_dir", frozen_root=tmp_path / "empty_frozen").run()

        empty_text_dir = tmp_path / "empty_pure" / "pure" / "text" / "dev"
        empty_text_dir.mkdir(parents=True)
        with pytest.raises(ValueError, match="No PURE SRS .txt files found"):
            CorpusBenchmarkRunner(tmp_path / "no_txt", frozen_root=tmp_path / "empty_pure").run()

    @pytest.mark.parametrize(
        ("alloy_status", "expected_event"),
        [
            (AlloyRunStatus.COMPILE_ERROR, "ALLOY_SYNTAX"),
            (AlloyRunStatus.VACUOUS_UNSAT, "ALLOY_VACUOUS"),
            (AlloyRunStatus.COUNTEREXAMPLE_FOUND, "UNSAT_CONTRADICTION"),
        ],
    )
    def test_corpus_bench_live_mode_synthetic_all_split_and_alloy_errors(
        self,
        tmp_path: Path,
        alloy_status: AlloyRunStatus,
        expected_event: str,
    ) -> None:
        frozen = tmp_path / "frozen"
        for sp in ("dev", "test"):
            d = frozen / "pure" / "text" / sp
            d.mkdir(parents=True, exist_ok=True)
            (d / f"0001_{sp}.txt").write_text(
                "1. System Requirements\n"
                "The system shall authenticate users and maintain an immutable audit log.\n"
                "Operators shall complete onboarding training before production access.\n",
                encoding="utf-8",
                newline="\n",
            )

        out_dir = tmp_path / f"out_{alloy_status.value}"
        runner = CorpusBenchmarkRunner(
            out_dir,
            mode=RunMode.LIVE,
            split="all",
            frozen_root=frozen,
            live_router=_build_offline_router(),
            live_alloy_analyzer=MockAlloyAnalyzer(default_status=alloy_status),
        )
        summary = runner.run()
        assert summary.total_docs == 2
        assert summary.alloy_verified_rate == 0.0
        assert summary.error_count == 2
        recs = runner._load_existing_records()
        assert all(expected_event in r.error_events for r in recs)

    @settings(derandomize=True, max_examples=50)
    @given(
        st.lists(
            st.fixed_dictionaries(
                {
                    "char_count": st.integers(min_value=10, max_value=250_000),
                    "trunc": st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
                    "nodes": st.integers(min_value=0, max_value=50),
                    "findings": st.integers(min_value=0, max_value=50),
                    "pbt": st.integers(min_value=0, max_value=20),
                    "verified": st.booleans(),
                    "lock": st.booleans(),
                    "wall": st.floats(min_value=0.0, max_value=300.0, allow_nan=False, allow_infinity=False),
                    "ram": st.floats(min_value=1.0, max_value=4096.0, allow_nan=False, allow_infinity=False),
                    "errs": st.integers(min_value=0, max_value=3),
                }
            ),
            min_size=1,
            max_size=15,
        )
    )
    def test_pbt_summarize_corpus_records_invariants(self, rows: list[dict[str, Any]]) -> None:
        records = [
            CorpusDocRecord(
                doc_id=f"doc-{i}",
                rel_path=f"data/frozen/pure/text/dev/doc-{i}.txt",
                split="dev",
                run_mode=RunMode.MOCK,
                char_count=row["char_count"],
                over_120k_chars=row["char_count"] > 120_000,
                chunk_count=1,
                truncation_ratio=row["trunc"],
                node_count=row["nodes"],
                finding_count=row["findings"],
                alloy_status=(
                    AlloyRunStatus.VERIFIED.value if row["verified"] else AlloyRunStatus.COMPILE_ERROR.value
                ),
                alloy_non_vacuous=row["verified"],
                pbt_property_count=row["pbt"],
                lock_verified=row["lock"],
                wall_clock_sec=row["wall"],
                peak_ram_mb=row["ram"],
                error_events=["ALLOY_SYNTAX"] * row["errs"],
            )
            for i, row in enumerate(rows)
        ]
        summary = summarize_corpus_records(records, run_mode=RunMode.MOCK, split="dev")
        assert summary.total_docs == len(records)
        assert 0 <= summary.over_120k_docs <= summary.total_docs
        assert 0.0 <= summary.zero_truncation_rate <= 1.0
        assert 0.0 <= summary.alloy_verified_rate <= 1.0
        assert 0.0 <= summary.lock_verified_rate <= 1.0
        assert summary.median_wall_clock_sec <= summary.p95_wall_clock_sec
        assert summary.total_nodes == sum(r.node_count for r in records)
        assert summary.total_findings == sum(r.finding_count for r in records)
        assert summary.total_pbt_properties == sum(r.pbt_property_count for r in records)
        assert summary.error_count == sum(len(r.error_events) for r in records)


class TestAblationRunnerAndPBT:
    """Tests & Hypothesis PBT for Proposal B3 and leave-one-critic-out ablations."""

    def test_build_critics_for_arm_exact_counts(self) -> None:
        assert len(build_critics_for_arm("full_5_critics")) == 5
        assert len(build_critics_for_arm("b3_no_human")) == 5
        for arm in (
            "minus_functional",
            "minus_nfr",
            "minus_logical",
            "minus_process",
            "minus_training",
        ):
            subset = build_critics_for_arm(arm)
            assert len(subset) == 4

    def test_ablation_runner_mock_with_promise_dev_and_resume(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        progress: list[str] = []
        runner = AblationRunner(
            Path("rel_ablate_out"),
            mode=RunMode.MOCK,
            split="dev",
            include_promise=True,
            frozen_root=REAL_FROZEN_ROOT,
            seed=42,
            progress_callback=progress.append,
        )
        report = runner.run()
        # 3 PURE dev + 11 PROMISE dev = 14 gold projects * 7 arms = 98 records
        assert report.total_records == 98
        assert len(report.arms) == 7
        assert len(progress) == 98
        by_arm = {a.arm: a for a in report.arms}
        assert by_arm["b3_no_human"].mean_questions_asked == 0.0
        assert by_arm["b3_no_human"].mean_gap_recall == 0.0
        assert by_arm["full_5_critics"].delta_recall_vs_full == 0.0
        assert by_arm["full_5_critics"].n_items == 14

        # Resume must produce zero duplicates and zero new progress callbacks
        resume_progress: list[str] = []
        resumed = AblationRunner(
            Path("rel_ablate_out"),
            mode=RunMode.MOCK,
            split="dev",
            include_promise=True,
            frozen_root=REAL_FROZEN_ROOT,
            seed=42,
            progress_callback=resume_progress.append,
        ).run()
        assert resumed.total_records == 98
        assert resume_progress == []

    def test_ablation_runner_synthetic_all_split_live_mode_and_errors(self, tmp_path: Path) -> None:
        empty_report = summarize_ablation_records(
            [],
            run_mode=RunMode.MOCK,
            split="dev",
            include_promise=False,
            seed=42,
        )
        assert empty_report.total_records == 0
        assert all(a.n_items == 0 for a in empty_report.arms)

        with pytest.raises(LiveExecutionUnavailableError, match="without live_router"):
            AblationRunner(tmp_path / "live_fail", mode=RunMode.LIVE).run()

        empty_gold_dir = tmp_path / "empty_gold" / "gold" / "dev"
        empty_gold_dir.mkdir(parents=True)
        with pytest.raises(ValueError, match="No verified gold documents available"):
            AblationRunner(
                tmp_path / "no_docs",
                include_promise=False,
                frozen_root=tmp_path / "empty_gold",
            ).run()

        synth_root = _create_synthetic_frozen_root(tmp_path / "synth")
        report = AblationRunner(
            tmp_path / "all_live",
            mode=RunMode.LIVE,
            split="all",
            include_promise=True,
            frozen_root=synth_root,
            live_router=_build_offline_router(),
        ).run()
        # 2 PURE (dev+test) + 2 PROMISE (dev+test) = 4 docs * 7 arms = 28 records
        assert report.total_records == 28
        assert report.split == "all"

    def test_ablation_runner_evaluates_each_critic_once_per_doc_and_supports_partial_arm_resume(
        self, tmp_path: Path
    ) -> None:
        from reqhq.elicitation.critics import CriticPanel
        from reqhq.evaluation.ablations import _graph_from_retained_clauses
        from reqhq.benchmark.gap_injector import GapInjector
        from reqhq.benchmark.models import GapClause
        from reqhq.evaluation.runner import _extract_keywords, _kind_to_gap_category

        synth_root = _create_synthetic_frozen_root(
            tmp_path / "synth_once", include_gold=True, include_promise=False
        )
        base_router = _build_offline_router()
        routed_stages: list[tuple[str, str]] = []
        orig_route = base_router.route

        def _counting_route(request: Any, *args: Any, **kwargs: Any) -> Any:
            routed_stages.append(
                (str(request.metadata.get("stage", "")), str(request.metadata.get("critic", "")))
            )
            return orig_route(request, *args, **kwargs)

        base_router.route = _counting_route  # type: ignore[method-assign]

        out_dir = tmp_path / "ablate_once"
        runner = AblationRunner(
            out_dir,
            mode=RunMode.LIVE,
            split="dev",
            include_promise=False,
            frozen_root=synth_root,
            seed=42,
            live_router=base_router,
        )
        report = runner.run()
        assert report.total_records == 7
        # Exactly 5 critic evaluations for 1 doc across all 7 arms (0 arbitration calls, 0 redundant passes)
        assert len(routed_stages) == 5
        assert all(stage == "critic" for stage, _ in routed_stages)
        assert len({critic for _, critic in routed_stages}) == 5

        # Verify findings count matches direct CriticPanel.run_on_nodes for full_5_critics and minus_functional
        doc = runner._load_gold_docs()[0]
        clauses = [
            GapClause(
                clause_id=f"{doc.item_id}-{req['id']}",
                category=_kind_to_gap_category(req["kind"]),
                text=req["text"],
                keywords=_extract_keywords(req["text"]),
            )
            for req in doc.requirements
        ]
        injected = GapInjector(seed=42).inject(
            spec_id=doc.item_id,
            title=doc.item_id,
            clauses=clauses,
            drop_ratio=0.30,
        )
        graph = _graph_from_retained_clauses(doc, injected.manifest.retained_clauses)
        by_arm_rec = {rec.arm: rec for rec in runner._load_existing()}
        for arm_name in ("full_5_critics", "minus_functional"):
            panel_findings, _ = CriticPanel(
                _build_offline_router(), critics=build_critics_for_arm(arm_name)
            ).run_on_nodes(graph)
            assert by_arm_rec[arm_name].findings_count == len(panel_findings)

        # Partial per-arm resume: remove only minus_functional; functional critic must be skipped
        kept_lines = [
            ln
            for ln in (out_dir / "ablation_records.jsonl").read_text(encoding="utf-8").splitlines()
            if '"minus_functional"' not in ln
        ]
        (out_dir / "ablation_records.jsonl").write_text(
            "\n".join(kept_lines) + "\n", encoding="utf-8", newline="\n"
        )
        routed_stages.clear()
        resumed = AblationRunner(
            out_dir,
            mode=RunMode.LIVE,
            split="dev",
            include_promise=False,
            frozen_root=synth_root,
            seed=42,
            live_router=base_router,
        ).run()
        assert resumed.total_records == 7
        assert len(routed_stages) == 4
        assert "functional_completeness" not in {critic for _, critic in routed_stages}

    @settings(derandomize=True, max_examples=50)
    @given(
        st.lists(
            st.fixed_dictionaries(
                {
                    "arm": st.sampled_from(ABLATION_ARMS),
                    "findings": st.integers(min_value=0, max_value=30),
                    "bm": st.integers(min_value=0, max_value=30),
                    "q": st.integers(min_value=0, max_value=30),
                    "recall": st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
                    "prec": st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
                    "f1": st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
                    "wall": st.floats(min_value=0.0, max_value=60.0, allow_nan=False, allow_infinity=False),
                }
            ),
            min_size=1,
            max_size=21,
        )
    )
    def test_pbt_summarize_ablation_records_invariants(self, rows: list[dict[str, Any]]) -> None:
        records = [
            AblationItemRecord(
                record_id=f"ABL:{row['arm']}:item-{i}",
                arm=row["arm"],
                item_id=f"item-{i}",
                split="dev",
                run_mode=RunMode.MOCK,
                active_critics=5 if row["arm"] in ("full_5_critics", "b3_no_human") else 4,
                findings_count=row["findings"],
                blocker_major_count=min(row["bm"], row["findings"]),
                questions_asked=row["q"],
                gap_recall=row["recall"],
                precision=row["prec"],
                f1=row["f1"],
                wall_clock_sec=row["wall"],
            )
            for i, row in enumerate(rows)
        ]
        report = summarize_ablation_records(
            records,
            run_mode=RunMode.MOCK,
            split="dev",
            include_promise=True,
            seed=42,
        )
        assert report.total_records == len(records)
        assert len(report.arms) == len(ABLATION_ARMS)
        assert sum(a.n_items for a in report.arms) == len(records)
        by_arm = {a.arm: a for a in report.arms}
        if by_arm["full_5_critics"].n_items > 0:
            assert by_arm["full_5_critics"].delta_recall_vs_full == 0.0
            assert by_arm["full_5_critics"].delta_findings_vs_full == 0.0


class TestEvalOrchestratorAndCLILargeScaleExtensions:
    """Integration & CLI tests for --include-promise, extended suites, corpus-bench, and ablate."""

    def test_eval_orchestrator_include_promise_synthetic_all_split(self, tmp_path: Path) -> None:
        synth_root = _create_synthetic_frozen_root(tmp_path / "synth")
        cfg = EvalRunConfig(
            out_dir=tmp_path / "eval_l1_all",
            layers=[1],
            mode=RunMode.MOCK,
            split="all",
            include_promise=True,
            seed=42,
        )
        manifest = EvalOrchestrator(cfg, frozen_root=synth_root).run_all()
        assert manifest.record_counts["layer1"] == 4

    def test_eval_orchestrator_extended_layer2_and_layer3_mock(self, tmp_path: Path) -> None:
        cfg_l2 = EvalRunConfig(
            out_dir=tmp_path / "eval_l2_ext",
            layers=[2],
            mode=RunMode.MOCK,
            cross_domain_suite="extended",
            seed=42,
        )
        manifest_l2 = EvalOrchestrator(cfg_l2).run_all()
        assert manifest_l2.record_counts["layer2"] == 48

        cfg_l3 = EvalRunConfig(
            out_dir=tmp_path / "eval_l3_ext",
            layers=[3],
            mode=RunMode.MOCK,
            coding_suite="extended",
            max_tasks=7,
            replicates=1,
            seed=42,
        )
        manifest_l3 = EvalOrchestrator(cfg_l3).run_all()
        # 7 tasks (6 canonical + 1 extended) * 3 conditions * 1 replicate = 21 records
        assert manifest_l3.record_counts["layer3"] == 21

    def test_cli_eval_run_with_include_promise(self, tmp_path: Path) -> None:
        runner = CliRunner()
        out_dir = tmp_path / "cli_l1_promise"
        res = runner.invoke(
            app,
            [
                "eval",
                "run",
                "--layers",
                "1",
                "--include-promise",
                "--mode",
                "mock",
                "--out",
                str(out_dir),
            ],
        )
        assert res.exit_code == 0, res.output
        assert "layer1=14" in res.output

    def test_cli_eval_corpus_bench_mock_live_and_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = CliRunner()
        out_mock = tmp_path / "cli_corpus_mock"
        res = runner.invoke(
            app,
            [
                "eval",
                "corpus-bench",
                "--mode",
                "mock",
                "--raw-frontend",
                "--max-docs",
                "1",
                "--out",
                str(out_mock),
            ],
        )
        assert res.exit_code == 0, res.output
        assert "docs=1" in res.output

        # Live CLI hook wiring test (monkeypatched preflight/hooks to keep offline CI zero-network)
        monkeypatch.setattr(
            "reqhq.cli._build_live_eval_hooks",
            lambda **kwargs: {
                "live_router": _build_offline_router(),
                "live_alloy_analyzer": MockAlloyAnalyzer(default_status=AlloyRunStatus.VERIFIED),
            },
        )
        out_live = tmp_path / "cli_corpus_live"
        res_live = runner.invoke(
            app,
            [
                "eval",
                "corpus-bench",
                "--mode",
                "live",
                "--max-docs",
                "1",
                "--out",
                str(out_live),
            ],
        )
        assert res_live.exit_code == 0, res_live.output
        assert "mode=live" in res_live.output

        # ADR directory guard refuses writes
        res_err = runner.invoke(
            app,
            [
                "eval",
                "corpus-bench",
                "--out",
                str(tmp_path / "docs" / "adr"),
            ],
        )
        assert res_err.exit_code == 1

    def test_cli_eval_ablate_mock_live_and_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = CliRunner()
        out_mock = tmp_path / "cli_ablate_mock"
        res = runner.invoke(
            app,
            [
                "eval",
                "ablate",
                "--mode",
                "mock",
                "--no-include-promise",
                "--out",
                str(out_mock),
            ],
        )
        assert res.exit_code == 0, res.output
        assert "records=21" in res.output  # 3 PURE dev * 7 arms

        monkeypatch.setattr(
            "reqhq.cli._build_live_eval_hooks",
            lambda **kwargs: {"live_router": _build_offline_router()},
        )
        out_live = tmp_path / "cli_ablate_live"
        res_live = runner.invoke(
            app,
            [
                "eval",
                "ablate",
                "--mode",
                "live",
                "--no-include-promise",
                "--out",
                str(out_live),
            ],
        )
        assert res_live.exit_code == 0, res_live.output
        assert "mode=live" in res_live.output

        res_err = runner.invoke(
            app,
            [
                "eval",
                "ablate",
                "--out",
                str(tmp_path / "docs" / "adr"),
            ],
        )
        assert res_err.exit_code == 1
