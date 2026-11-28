"""Phase 4 CLI end-to-end tests (`reqhq eval ...`) plus Hypothesis PBT for layer parsing."""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from reqhq.cli import _parse_layers_option, app
from reqhq.evaluation.models import (
    EvalBundleManifest,
    Layer1Record,
    Layer2Record,
    Layer3Record,
    Layer4TelemetryRecord,
    RunMode,
)
from reqhq.reporting.generator import DRY_RUN_BANNER_MD, DRY_RUN_BANNER_TEX

runner = CliRunner()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_jsonl(path: Path, records: list[object]) -> None:
    lines = [
        json.dumps(rec.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        for rec in records
    ]
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def _write_verified_live_eval_dir(eval_dir: Path) -> None:
    """Create a complete, SHA-256-verified simulated live evaluation directory for CLI E2E."""
    eval_dir.mkdir(parents=True, exist_ok=True)
    l1_records = [
        Layer1Record(
            record_id=f"L1:item-{idx}",
            item_id=f"pure-000{idx}",
            split="dev",
            seed=42 + idx,
            run_mode=RunMode.LIVE,
            gold_verified=True,
            single_pass_recall=0.45,
            iterative_recall=0.88,
            iterative_precision=0.90,
            iterative_f1=0.89,
            all_cloud_f1=0.90,
            questions_asked=4,
            exhaustive_baseline_questions=12,
            redundant_question_rate=0.05,
            cloud_requests=2,
            all_cloud_requests=20,
            cloud_tokens=800,
            all_cloud_tokens=10000,
            wall_clock_sec=1.5,
            error_events=["QUOTA_FALLBACK: free-tier ceiling near 90%"] if idx == 0 else [],
        )
        for idx in range(6)
    ]
    l2_records = [
        Layer2Record(
            record_id=f"L2:case-{idx}",
            case_id=f"case-{idx}",
            domain="fintech",
            input_kind="one_liner" if idx % 2 == 0 else "multi_paragraph",
            run_mode=RunMode.LIVE,
            route_accuracy=1.0,
            alloy_contradictions_caught=2,
            llm_only_contradictions_caught=0,
            alloy_caught_binary=True,
            llm_only_caught_binary=False,
            routing_adr_rubric_score=0.92,
            software_only_rubric_score=0.60,
            cohens_kappa=0.85,
            wall_clock_sec=2.0,
            error_events=["UNSAT_CONTRADICTION: mutual exclusion"] if idx == 0 else [],
        )
        for idx in range(8)
    ]
    l3_records: list[Layer3Record] = []
    for task_idx in range(6):
        for rep in (1, 2):
            for cond, pass_at_1, pbt_rate, mut_score in (
                ("raw_prd", False, 0.30, 0.25),
                ("single_pass_nl_spec", False, 0.55, 0.45),
                ("reqhq_full_bundle", True, 0.96, 0.88),
            ):
                l3_records.append(
                    Layer3Record(
                        record_id=f"L3:task-{task_idx}:{cond}:r{rep}",
                        task_id=f"task-{task_idx}",
                        domain="fintech",
                        condition=cond,
                        replicate=rep,
                        seed=42 + rep,
                        run_mode=RunMode.LIVE,
                        alloy_formalized=True,
                        alloy_non_vacuous=True,
                        lock_verified=True,
                        hidden_pass_at_1=pass_at_1,
                        pbt_pass_rate=pbt_rate,
                        mutation_score=mut_score,
                        stall_count=0,
                        wall_clock_sec=1.0,
                        cloud_requests=1 if cond == "reqhq_full_bundle" else 5,
                        cloud_tokens=500 if cond == "reqhq_full_bundle" else 4000,
                        error_events=[],
                    )
                )
    l4_records = [
        Layer4TelemetryRecord(
            stage="layer1_elicitation",
            run_mode=RunMode.LIVE,
            hardware_tier=0,
            gpu_name="GTX-1650",
            vram_gb=2.0,
            ram_gb=16.0,
            total_steps=20,
            local_only_steps=18,
            local_only_pct=90.0,
            cloud_requests=2,
            all_cloud_baseline_requests=20,
            cloud_tokens=1000,
            all_cloud_baseline_tokens=10000,
            cloud_reduction_pct=90.0,
            wall_clock_sec=3.0,
            peak_ram_mb=256.0,
            peak_vram_mb=1200.0,
            error_events=[],
        ),
        Layer4TelemetryRecord(
            stage="layer3_coding",
            run_mode=RunMode.LIVE,
            hardware_tier=0,
            gpu_name="GTX-1650",
            vram_gb=2.0,
            ram_gb=16.0,
            total_steps=36,
            local_only_steps=32,
            local_only_pct=88.9,
            cloud_requests=4,
            all_cloud_baseline_requests=36,
            cloud_tokens=2000,
            all_cloud_baseline_tokens=20000,
            cloud_reduction_pct=90.0,
            wall_clock_sec=6.0,
            peak_ram_mb=310.0,
            peak_vram_mb=1400.0,
            error_events=[],
        ),
    ]

    files = {
        "layer1": eval_dir / "layer1.jsonl",
        "layer2": eval_dir / "layer2.jsonl",
        "layer3": eval_dir / "layer3.jsonl",
        "layer4_telemetry": eval_dir / "layer4_telemetry.jsonl",
    }
    _write_jsonl(files["layer1"], l1_records)
    _write_jsonl(files["layer2"], l2_records)
    _write_jsonl(files["layer3"], l3_records)
    _write_jsonl(files["layer4_telemetry"], l4_records)

    manifest = EvalBundleManifest(
        run_id="eval-live-cli-e2e",
        run_mode=RunMode.LIVE,
        seed=42,
        replicates=2,
        layers_completed=[1, 2, 3, 4],
        gold_verified=True,
        hardware_profile={"tier": "TIER_0", "ram_gb": 16.0, "vram_gb": 2.0},
        log_files={k: p.name for k, p in files.items()},
        log_sha256={k: _sha256(p) for k, p in files.items()},
        record_counts={
            "layer1": len(l1_records),
            "layer2": len(l2_records),
            "layer3": len(l3_records),
            "layer4_telemetry": len(l4_records),
        },
        created_at="2026-11-25T10:00:00Z",
    )
    with (eval_dir / "eval_manifest.json").open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")


def test_cli_phase4_offline_mock_quarantine_e2e_and_error_paths(tmp_path: Path) -> None:
    """Full offline CLI pipeline: run -> resume -> analyze -> strict-live fail -> report -> package -> verify + tamper."""
    eval_dir = tmp_path / "mock_run"
    analysis_dir = eval_dir / "analysis"
    report_dir = eval_dir / "report"
    bundle_dir = tmp_path / "mock_bundle"

    # 1. eval run --mode mock
    res_run = runner.invoke(
        app,
        [
            "eval",
            "run",
            "--layers",
            "1,2,3,4",
            "--out",
            str(eval_dir),
            "--mode",
            "mock",
            "--replicates",
            "2",
            "--seed",
            "42",
            "--max-tasks",
            "2",
        ],
    )
    assert res_run.exit_code == 0, res_run.output
    assert "Evaluation complete:" in res_run.output
    assert (eval_dir / "eval_manifest.json").is_file()
    layer3_before = (eval_dir / "layer3.jsonl").read_text(encoding="utf-8")

    # 2. eval resume (idempotent zero duplicates)
    res_resume = runner.invoke(app, ["eval", "resume", "--out", str(eval_dir)])
    assert res_resume.exit_code == 0, res_resume.output
    assert "Evaluation resumed:" in res_resume.output
    assert (eval_dir / "layer3.jsonl").read_text(encoding="utf-8") == layer3_before

    # 3. eval analyze (quarantined dry-run succeeds cleanly without --strict-live)
    res_analyze = runner.invoke(
        app,
        [
            "eval",
            "analyze",
            "--in",
            str(eval_dir),
            "--out",
            str(analysis_dir),
            "--resamples",
            "200",
        ],
    )
    assert res_analyze.exit_code == 0, res_analyze.output
    assert "DRY_RUN_UNSCORED" in res_analyze.output
    assert "QUARANTINED_MOCK_OR_INCOMPLETE" in res_analyze.output

    # Verify all 4 SVGs parse cleanly as XML and carry DRY-RUN MOCK DATA watermark
    svg_names = (
        "pareto_quality_vs_cloud_cost.svg",
        "pareto_quality_vs_latency.svg",
        "cond_abc_comparison.svg",
        "error_taxonomy_bar.svg",
    )
    for name in svg_names:
        svg_text = (analysis_dir / name).read_text(encoding="utf-8")
        ET.fromstring(svg_text)
        assert "DRY-RUN MOCK DATA" in svg_text

    # 4. eval analyze --strict-live MUST fail closed on mock data
    res_strict = runner.invoke(
        app,
        [
            "eval",
            "analyze",
            "--in",
            str(eval_dir),
            "--out",
            str(tmp_path / "strict_out"),
            "--strict-live",
        ],
    )
    assert res_strict.exit_code == 1
    assert "Error:" in res_strict.output

    # 5. eval report (quarantined banner, zero VERIFIED_LIVE/SUPPORTED/REFUTED leaks)
    res_report = runner.invoke(
        app,
        [
            "eval",
            "report",
            "--in",
            str(analysis_dir),
            "--out",
            str(report_dir),
            "--format",
            "both",
        ],
    )
    assert res_report.exit_code == 0, res_report.output
    assert "DRY_RUN_UNSCORED" in res_report.output
    md_text = (report_dir / "FINAL_REPORT.md").read_text(encoding="utf-8")
    tex_text = (report_dir / "tables_latex.tex").read_text(encoding="utf-8")
    assert DRY_RUN_BANNER_MD in md_text
    assert DRY_RUN_BANNER_TEX in tex_text
    assert "VERIFIED_LIVE" not in md_text
    assert "VERIFIED_LIVE" not in tex_text

    # 6. eval package & eval verify-bundle
    res_pkg = runner.invoke(
        app,
        ["eval", "package", "--in", str(eval_dir), "--out", str(bundle_dir)],
    )
    assert res_pkg.exit_code == 0, res_pkg.output
    manifest_path = bundle_dir / "artifacts_manifest.json"
    assert manifest_path.is_file()

    res_verify_ok = runner.invoke(app, ["eval", "verify-bundle", str(manifest_path)])
    assert res_verify_ok.exit_code == 0, res_verify_ok.output
    assert "Bundle verification PASSED:" in res_verify_ok.output

    # 7. Tamper 1 byte in a packaged artifact and verify fail-closed exit code 1
    target_report = bundle_dir / "report" / "FINAL_REPORT.md"
    target_report.write_bytes(target_report.read_bytes() + b"X")
    res_verify_bad = runner.invoke(app, ["eval", "verify-bundle", str(manifest_path)])
    assert res_verify_bad.exit_code == 1
    assert "Bundle verification FAILED:" in res_verify_bad.output

    # 8. Error paths & docs/adr/ namespace refusal across eval commands
    adr_target = tmp_path / "docs" / "adr" / "oops"
    res_bad_layers = runner.invoke(
        app, ["eval", "run", "--layers", "1,9", "--out", str(tmp_path / "bad_l")]
    )
    assert res_bad_layers.exit_code == 1

    res_empty_layers = runner.invoke(
        app, ["eval", "run", "--layers", " , ", "--out", str(tmp_path / "bad_e")]
    )
    assert res_empty_layers.exit_code == 1

    res_adr_guard = runner.invoke(app, ["eval", "run", "--out", str(adr_target)])
    assert res_adr_guard.exit_code == 1
    assert "Refusing to write evaluation output" in res_adr_guard.output

    res_missing_resume = runner.invoke(
        app, ["eval", "resume", "--out", str(tmp_path / "does_not_exist")]
    )
    assert res_missing_resume.exit_code == 1

    res_bad_report = runner.invoke(
        app,
        ["eval", "report", "--in", str(tmp_path / "missing_analysis"), "--out", str(tmp_path / "r")],
    )
    assert res_bad_report.exit_code == 1

    res_bad_pkg = runner.invoke(
        app,
        ["eval", "package", "--in", str(tmp_path / "missing_in"), "--out", str(tmp_path / "p")],
    )
    assert res_bad_pkg.exit_code == 1


def test_cli_phase4_simulated_live_verified_e2e(tmp_path: Path) -> None:
    """Simulated SHA-256-verified live manifest flows through analyze --strict-live -> report -> package -> verify-bundle."""
    eval_dir = tmp_path / "live_run"
    analysis_dir = eval_dir / "analysis"
    report_dir = eval_dir / "report"
    bundle_dir = tmp_path / "live_bundle"
    _write_verified_live_eval_dir(eval_dir)

    res_analyze = runner.invoke(
        app,
        [
            "eval",
            "analyze",
            "--in",
            str(eval_dir),
            "--out",
            str(analysis_dir),
            "--strict-live",
            "--resamples",
            "300",
        ],
    )
    assert res_analyze.exit_code == 0, res_analyze.output
    assert "VERIFIED_LIVE" in res_analyze.output
    assert "quarantined=False" in res_analyze.output

    for svg_name in (
        "pareto_quality_vs_cloud_cost.svg",
        "pareto_quality_vs_latency.svg",
        "cond_abc_comparison.svg",
        "error_taxonomy_bar.svg",
    ):
        svg_text = (analysis_dir / svg_name).read_text(encoding="utf-8")
        ET.fromstring(svg_text)
        assert "DRY-RUN MOCK DATA" not in svg_text

    res_report = runner.invoke(
        app,
        [
            "eval",
            "report",
            "--in",
            str(analysis_dir),
            "--out",
            str(report_dir),
            "--format",
            "both",
        ],
    )
    assert res_report.exit_code == 0, res_report.output
    assert "VERIFIED_LIVE" in res_report.output
    md_text = (report_dir / "FINAL_REPORT.md").read_text(encoding="utf-8")
    tex_text = (report_dir / "tables_latex.tex").read_text(encoding="utf-8")
    assert DRY_RUN_BANNER_MD not in md_text
    assert "\\toprule" in tex_text and "\\bottomrule" in tex_text

    res_pkg = runner.invoke(
        app,
        ["eval", "package", "--in", str(eval_dir), "--out", str(bundle_dir)],
    )
    assert res_pkg.exit_code == 0, res_pkg.output
    assert "VERIFIED_LIVE" in res_pkg.output

    res_verify = runner.invoke(
        app, ["eval", "verify-bundle", str(bundle_dir / "artifacts_manifest.json")]
    )
    assert res_verify.exit_code == 0, res_verify.output
    assert "Bundle verification PASSED:" in res_verify.output


@settings(derandomize=True, max_examples=50)
@given(
    layers=st.lists(st.integers(min_value=1, max_value=4), min_size=1, max_size=8),
)
def test_pbt_parse_layers_option_deduplicates_and_sorts(layers: list[int]) -> None:
    """PBT: _parse_layers_option returns a sorted unique subset of {1, 2, 3, 4}."""
    raw = ", ".join(str(layer) for layer in layers)
    parsed = _parse_layers_option(raw)
    assert parsed == sorted(set(layers))
    assert all(item in (1, 2, 3, 4) for item in parsed)
