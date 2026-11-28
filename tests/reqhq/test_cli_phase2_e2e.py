"""End-to-end offline tests for ReqHQ v2 Phase 2 CLI flows (elicit, revise, finalize, verify, export)."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError
import pytest
from typer.testing import CliRunner

from reqhq.alloy import (
    extract_alloy_blocks,
    lint_core_domain_version_agnostic,
)
from reqhq.cli import (
    _bind_answers_to_questions,
    _build_alloy_analyzer,
    _build_router,
    _parse_answers_file,
    app,
    main,
)
from reqhq.config import ReqHQConfig
from reqhq.store.event_log import SessionEventLog
from reqhq.synthesis import parse_emitted_adr, validate_routing_adr_markdown

runner = CliRunner()


def _write_multiline_prd(path: Path) -> Path:
    prd = """# Expense Approval Platform PRD

## 1. Functional Requirements
- The system shall expose an authenticated REST API endpoint to submit expense reports with receipt attachments and validate line-item totals before persisting to the database.
- The system shall run an automated policy state machine that routes expenses above $500 to finance review and emits webhook audit notifications.

## 2. Non-Functional & Security Budgets
- The API shall respond within 200ms at p99 latency under 100 concurrent users and encrypt all stored receipts at rest using AES-256.

## 3. Operational Process & Governance
- Finance compliance officers shall execute a manual approval SOP and quarterly audit review runbook for exceptions above $5,000 before sign-off.

## 4. Training & Adoption
- New finance analysts shall complete an onboarding training workshop and certification curriculum before receiving production approval privileges.
"""
    path.write_text(prd, encoding="utf-8")
    return path


class TestPhase2ConfigExtensions:
    """Verify backward-compatible Phase 2 Alloy config fields and env parsing."""

    def test_config_alloy_defaults_and_env(self) -> None:
        default_cfg = ReqHQConfig()
        assert default_cfg.alloy_jar_path is None
        assert default_cfg.alloy_compat == "alloy6"

        env_cfg = ReqHQConfig.from_env(
            {
                "REQHQ_ALLOY_JAR_PATH": "tools/org.alloytools.alloy.dist.jar",
                "REQHQ_ALLOY_COMPAT": "LEGACY",
            }
        )
        assert env_cfg.alloy_jar_path == Path("tools/org.alloytools.alloy.dist.jar")
        assert env_cfg.alloy_compat == "legacy"

        with pytest.raises(ValidationError):
            ReqHQConfig.from_env({"REQHQ_ALLOY_COMPAT": "alloy4"})

        with pytest.raises(ValidationError):
            ReqHQConfig(unknown_extra_field="nope")  # type: ignore[call-arg]

    def test_non_mock_builder_helpers_construct_offline(self, tmp_path: Path) -> None:
        router = _build_router(sessions_dir=tmp_path, strict_local=True, mock=False)
        assert router.strict_local is True
        assert router.local_provider.name == "ollama"

        analyzer = _build_alloy_analyzer(mock_alloy=False)
        assert hasattr(analyzer, "run_check")


class TestPhase2EndToEndFlows:
    """Offline end-to-end CLI tests covering Flow A (1-liner) and Flow B (multi-page PRD)."""

    def test_flow_a_one_liner_prompt_elicit_revise_finalize_verify_export(
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
                                "answer": "All API requests require OAuth2 bearer tokens and p99 latency <= 250ms."
                            },
                            {
                                "answer": "Finance managers execute a manual SOP escalation for disbursements > $5000."
                            },
                        ],
                        [
                            {
                                "answer": "Operators complete a 2-hour onboarding workshop before production sign-off."
                            }
                        ],
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
                "flow-a",
                "--sessions-dir",
                str(sessions_dir),
                "--hardware-tier",
                "0",
                "--strict-local",
                "--prompt",
                (
                    "Build a patient intake API with role-based access control, "
                    "a manual nurse triage SOP escalation workflow, and staff onboarding training."
                ),
            ],
        )
        assert res_init.exit_code == 0, res_init.output
        assert "Initialized session: flow-a" in res_init.output
        assert "Prompt ingested" in res_init.output

        # 2. elicit with scripted answers (turn 1 critics + turn 2 answers + turn 3 answers)
        res_elicit = runner.invoke(
            app,
            [
                "elicit",
                "flow-a",
                "--sessions-dir",
                str(sessions_dir),
                "--answers-file",
                str(answers_file),
                "--max-turns",
                "3",
                "--mock",
            ],
        )
        assert res_elicit.exit_code == 0, res_elicit.output
        assert "Elicitation session 'flow-a':" in res_elicit.output

        # 3. revise turn-002 (selective delta re-evaluation + downstream turn-003 invalidation)
        res_revise = runner.invoke(
            app,
            [
                "revise",
                "flow-a",
                "turn-002",
                "--note",
                "Require MFA + 150ms p99 SLA and nurse lead sign-off on escalations.",
                "--mock",
                "--sessions-dir",
                str(sessions_dir),
            ],
        )
        assert res_revise.exit_code == 0, res_revise.output
        assert "Revised turn 'turn-002' in session 'flow-a'." in res_revise.output
        assert "Delta re-evaluation complete:" in res_revise.output

        # 4. finalize without --accept-defaults fails convergence gate if open BLOCKER/MAJOR remain
        res_fin_blocked = runner.invoke(
            app,
            [
                "finalize",
                "flow-a",
                "--mock",
                "--sessions-dir",
                str(sessions_dir),
            ],
        )
        assert res_fin_blocked.exit_code == 1
        assert "Cannot finalize session" in res_fin_blocked.output

        # 5. finalize --accept-defaults records unconfirmed Assumption ADRs
        res_fin = runner.invoke(
            app,
            [
                "finalize",
                "flow-a",
                "--accept-defaults",
                "--mock",
                "--sessions-dir",
                str(sessions_dir),
            ],
        )
        assert res_fin.exit_code == 0, res_fin.output
        assert "Finalized session 'flow-a':" in res_fin.output
        assert "accepted_defaults=True" in res_fin.output

        # 6. verify with Alloy 6 + vacuity gate (--mock-alloy)
        res_verify = runner.invoke(
            app,
            [
                "verify",
                "flow-a",
                "--alloy-compat",
                "alloy6",
                "--mock-alloy",
                "--max-scope",
                "4",
                "--sessions-dir",
                str(sessions_dir),
            ],
        )
        assert res_verify.exit_code == 0, res_verify.output
        assert "Verification for session 'flow-a': PASSED" in res_verify.output
        assert "Alloy verification (alloy6): VERIFIED | vacuity_checked=True" in res_verify.output

        # 7. export --out full Phase 2 synthesis bundle
        bundle_dir = tmp_path / "export_flow_a"
        res_export = runner.invoke(
            app,
            [
                "export",
                "flow-a",
                "--out",
                str(bundle_dir),
                "--alloy-compat",
                "alloy6",
                "--mock-alloy",
                "--sessions-dir",
                str(sessions_dir),
            ],
        )
        assert res_export.exit_code == 0, res_export.output
        assert "Exported synthesis bundle for 'flow-a'" in res_export.output
        assert "orphans=0" in res_export.output

        # Validate all exported artifacts
        spec_path = bundle_dir / "Spec.md"
        design_path = bundle_dir / "Design.md"
        proc_train_path = bundle_dir / "Process_and_Training.md"
        matrix_path = bundle_dir / "traceability_matrix.json"
        adrs_dir = bundle_dir / "ADRs"

        assert spec_path.is_file()
        assert design_path.is_file()
        assert proc_train_path.is_file()
        assert matrix_path.is_file()
        assert adrs_dir.is_dir()

        spec_md = spec_path.read_text(encoding="utf-8")
        blocks = extract_alloy_blocks(spec_md)
        assert "core_domain" in blocks and "behavior" in blocks
        assert lint_core_domain_version_agnostic(blocks["core_domain"].code) == []
        assert "var " in blocks["behavior"].code
        assert "always" in blocks["behavior"].code

        routing_adr_files = sorted(adrs_dir.glob("ADR-R*.md"))
        assumption_adr_files = sorted(adrs_dir.glob("ADR-A*.md"))
        assert len(routing_adr_files) >= 1
        assert len(assumption_adr_files) >= 1

        for radr in routing_adr_files:
            parsed_routing = validate_routing_adr_markdown(radr.read_text(encoding="utf-8"))
            assert parsed_routing.adr_id.startswith("ADR-R")

        for aadr in assumption_adr_files:
            fm, _ = parse_emitted_adr(aadr.read_text(encoding="utf-8"))
            assert fm["type"] == "assumption"
            assert fm["user_confirmed"] is False

        design_md = design_path.read_text(encoding="utf-8")
        assert "Functional Core" in design_md
        assert "```mermaid" in design_md

        pt_md = proc_train_path.read_text(encoding="utf-8")
        assert "SOPs" in pt_md and "Training" in pt_md

        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        assert matrix["session_id"] == "flow-a"
        assert matrix["orphaned_req_ids"] == []
        assert len(matrix["rows"]) >= 1
        assert any(row["user_confirmed"] is False for row in matrix["rows"])
        for row in matrix["rows"]:
            assert len(row["source_sha256"]) == 64
            assert len(row["adr_ids"]) >= 1
            assert len(row["alloy_refs"]) >= 1

        # Verify event log JSONL/SQLite parity remained intact throughout
        assert SessionEventLog(sessions_dir).verify_jsonl_parity("flow-a") is True

    def test_flow_b_multipage_prd_alloy6_and_legacy_modes(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / "sessions"
        prd_path = _write_multiline_prd(tmp_path / "enterprise_prd.md")
        answers_file = tmp_path / "prd_answers.json"
        answers_file.write_text(
            json.dumps(
                [
                    "Validate receipt schema strictly with max 10MB upload and idempotent submission keys.",
                    "Escalate exceptions over $5000 to finance director within 24 hours.",
                    "Require annual refresher certification for all finance reviewers.",
                ]
            ),
            encoding="utf-8",
        )

        # 1. init --source <prd.md>
        res_init = runner.invoke(
            app,
            [
                "init",
                "--session-id",
                "flow-b",
                "--sessions-dir",
                str(sessions_dir),
                "--hardware-tier",
                "0",
                "--source",
                str(prd_path),
            ],
        )
        assert res_init.exit_code == 0, res_init.output
        assert "Ingested 1 source(s)" in res_init.output

        # 2. elicit
        res_elicit = runner.invoke(
            app,
            [
                "elicit",
                "flow-b",
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

        # 3. verify in BOTH alloy6 and legacy modes
        for mode in ("alloy6", "legacy"):
            res_verify = runner.invoke(
                app,
                [
                    "verify",
                    "flow-b",
                    "--alloy-compat",
                    mode,
                    "--mock-alloy",
                    "--sessions-dir",
                    str(sessions_dir),
                ],
            )
            assert res_verify.exit_code == 0, res_verify.output
            assert f"Alloy verification ({mode}): VERIFIED | vacuity_checked=True" in res_verify.output

        # 4. export --out in BOTH alloy6 and legacy modes
        out_alloy6 = tmp_path / "bundle_alloy6"
        out_legacy = tmp_path / "bundle_legacy"

        res_exp_a6 = runner.invoke(
            app,
            [
                "export",
                "flow-b",
                "--out",
                str(out_alloy6),
                "--alloy-compat",
                "alloy6",
                "--mock-alloy",
                "--sessions-dir",
                str(sessions_dir),
            ],
        )
        assert res_exp_a6.exit_code == 0, res_exp_a6.output

        res_exp_leg = runner.invoke(
            app,
            [
                "export",
                "flow-b",
                "--out",
                str(out_legacy),
                "--alloy-compat",
                "legacy",
                "--mock-alloy",
                "--sessions-dir",
                str(sessions_dir),
            ],
        )
        assert res_exp_leg.exit_code == 0, res_exp_leg.output

        # Verify Alloy 6 vs legacy output contract
        blocks_a6 = extract_alloy_blocks((out_alloy6 / "Spec.md").read_text(encoding="utf-8"))
        blocks_leg = extract_alloy_blocks((out_legacy / "Spec.md").read_text(encoding="utf-8"))

        assert lint_core_domain_version_agnostic(blocks_a6["core_domain"].code) == []
        assert "var " in blocks_a6["behavior"].code

        assert lint_core_domain_version_agnostic(blocks_leg["core_domain"].code) == []
        assert lint_core_domain_version_agnostic(blocks_leg["behavior"].code) == []
        assert "open util/ordering[Time]" in blocks_leg["behavior"].code

        matrix_a6 = json.loads((out_alloy6 / "traceability_matrix.json").read_text(encoding="utf-8"))
        matrix_leg = json.loads((out_legacy / "traceability_matrix.json").read_text(encoding="utf-8"))
        assert matrix_a6["orphaned_req_ids"] == []
        assert matrix_leg["orphaned_req_ids"] == []
        assert len(matrix_a6["rows"]) >= 3


class TestPhase2CLIEdgeCases:
    """Cover interactive elicit, multi-source chunk merge, answers parsing, and error guards."""

    def test_interactive_elicit_and_multi_source_init(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / "sessions"
        doc1 = tmp_path / "doc1.md"
        doc2 = tmp_path / "doc2.md"
        doc1.write_text("# API\n- System shall provide login API.\n", encoding="utf-8")
        doc2.write_text("# Training\n- Staff shall complete security onboarding workshop.\n", encoding="utf-8")

        res_init = runner.invoke(
            app,
            [
                "init",
                "--session-id",
                "multi-src",
                "--sessions-dir",
                str(sessions_dir),
                "--hardware-tier",
                "0",
                "--source",
                str(doc1),
                "--source",
                str(doc2),
            ],
        )
        assert res_init.exit_code == 0, res_init.output
        assert "Ingested 2 source(s)" in res_init.output

        # Interactive elicit: answer first prompt, then empty line to stop cleanly
        res_elicit = runner.invoke(
            app,
            [
                "elicit",
                "multi-src",
                "--sessions-dir",
                str(sessions_dir),
                "--max-turns",
                "2",
                "--mock",
            ],
            input="Use OAuth2 PKCE with 15-minute access tokens.\n\n",
        )
        assert res_elicit.exit_code == 0, res_elicit.output
        assert "Elicitation session 'multi-src':" in res_elicit.output

        # Also test loading from SnapshotStore fallback when chunking_results.json is absent
        (sessions_dir / "multi-src" / "chunking_results.json").unlink()
        (sessions_dir / "multi-src" / "elicitation_state.json").unlink()
        res_elicit_fallback = runner.invoke(
            app,
            [
                "elicit",
                "multi-src",
                "--sessions-dir",
                str(sessions_dir),
                "--max-turns",
                "1",
                "--mock",
            ],
        )
        assert res_elicit_fallback.exit_code == 0, res_elicit_fallback.output

    def test_cli_error_paths_and_namespace_guard(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / "sessions"

        # Blank prompt rejected
        res_blank = runner.invoke(
            app,
            ["init", "--prompt", "   ", "--sessions-dir", str(sessions_dir)],
        )
        assert res_blank.exit_code == 1

        # Empty session without prompt/source -> elicit and export --out fail cleanly
        res_empty = runner.invoke(
            app,
            ["init", "--session-id", "empty-sess", "--sessions-dir", str(sessions_dir)],
        )
        assert res_empty.exit_code == 0

        res_elicit_err = runner.invoke(
            app,
            ["elicit", "empty-sess", "--mock", "--sessions-dir", str(sessions_dir)],
        )
        assert res_elicit_err.exit_code == 1

        res_export_err = runner.invoke(
            app,
            [
                "export",
                "empty-sess",
                "--out",
                str(tmp_path / "out"),
                "--mock-alloy",
                "--sessions-dir",
                str(sessions_dir),
            ],
        )
        assert res_export_err.exit_code == 1

        # Missing session / answers file errors
        assert runner.invoke(app, ["elicit", "nope", "--sessions-dir", str(sessions_dir)]).exit_code == 1
        assert (
            runner.invoke(
                app,
                [
                    "elicit",
                    "empty-sess",
                    "--answers-file",
                    str(tmp_path / "missing.json"),
                    "--sessions-dir",
                    str(sessions_dir),
                ],
            ).exit_code
            == 1
        )
        bad_json = tmp_path / "bad.json"
        bad_json.write_text("{not-json", encoding="utf-8")
        assert (
            runner.invoke(
                app,
                [
                    "elicit",
                    "empty-sess",
                    "--answers-file",
                    str(bad_json),
                    "--sessions-dir",
                    str(sessions_dir),
                ],
            ).exit_code
            == 1
        )
        assert runner.invoke(app, ["finalize", "nope", "--sessions-dir", str(sessions_dir)]).exit_code == 1

        # Namespace separation guard: exporting bundle into .../docs/adr fails cleanly
        res_p = runner.invoke(
            app,
            [
                "init",
                "--session-id",
                "ns-check",
                "--prompt",
                "Build a REST API service with database persistence.",
                "--sessions-dir",
                str(sessions_dir),
            ],
        )
        assert res_p.exit_code == 0
        assert (
            runner.invoke(
                app,
                ["elicit", "ns-check", "--max-turns", "1", "--mock", "--sessions-dir", str(sessions_dir)],
            ).exit_code
            == 0
        )
        forbidden_out = tmp_path / "docs" / "adr"
        res_ns = runner.invoke(
            app,
            [
                "export",
                "ns-check",
                "--out",
                str(forbidden_out),
                "--mock-alloy",
                "--sessions-dir",
                str(sessions_dir),
            ],
        )
        assert res_ns.exit_code == 1
        assert "Refusing to write" in res_ns.output

    def test_parse_answers_file_variants(self, tmp_path: Path) -> None:
        f_answers = tmp_path / "a.json"
        f_answers.write_text(json.dumps({"answers": [{"question_id": "Q1", "answer": "Yes"}]}), encoding="utf-8")
        assert _parse_answers_file(f_answers) == [[{"question_id": "Q1", "answer": "Yes"}]]

        f_map = tmp_path / "b.json"
        f_map.write_text(json.dumps({"Q1": "Yes"}), encoding="utf-8")
        assert _parse_answers_file(f_map) == [[{"question_id": "Q1", "answer": "Yes"}]]

        f_turns = tmp_path / "c.json"
        f_turns.write_text(json.dumps([{"Q1": "A1"}, ["A2"]]), encoding="utf-8")
        assert _parse_answers_file(f_turns) == [[{"question_id": "Q1", "answer": "A1"}], [{"answer": "A2"}]]

        f_empty = tmp_path / "d.json"
        f_empty.write_text("[]", encoding="utf-8")
        assert _parse_answers_file(f_empty) == []

        f_invalid = tmp_path / "e.json"
        f_invalid.write_text("123", encoding="utf-8")
        with pytest.raises(ValueError):
            _parse_answers_file(f_invalid)

        # Flat list of answer dicts (exercises single_batch dict branch)
        f_flat_dicts = tmp_path / "f.json"
        f_flat_dicts.write_text(
            json.dumps([{"question_id": "Q1", "answer": "Auth via OAuth2"}]),
            encoding="utf-8",
        )
        assert _parse_answers_file(f_flat_dicts) == [
            [{"question_id": "Q1", "answer": "Auth via OAuth2"}]
        ]

        # Blank answer entries are skipped cleanly
        assert _bind_answers_to_questions(
            [{"question_id": "Q1", "answer": "   "}, {"question_id": "Q2", "answer": "Valid"}],
            [],
        ) == [{"question_id": "Q2", "answer": "Valid"}]

    def test_elicit_scripted_exhaustion_empty_batch_and_revise_answers_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sessions_dir = tmp_path / "sessions"
        assert (
            runner.invoke(
                app,
                [
                    "init",
                    "--session-id",
                    "edge-cli",
                    "--sessions-dir",
                    str(sessions_dir),
                    "--prompt",
                    "Build an invoice service with approval SOP and staff training.",
                ],
            ).exit_code
            == 0
        )

        # 1. Scripted answers file with only 1 turn while max_turns=4 hits batch_idx >= len(scripted_batches)
        one_turn = tmp_path / "one_turn.json"
        one_turn.write_text(
            json.dumps({"turns": [[{"answer": "Require TLS 1.3 and p99 <= 200ms."}]]}),
            encoding="utf-8",
        )
        res_one = runner.invoke(
            app,
            [
                "elicit",
                "edge-cli",
                "--sessions-dir",
                str(sessions_dir),
                "--answers-file",
                str(one_turn),
                "--max-turns",
                "4",
                "--mock",
            ],
        )
        assert res_one.exit_code == 0, res_one.output

        # 2. Scripted answers file with blank-only batch hits `if not bound_answers: break`
        blank_batch = tmp_path / "blank_batch.json"
        blank_batch.write_text(
            json.dumps({"turns": [[{"answer": "   "}]]}),
            encoding="utf-8",
        )
        res_blank = runner.invoke(
            app,
            [
                "elicit",
                "edge-cli",
                "--sessions-dir",
                str(sessions_dir),
                "--answers-file",
                str(blank_batch),
                "--max-turns",
                "3",
                "--mock",
            ],
        )
        assert res_blank.exit_code == 0, res_blank.output

        # 3. revise with --answers-file (valid file, missing file, and invalid turn_id)
        rev_answers = tmp_path / "rev_answers.json"
        rev_answers.write_text(
            json.dumps([{"answer": "Updated SLA: p99 <= 120ms and MFA mandatory."}]),
            encoding="utf-8",
        )
        res_rev_ok = runner.invoke(
            app,
            [
                "revise",
                "edge-cli",
                "turn-002",
                "--note",
                "Apply replacement answers file",
                "--answers-file",
                str(rev_answers),
                "--mock",
                "--sessions-dir",
                str(sessions_dir),
            ],
        )
        assert res_rev_ok.exit_code == 0, res_rev_ok.output

        res_rev_missing_file = runner.invoke(
            app,
            [
                "revise",
                "edge-cli",
                "turn-002",
                "--note",
                "Missing file check",
                "--answers-file",
                str(tmp_path / "does_not_exist.json"),
                "--mock",
                "--sessions-dir",
                str(sessions_dir),
            ],
        )
        assert res_rev_missing_file.exit_code == 1
        assert "answers file not found" in res_rev_missing_file.output

        res_rev_bad_turn = runner.invoke(
            app,
            [
                "revise",
                "edge-cli",
                "turn-999",
                "--note",
                "Invalid turn",
                "--mock",
                "--sessions-dir",
                str(sessions_dir),
            ],
        )
        assert res_rev_bad_turn.exit_code == 1
        assert "Error:" in res_rev_bad_turn.output

        # 4. adr sync-index error path (corrupted ADR status on disk)
        bad_adr_dir = tmp_path / "bad_adr"
        bad_adr_dir.mkdir()
        (bad_adr_dir / "0001-broken.md").write_text(
            "# 0001. Broken\n\n- **Date:** 2026-11-24\n- **Status:** InvalidStatus\n",
            encoding="utf-8",
        )
        res_sync_err = runner.invoke(
            app,
            ["adr", "sync-index", "--adr-dir", str(bad_adr_dir)],
        )
        assert res_sync_err.exit_code == 1
        assert "Error:" in res_sync_err.output

        # 5. main() console script entrypoint delegates to app()
        called: list[bool] = []
        monkeypatch.setattr("reqhq.cli.app", lambda: called.append(True))
        main()
        assert called == [True]
