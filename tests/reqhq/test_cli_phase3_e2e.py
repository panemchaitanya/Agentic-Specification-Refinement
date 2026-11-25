"""Full offline end-to-end tests for ReqHQ v2 Phase 3 CLI commands and handoff/benchmark workflows."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from typer.testing import CliRunner

from reqhq.cli import app
from reqhq.harness import (
    ASTMutantEngine,
    ExperimentCondition,
    ExperimentHarness,
    MockCodingAgent,
)
from reqhq.pbt import load_pbt_spec_yaml, verify_contract_lock
from reqhq.store.event_log import SessionEventLog

runner = CliRunner()


class TestPhase3CLIEndToEnd:
    """Exercise full Phase 1 -> Phase 2 -> Phase 3 CLI pipeline 100% offline."""

    def test_full_pipeline_init_to_pbt_export_handoff_experiment_and_benchmarks(
        self,
        tmp_path: Path,
    ) -> None:
        sessions_dir = tmp_path / "sessions"
        answers_file = tmp_path / "answers.json"
        answers_file.write_text(
            json.dumps(
                {
                    "turns": [
                        [
                            {
                                "answer": "All settlement API requests require OAuth2, p99 <= 200ms, and non-negative amounts."
                            },
                            {
                                "answer": "Finance operations execute a manual SOP dual-approval for transfers > $10,000."
                            },
                        ]
                    ]
                }
            ),
            encoding="utf-8",
        )

        # 1. init --prompt
        res_init = runner.invoke(
            app,
            [
                "init",
                "--session-id",
                "p3-e2e",
                "--sessions-dir",
                str(sessions_dir),
                "--hardware-tier",
                "0",
                "--strict-local",
                "--prompt",
                (
                    "Build a fintech ledger settlement API with strict balance conservation, "
                    "a manual treasury SOP dual-approval workflow, and operator onboarding training."
                ),
            ],
        )
        assert res_init.exit_code == 0, res_init.output

        # 2. elicit
        res_elicit = runner.invoke(
            app,
            [
                "elicit",
                "p3-e2e",
                "--sessions-dir",
                str(sessions_dir),
                "--answers-file",
                str(answers_file),
                "--max-turns",
                "2",
                "--mock",
            ],
        )
        assert res_elicit.exit_code == 0, res_elicit.output

        # 3. finalize --accept-defaults
        res_fin = runner.invoke(
            app,
            [
                "finalize",
                "p3-e2e",
                "--accept-defaults",
                "--mock",
                "--sessions-dir",
                str(sessions_dir),
            ],
        )
        assert res_fin.exit_code == 0, res_fin.output

        # 4. verify (--mock-alloy)
        res_verify = runner.invoke(
            app,
            [
                "verify",
                "p3-e2e",
                "--alloy-compat",
                "alloy6",
                "--mock-alloy",
                "--sessions-dir",
                str(sessions_dir),
            ],
        )
        assert res_verify.exit_code == 0, res_verify.output

        # 5. export --out
        export_dir = tmp_path / "phase2_bundle"
        res_export = runner.invoke(
            app,
            [
                "export",
                "p3-e2e",
                "--out",
                str(export_dir),
                "--alloy-compat",
                "alloy6",
                "--mock-alloy",
                "--sessions-dir",
                str(sessions_dir),
            ],
        )
        assert res_export.exit_code == 0, res_export.output
        assert (export_dir / "Spec.md").is_file()

        # 6. pbt-export
        pbt_dir = tmp_path / "pbt_bundle"
        res_pbt = runner.invoke(
            app,
            [
                "pbt-export",
                "p3-e2e",
                "--out",
                str(pbt_dir),
                "--mock",
                "--sessions-dir",
                str(sessions_dir),
            ],
        )
        assert res_pbt.exit_code == 0, res_pbt.output
        assert "Exported PBT guardrail bundle for 'p3-e2e'" in res_pbt.output
        assert (pbt_dir / "PBT_Spec.yaml").is_file()
        assert (pbt_dir / "pbt_lock.sha256").is_file()
        loaded_pbt = load_pbt_spec_yaml((pbt_dir / "PBT_Spec.yaml").read_text(encoding="utf-8"))
        assert len(loaded_pbt.properties) >= 1
        assert verify_contract_lock(pbt_dir)

        for py_file in [*(pbt_dir / "guardrails").glob("*.py"), *(pbt_dir / "tests" / "properties").glob("*.py")]:
            ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))

        # 7. handoff (alloy6)
        handoff_dir = tmp_path / "handoff_bundle"
        res_handoff = runner.invoke(
            app,
            [
                "handoff",
                "p3-e2e",
                "--out",
                str(handoff_dir),
                "--alloy-compat",
                "alloy6",
                "--mock",
                "--mock-alloy",
                "--sessions-dir",
                str(sessions_dir),
            ],
        )
        assert res_handoff.exit_code == 0, res_handoff.output
        assert "Exported Antigravity handoff bundle for 'p3-e2e'" in res_handoff.output
        assert (handoff_dir / "HANDOFF_INSTRUCTIONS.md").is_file()
        assert (handoff_dir / "Spec.md").is_file()
        assert (handoff_dir / "Design.md").is_file()
        assert (handoff_dir / "Process_and_Training.md").is_file()
        assert (handoff_dir / "PBT_Spec.yaml").is_file()
        assert (handoff_dir / "pbt_lock.sha256").is_file()
        assert verify_contract_lock(handoff_dir)

        matrix = json.loads((handoff_dir / "traceability_matrix.json").read_text(encoding="utf-8"))
        assert matrix["orphaned_req_ids"] == []
        assert any(row["pbt_refs"] for row in matrix["rows"])

        # 8. Simulated Condition A / B / C experiment + AST mutation engine run
        sample_pkg = tmp_path / "sample_mut_target"
        sample_pkg.mkdir()
        target_mod = sample_pkg / "ledger_core.py"
        target_mod.write_text(
            "def can_settle(balance: int, amount: int) -> bool:\n"
            "    return amount > 0 and balance >= amount\n",
            encoding="utf-8",
        )
        target_test = sample_pkg / "test_ledger_core.py"
        target_test.write_text(
            "from ledger_core import can_settle\n\n"
            "def test_settle_boundaries() -> None:\n"
            "    assert can_settle(100, 100) is True\n"
            "    assert can_settle(100, 1) is True\n"
            "    assert can_settle(100, 0) is False\n"
            "    assert can_settle(50, 100) is False\n",
            encoding="utf-8",
        )
        mut_report = ASTMutantEngine(timeout_seconds=15.0, max_mutants=10).run(
            sample_pkg,
            [target_mod],
            [target_test],
        )
        assert mut_report.total_mutants >= 1
        assert mut_report.killed >= 1
        assert 0.0 < mut_report.mutation_score <= 1.0

        exp_out = tmp_path / "exp_trials"
        harness = ExperimentHarness(
            agent=MockCodingAgent(),
            mutation_runner=ASTMutantEngine(timeout_seconds=15.0, max_mutants=5),
        )
        rows = [
            harness.run_trial(
                case_id="FIN-01",
                condition=cond,
                seed=42,
                out_dir=exp_out,
                bundle_dir=handoff_dir if cond == ExperimentCondition.COND_C else None,
            )
            for cond in (
                ExperimentCondition.COND_A,
                ExperimentCondition.COND_B,
                ExperimentCondition.COND_C,
            )
        ]
        assert len(rows) == 3
        cond_c_row = next(r for r in rows if r.condition == ExperimentCondition.COND_C)
        assert cond_c_row.lock_verified is True
        assert (exp_out / "results.csv").is_file()

        # 9. reqhq benchmark run --suite cross_domain and gap_injection
        bench_out = tmp_path / "bench_out"
        res_cd = runner.invoke(
            app,
            [
                "benchmark",
                "run",
                "--suite",
                "cross_domain",
                "--out",
                str(bench_out),
                "--seed",
                "42",
                "--mock",
            ],
        )
        assert res_cd.exit_code == 0, res_cd.output
        assert "Benchmark 'cross_domain' complete: cases=16" in res_cd.output
        assert (bench_out / "cross_domain_results.json").is_file()

        res_gi = runner.invoke(
            app,
            [
                "benchmark",
                "run",
                "--suite",
                "gap_injection",
                "--out",
                str(bench_out),
                "--seed",
                "42",
                "--mock",
            ],
        )
        assert res_gi.exit_code == 0, res_gi.output
        assert "Benchmark 'gap_injection' complete: cases=16" in res_gi.output
        assert "leakage_verified=True" in res_gi.output
        assert (bench_out / "gap_injection_results.json").is_file()

        # 10. reqhq benchmark kappa
        ann_a = tmp_path / "ann_a.json"
        ann_b = tmp_path / "ann_b.json"
        ann_a.write_text(
            json.dumps(
                [
                    {"item_id": "C1", "label": "software", "skipped": False, "verified": True},
                    {"item_id": "C2", "label": "process", "skipped": False, "verified": True},
                    {"item_id": "C3", "label": "training", "skipped": False, "verified": True},
                    {"item_id": "C4", "label": "hybrid", "skipped": False, "verified": True},
                ]
            ),
            encoding="utf-8",
        )
        ann_b.write_text(
            json.dumps(
                [
                    {"item_id": "C1", "label": "software", "skipped": False, "verified": True},
                    {"item_id": "C2", "label": "process", "skipped": False, "verified": True},
                    {"item_id": "C3", "label": "process", "skipped": False, "verified": True},
                    {"item_id": "C4", "label": "hybrid", "skipped": False, "verified": True},
                ]
            ),
            encoding="utf-8",
        )
        res_kappa = runner.invoke(app, ["benchmark", "kappa", str(ann_a), str(ann_b)])
        assert res_kappa.exit_code == 0, res_kappa.output
        assert "Cohen's kappa:" in res_kappa.output
        assert "Disagreed IDs: C3" in res_kappa.output

        assert SessionEventLog(sessions_dir).verify_jsonl_parity("p3-e2e") is True

    def test_phase3_cli_error_paths_and_namespace_guards(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / "sessions"
        assert (
            runner.invoke(
                app,
                ["init", "--session-id", "empty-p3", "--sessions-dir", str(sessions_dir)],
            ).exit_code
            == 0
        )

        # Missing session errors
        assert (
            runner.invoke(
                app,
                ["pbt-export", "missing-sess", "--out", str(tmp_path / "o1"), "--sessions-dir", str(sessions_dir)],
            ).exit_code
            == 1
        )
        assert (
            runner.invoke(
                app,
                ["handoff", "missing-sess", "--out", str(tmp_path / "o2"), "--sessions-dir", str(sessions_dir)],
            ).exit_code
            == 1
        )

        # Missing elicitation state errors
        assert (
            runner.invoke(
                app,
                ["pbt-export", "empty-p3", "--out", str(tmp_path / "o3"), "--mock", "--sessions-dir", str(sessions_dir)],
            ).exit_code
            == 1
        )
        assert (
            runner.invoke(
                app,
                ["handoff", "empty-p3", "--out", str(tmp_path / "o4"), "--mock-alloy", "--sessions-dir", str(sessions_dir)],
            ).exit_code
            == 1
        )

        # Namespace separation guard on handoff (--out .../docs/adr)
        assert (
            runner.invoke(
                app,
                [
                    "init",
                    "--session-id",
                    "ns-p3",
                    "--prompt",
                    "Build a REST API service with database persistence.",
                    "--sessions-dir",
                    str(sessions_dir),
                ],
            ).exit_code
            == 0
        )
        assert (
            runner.invoke(
                app,
                ["elicit", "ns-p3", "--max-turns", "1", "--mock", "--sessions-dir", str(sessions_dir)],
            ).exit_code
            == 0
        )
        res_ns_pbt = runner.invoke(
            app,
            [
                "pbt-export",
                "ns-p3",
                "--out",
                str(tmp_path / "docs" / "adr"),
                "--mock",
                "--sessions-dir",
                str(sessions_dir),
            ],
        )
        assert res_ns_pbt.exit_code == 1
        assert "Refusing to write" in res_ns_pbt.output

        res_ns = runner.invoke(
            app,
            [
                "handoff",
                "ns-p3",
                "--out",
                str(tmp_path / "docs" / "adr"),
                "--mock-alloy",
                "--sessions-dir",
                str(sessions_dir),
            ],
        )
        assert res_ns.exit_code == 1
        assert "Refusing to write" in res_ns.output

        # benchmark kappa error path (missing / skipped fail-closed)
        res_k_missing = runner.invoke(
            app,
            ["benchmark", "kappa", str(tmp_path / "no_a.json"), str(tmp_path / "no_b.json")],
        )
        assert res_k_missing.exit_code == 1
        assert "Error:" in res_k_missing.output
