"""Unit and deterministic Hypothesis property-based tests for Phase 4 reporting & packaging."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings, strategies as st
from pydantic import ValidationError

from reqhq.reporting import (
    DRY_RUN_BANNER_MD,
    DRY_RUN_BANNER_TEX,
    EXPECTED_SVGS,
    MANIFEST_FILENAME,
    QUARANTINED_STATUS,
    TAXONOMY_CATEGORIES,
    TEAM_AUTHORS,
    TEAM_INSTITUTION,
    TEAM_NAME,
    ArtifactsManifest,
    BundleTamperError,
    BundleVerificationResult,
    ReleaseArtifactEntry,
    ReportGenerationResult,
    escape_latex,
    generate_final_report,
    package_release_bundle,
    verify_bundle,
)
from reqhq.reporting.packager import _validate_safe_rel_path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SAMPLE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="40">'
    '<text x="5" y="20">ReqHQ</text></svg>\n'
)


def _make_live_summary() -> dict[str, Any]:
    return {
        "run_id": "live-2026-11-25-001",
        "run_mode": "live",
        "quarantined": False,
        "quarantine_reasons": [],
        "verdict_status": "VERIFIED_LIVE",
        "seed": 42,
        "replicates": 3,
        "hardware_profile": {
            "tier": 0,
            "gpu_name": "Intel Iris Xe_4GB",
            "vram_gb": 4.0,
            "ram_gb": 16.0,
        },
        "hypotheses": [
            {
                "hypothesis_id": "H1",
                "title": "Iterative recall > single_pass & 50% fewer questions #1",
                "status": "SUPPORTED",
                "metrics": {"iterative_recall": 0.91, "single_pass_recall": 0.68},
                "p_values": {"wilcoxon_p": 0.0041},
                "bootstrap_cis": {
                    "recall_diff": {
                        "mean_diff": 0.23,
                        "ci_low": 0.14,
                        "ci_high": 0.31,
                        "n_resamples": 2000,
                        "alpha": 0.05,
                        "seed": 42,
                    }
                },
                "rationale": "95% CI excludes zero and question count dropped 58%.",
            },
            {
                "hypothesis_id": "H2",
                "title": "Local-first TIER_0 F1 parity & >=80% cloud reduction",
                "status": "SUPPORTED",
                "metrics": {"local_first_f1": 0.87, "cloud_reduction_pct": 86.5},
                "p_values": {"wilcoxon_p": 0.012},
                "bootstrap_cis": {"f1_diff": "0.01 [-0.02, 0.04]"},
                "rationale": "Within 0.05 F1 of all-cloud with 86.5% reduction.",
            },
            {
                "hypothesis_id": "H3",
                "title": "Alloy 6 contradiction detection & socio-technical ADRs",
                "status": "SUPPORTED",
                "metrics": {"alloy_caught": 14, "llm_only_caught": 6, "alloy_non_vacuous": True},
                "p_values": {"mcnemar_p": 0.0078},
                "bootstrap_cis": {},
                "rationale": "Alloy 6 caught significantly more contradictions.",
            },
            {
                "hypothesis_id": "H4",
                "title": "Cond C > Cond A/B on Pass@1, PBT & mutation score",
                "status": "REFUTED",
                "metrics": {"cond_c_mutation": 0.84, "cond_a_mutation": 0.61},
                "p_values": {"mcnemar_p": 0.0625},
                "bootstrap_cis": {},
                "rationale": "Did not reach significance threshold on sample size.",
            },
        ],
        "taxonomy": {
            "total_failures": 5,
            "categories": [
                {
                    "category": "SCHEMA_DRIFT",
                    "count": 2,
                    "share_pct": 40.0,
                    "sample_ids": ["L1_item_01", "L1_item_02"],
                },
                {
                    "category": "ALLOY_VACUOUS",
                    "count": 1,
                    "share_pct": 20.0,
                    "sample_ids": ["L3_task_02"],
                },
                {
                    "category": "PBT_COUNTEREXAMPLE",
                    "count": 2,
                    "share_pct": 40.0,
                    "sample_ids": ["L3_task_05", "L3_task_06"],
                },
            ],
        },
        "pareto_cost_points": [
            {"label": "TIER_0_Local & Cached", "quality": 0.89, "cost": 12.0, "is_frontier": True},
            {"label": "All_Cloud_Baseline", "quality": 0.90, "cost": 100.0, "is_frontier": True},
            {"label": "Single_Pass_50%", "quality": 0.65, "cost": 45.0, "is_frontier": False},
        ],
        "pareto_latency_points": [
            {"label": "TIER_0_Local", "quality": 0.89, "cost": 18.5, "is_frontier": True},
            {"label": "Single_Pass", "quality": 0.65, "cost": 22.0, "is_frontier": False},
        ],
        "svg_files": {
            "pareto_cost": "pareto_quality_vs_cloud_cost.svg",
            "pareto_latency": "pareto_quality_vs_latency.svg",
            "cond_abc": "cond_abc_comparison.svg",
            "taxonomy": "error_taxonomy_bar.svg",
            "traversal_attempt": "../escape.svg",
        },
        "layer_metrics": {"layer1_count": 3},
    }


def _write_svg_set(target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for filename, _caption in EXPECTED_SVGS:
        (target_dir / filename).write_text(_SAMPLE_SVG, encoding="utf-8", newline="\n")


def _assert_no_unescaped_latex_specials(escaped: str) -> None:
    """Assert every %, _, &, #, $ in escaped LaTeX is preceded by an odd number of backslashes."""
    for match in re.finditer(r"[%_&#$]", escaped):
        idx = match.start()
        backslashes = 0
        pos = idx - 1
        while pos >= 0 and escaped[pos] == "\\":
            backslashes += 1
            pos -= 1
        assert backslashes % 2 == 1, (
            f"Unescaped {match.group(0)!r} at index {idx} in {escaped!r}"
        )


# ---------------------------------------------------------------------------
# Mandatory Hypothesis Property-Based Tests (derandomize=True, max_examples=50)
# ---------------------------------------------------------------------------


@settings(derandomize=True, max_examples=50)
@given(
    text=st.text(
        alphabet=st.characters(
            blacklist_categories=("Cs",),
        )
        | st.sampled_from(list(r"\{}$&#_%~^")),
        max_size=120,
    )
)
def test_pbt_escape_latex_never_leaves_unescaped_specials(text: str) -> None:
    escaped = escape_latex(text)
    _assert_no_unescaped_latex_specials(escaped)
    assert "~" not in escaped
    assert "^" not in escaped


@settings(derandomize=True, max_examples=50, deadline=None)
@given(
    payload=st.binary(min_size=1, max_size=512),
    offset_seed=st.integers(min_value=0, max_value=10_000),
    xor_mask=st.integers(min_value=1, max_value=255),
)
def test_pbt_packager_verify_bundle_catches_single_byte_tamper(
    payload: bytes,
    offset_seed: int,
    xor_mask: int,
) -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        in_dir = tmp_path / "in"
        out_dir = tmp_path / "bundle"
        in_dir.mkdir(parents=True, exist_ok=True)
        tracked_file = in_dir / "layer1.jsonl"
        tracked_file.write_bytes(payload)

        manifest = package_release_bundle(in_dir, out_dir, frozen_root=tmp_path / "no_frozen")
        manifest_path = out_dir / MANIFEST_FILENAME
        assert len(manifest.artifacts) == 1

        intact = verify_bundle(manifest_path, raise_on_error=True)
        assert intact.valid is True
        assert intact.checked_count == 1
        assert intact.mismatched_files == []

        tamper_target = out_dir / "layer1.jsonl"
        mutated = bytearray(tamper_target.read_bytes())
        idx = offset_seed % len(mutated)
        mutated[idx] ^= xor_mask
        tamper_target.write_bytes(bytes(mutated))

        tampered = verify_bundle(manifest_path, raise_on_error=False)
        assert tampered.valid is False
        assert "layer1.jsonl" in tampered.mismatched_files
        assert tampered.errors

        with pytest.raises(BundleTamperError):
            verify_bundle(manifest_path, raise_on_error=True)


@settings(derandomize=True, max_examples=50, deadline=None)
@given(
    run_mode=st.sampled_from(["mock", "dry_run", "pilot", "staging", "live"]),
    quarantined_flag=st.booleans(),
    adversarial_status=st.sampled_from(["SUPPORTED", "REFUTED", "VERIFIED_LIVE", "INCONCLUSIVE"]),
    adversarial_token=st.sampled_from(
        ["VERIFIED_LIVE", "SUPPORTED", "REFUTED", "claim_SUPPORTED_100%_&_#1"]
    ),
)
def test_pbt_mock_quarantine_never_emits_live_or_hypothesis_claims(
    run_mode: str,
    quarantined_flag: bool,
    adversarial_status: str,
    adversarial_token: str,
) -> None:
    # Ensure at least one quarantine trigger is active (run_mode != "live" OR quarantined is True)
    effective_quarantined = True if run_mode == "live" else quarantined_flag
    summary: dict[str, Any] = {
        "run_id": f"run-{adversarial_token}",
        "run_mode": run_mode,
        "quarantined": effective_quarantined,
        "quarantine_reasons": [f"reason containing {adversarial_token}"],
        "verdict_status": "VERIFIED_LIVE",
        "seed": 42,
        "replicates": 1,
        "hardware_profile": {"tier": 0, "gpu_name": adversarial_token, "vram_gb": 4.0, "ram_gb": 16.0},
        "hypotheses": [
            {
                "hypothesis_id": hid,
                "title": f"{hid} {adversarial_token}",
                "status": adversarial_status,
                "metrics": {f"m_{adversarial_token}": 0.99},
                "p_values": {"p": 0.001},
                "bootstrap_cis": {},
                "rationale": f"{adversarial_status}: {adversarial_token}",
            }
            for hid in ("H1", "H2", "H3", "H4")
        ],
        "taxonomy": {
            "total_failures": 1,
            "categories": [
                {
                    "category": "SCHEMA_DRIFT",
                    "count": 1,
                    "share_pct": 100.0,
                    "sample_ids": [adversarial_token],
                }
            ],
        },
        "pareto_cost_points": [
            {"label": adversarial_token, "quality": 0.8, "cost": 10.0, "is_frontier": True}
        ],
        "pareto_latency_points": [],
        "svg_files": {},
    }

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        in_dir = tmp_path / "analysis"
        out_dir = tmp_path / "report"
        in_dir.mkdir(parents=True, exist_ok=True)
        (in_dir / "analysis_summary.json").write_text(
            json.dumps(summary, indent=2),
            encoding="utf-8",
            newline="\n",
        )

        result = generate_final_report(in_dir, out_dir, fmt="both")
        assert result.quarantined is True
        assert result.verdict_status == "DRY_RUN_UNSCORED"
        assert result.report_md is not None
        assert result.tables_tex is not None

        md_text = result.report_md.read_text(encoding="utf-8")
        tex_text = result.tables_tex.read_text(encoding="utf-8")

        assert DRY_RUN_BANNER_MD in md_text
        assert DRY_RUN_BANNER_TEX in tex_text
        assert QUARANTINED_STATUS in md_text
        assert escape_latex(QUARANTINED_STATUS) in tex_text

        for forbidden in ("VERIFIED_LIVE", "SUPPORTED", "REFUTED"):
            assert forbidden not in md_text
            assert forbidden not in tex_text


# ---------------------------------------------------------------------------
# Unit Tests: Models
# ---------------------------------------------------------------------------


def test_models_strict_validation_and_invariants() -> None:
    valid_hash = "a" * 64
    entry = ReleaseArtifactEntry(
        rel_path="FINAL_REPORT.md",
        sha256=valid_hash.upper(),
        bytes=128,
        category="report_markdown",
    )
    assert entry.sha256 == valid_hash

    with pytest.raises(ValidationError):
        ReleaseArtifactEntry(rel_path="   ", sha256=valid_hash, bytes=1, category="log")

    with pytest.raises(ValidationError):
        ReleaseArtifactEntry(rel_path="a.txt", sha256="z" * 64, bytes=1, category="log")

    with pytest.raises(ValidationError):
        ReportGenerationResult(
            out_dir=Path("out"),
            verdict_status="VERIFIED_LIVE",
            quarantined=True,
        )

    with pytest.raises(ValidationError):
        ArtifactsManifest(
            run_id="r1",
            run_mode="mock",
            verdict_status="VERIFIED_LIVE",
            created_at="2026-11-25T00:00:00Z",
            seed=42,
            pinned_models={"TIER_0": "qwen3:4b"},
            frozen_dataset_hashes={},
            artifacts=[],
        )

    with pytest.raises(ValidationError):
        ArtifactsManifest(
            run_id="   ",
            run_mode="mock",
            verdict_status="DRY_RUN_UNSCORED",
            created_at="2026-11-25T00:00:00Z",
            seed=42,
            pinned_models={},
            frozen_dataset_hashes={},
            artifacts=[],
        )

    with pytest.raises(ValidationError):
        BundleVerificationResult(valid=True, checked_count=0, unexpected_field=1)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Unit Tests: Report Generator (Live, Quarantined, Formats, Edge Cases)
# ---------------------------------------------------------------------------


def test_generate_final_report_verified_live_both_md_and_tex(tmp_path: Path) -> None:
    analysis_dir = tmp_path / "analysis"
    out_dir = tmp_path / "final_report"
    _write_svg_set(analysis_dir)
    (analysis_dir / "analysis_summary.json").write_text(
        json.dumps(_make_live_summary(), indent=2),
        encoding="utf-8",
        newline="\n",
    )

    result = generate_final_report(analysis_dir, out_dir, fmt="both")
    assert result.quarantined is False
    assert result.verdict_status == "VERIFIED_LIVE"
    assert result.report_md == out_dir / "FINAL_REPORT.md"
    assert result.tables_tex == out_dir / "tables_latex.tex"

    md_bytes = result.report_md.read_bytes()
    tex_bytes = result.tables_tex.read_bytes()
    assert b"\r" not in md_bytes
    assert b"\r" not in tex_bytes

    md_text = md_bytes.decode("utf-8")
    tex_text = tex_bytes.decode("utf-8")

    # Team metadata & institution
    assert TEAM_NAME in md_text
    assert TEAM_INSTITUTION in md_text
    for author in TEAM_AUTHORS:
        assert author in md_text

    # Required report sections
    for section_fragment in (
        "## 1. Abstract",
        "## 2. Introduction",
        "src/specpipe/",
        "src/reqhq/",
        "TIER_0",
        "Alloy 6",
        "## 6. Experimental Setup",
        "## 7. Results: Hypotheses H1–H4",
        "## 8. Embedded SVG Pareto & Taxonomy Figures",
        "## 9. Error Analysis (7-Category Taxonomy)",
        "## 10. Threats to Validity",
        "SystemContextBase",
        "## 12. Conclusion",
        "VERIFIED_LIVE",
        "`SUPPORTED`",
        "`REFUTED`",
    ):
        assert section_fragment in md_text

    assert DRY_RUN_BANNER_MD not in md_text

    # All 7 taxonomy categories present
    for cat in TAXONOMY_CATEGORIES:
        assert cat in md_text
        assert escape_latex(cat) in tex_text

    # All 4 SVGs copied and embedded
    for svg_name, _caption in EXPECTED_SVGS:
        assert (out_dir / svg_name).is_file()
        assert f"({svg_name})" in md_text

    # Booktabs LaTeX structure and escaped table cells
    assert r"\toprule" in tex_text
    assert r"\midrule" in tex_text
    assert r"\bottomrule" in tex_text
    assert DRY_RUN_BANNER_TEX not in tex_text
    for line in tex_text.splitlines():
        if line.strip().startswith("%"):
            continue
        if r"\\" in line and "&" in line:
            # Split table row cells on unescaped column separator " & "
            cells = line.rsplit(r"\\", 1)[0].split(" & ")
            for cell in cells:
                _assert_no_unescaped_latex_specials(cell)


def test_generate_final_report_formats_and_edge_cases(tmp_path: Path) -> None:
    analysis_dir = tmp_path / "analysis"
    analysis_dir.mkdir()
    minimal_mock = {
        "run_id": "mock-minimal",
        "run_mode": "mock",
        "quarantined": False,
        "verdict_status": "DRY_RUN_UNSCORED",
        "hypotheses": [],
        "taxonomy": {"total_failures": 0, "categories": []},
        "pareto_cost_points": [],
        "pareto_latency_points": [],
        "svg_files": {},
    }
    summary_file = analysis_dir / "analysis_summary.json"
    summary_file.write_text(json.dumps(minimal_mock), encoding="utf-8", newline="\n")

    # Direct file path input + fmt="md" only (and in-place out_dir == analysis_dir)
    md_only = generate_final_report(summary_file, analysis_dir, fmt="md")
    assert md_only.report_md == analysis_dir / "FINAL_REPORT.md"
    assert md_only.tables_tex is None
    assert md_only.quarantined is True
    assert "not present in analysis directory" in md_only.report_md.read_text(encoding="utf-8")

    # fmt="tex" only
    tex_out = tmp_path / "tex_only"
    tex_only = generate_final_report(analysis_dir, tex_out, fmt="tex")
    assert tex_only.report_md is None
    assert tex_only.tables_tex == tex_out / "tables_latex.tex"
    assert "None recorded" in tex_only.tables_tex.read_text(encoding="utf-8")

    # Live mode with quarantined=True and empty quarantine_reasons triggers default reason
    live_q_dir = tmp_path / "live_q"
    live_q_dir.mkdir()
    _write_svg_set(live_q_dir)
    (live_q_dir / "analysis_summary.json").write_text(
        json.dumps(
            {
                "run_id": "live-q",
                "run_mode": "live",
                "quarantined": True,
                "quarantine_reasons": [],
                "verdict_status": "DRY_RUN_UNSCORED",
                "pareto_cost_points": ["not-a-dict"],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    res_q = generate_final_report(live_q_dir, live_q_dir, fmt="both")
    assert res_q.quarantined is True
    assert "quarantined fail-closed" in res_q.report_md.read_text(encoding="utf-8")  # type: ignore[union-attr]

    # Error branches
    with pytest.raises(ValueError, match="Unsupported report format"):
        generate_final_report(analysis_dir, tmp_path / "bad_fmt", fmt="pdf")  # type: ignore[arg-type]

    with pytest.raises(FileNotFoundError):
        generate_final_report(tmp_path / "does_not_exist", tmp_path / "out")

    bad_json_dir = tmp_path / "bad_json"
    bad_json_dir.mkdir()
    (bad_json_dir / "analysis_summary.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        generate_final_report(bad_json_dir, tmp_path / "out")


# ---------------------------------------------------------------------------
# Unit Tests: Packager & Tamper-Evident Bundle Verification
# ---------------------------------------------------------------------------


def test_package_and_verify_release_bundle_live_and_frozen_hashes(tmp_path: Path) -> None:
    in_dir = tmp_path / "eval_run"
    in_dir.mkdir()
    (in_dir / "nested").mkdir()
    (in_dir / "ignored.tmp").write_text("do not track", encoding="utf-8")
    (in_dir / MANIFEST_FILENAME).write_text("{}", encoding="utf-8")

    (in_dir / "eval_manifest.json").write_text(
        json.dumps(
            {
                "run_id": "live-2026-11-25-001",
                "run_mode": "live",
                "seed": 77,
                "replicates": 3,
                "gold_verified": True,
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    (in_dir / "layer1.jsonl").write_text('{"record_id": "L1-1"}\n', encoding="utf-8", newline="\n")
    (in_dir / "analysis_summary.json").write_text(
        json.dumps(_make_live_summary()),
        encoding="utf-8",
        newline="\n",
    )
    _write_svg_set(in_dir / "nested")
    generate_final_report(in_dir, in_dir, fmt="both")

    bundle_dir = tmp_path / "release_bundle"
    manifest = package_release_bundle(
        in_dir,
        bundle_dir,
        frozen_root=REPO_ROOT / "data" / "frozen",
    )
    assert manifest.run_id == "live-2026-11-25-001"
    assert manifest.run_mode == "live"
    assert manifest.verdict_status == "VERIFIED_LIVE"
    assert manifest.seed == 42
    assert set(manifest.pinned_models.keys()) == {"TIER_0", "TIER_1", "TIER_2", "TIER_3"}
    assert "qwen3:4b" in manifest.pinned_models["TIER_0"]

    # Frozen dataset SHA-256 hashes match real files in data/frozen/ (read-only)
    for key in ("pure/FREEZE.json", "promise_nfr/FREEZE.json"):
        expected = hashlib.sha256((REPO_ROOT / "data" / "frozen" / key).read_bytes()).hexdigest()
        assert manifest.frozen_dataset_hashes[key] == expected

    # Manifest written with LF endings and ignored.tmp excluded
    manifest_file = bundle_dir / MANIFEST_FILENAME
    assert b"\r" not in manifest_file.read_bytes()
    tracked_rel_paths = {entry.rel_path for entry in manifest.artifacts}
    assert "ignored.tmp" not in tracked_rel_paths
    assert MANIFEST_FILENAME not in tracked_rel_paths
    assert "FINAL_REPORT.md" in tracked_rel_paths
    assert "tables_latex.tex" in tracked_rel_paths
    assert "eval_manifest.json" in tracked_rel_paths
    assert "layer1.jsonl" in tracked_rel_paths
    assert "analysis_summary.json" in tracked_rel_paths

    # Verify via both file path and directory path
    res_file = verify_bundle(manifest_file, raise_on_error=True)
    assert res_file.valid is True
    assert res_file.checked_count == len(manifest.artifacts)
    res_dir = verify_bundle(bundle_dir, raise_on_error=True)
    assert res_dir.valid is True

    # In-place packaging (input_dir == out_dir)
    inplace_manifest = package_release_bundle(
        bundle_dir,
        bundle_dir,
        frozen_root=tmp_path / "missing_frozen",
    )
    assert inplace_manifest.frozen_dataset_hashes == {}
    assert verify_bundle(bundle_dir / MANIFEST_FILENAME).valid is True


def test_packager_edge_cases_and_security_rejections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(FileNotFoundError):
        package_release_bundle(tmp_path / "nonexistent", tmp_path / "out")

    # Non-dict JSON in eval_manifest.json falls back cleanly to DRY_RUN_UNSCORED
    weird_dir = tmp_path / "weird"
    weird_dir.mkdir()
    (weird_dir / "eval_manifest.json").write_text("[1, 2]", encoding="utf-8")
    m = package_release_bundle(weird_dir, tmp_path / "weird_out", frozen_root=tmp_path / "none")
    assert m.verdict_status == "DRY_RUN_UNSCORED"
    assert m.run_mode == "mock"

    # Missing manifest
    missing_res = verify_bundle(tmp_path / "no_manifest.json")
    assert missing_res.valid is False
    assert missing_res.errors
    with pytest.raises(BundleTamperError):
        verify_bundle(tmp_path / "no_manifest.json", raise_on_error=True)

    # Corrupted manifest JSON
    bad_manifest = tmp_path / "corrupt_manifest.json"
    bad_manifest.write_text("{not valid json", encoding="utf-8")
    corrupt_res = verify_bundle(bad_manifest)
    assert corrupt_res.valid is False
    with pytest.raises(BundleTamperError):
        verify_bundle(bad_manifest, raise_on_error=True)

    # Missing tracked file, duplicate entry, path traversal, backslash/drive chars
    sec_dir = tmp_path / "sec_bundle"
    sec_dir.mkdir()
    good_file = sec_dir / "layer1.jsonl"
    good_file.write_text("{}\n", encoding="utf-8")
    good_sha = hashlib.sha256(good_file.read_bytes()).hexdigest()

    crafted = {
        "schema_version": "1.0.0",
        "run_id": "sec-test",
        "run_mode": "mock",
        "verdict_status": "DRY_RUN_UNSCORED",
        "created_at": "2026-11-25T00:00:00Z",
        "seed": 42,
        "pinned_models": {"TIER_0": "qwen3:4b"},
        "frozen_dataset_hashes": {},
        "artifacts": [
            {"rel_path": "layer1.jsonl", "sha256": good_sha, "bytes": 3, "category": "layer_log"},
            {"rel_path": "layer1.jsonl", "sha256": good_sha, "bytes": 3, "category": "layer_log"},
            {"rel_path": "missing.jsonl", "sha256": good_sha, "bytes": 3, "category": "layer_log"},
            {"rel_path": "../escape.jsonl", "sha256": good_sha, "bytes": 3, "category": "layer_log"},
            {"rel_path": r"sub\bad.jsonl", "sha256": good_sha, "bytes": 3, "category": "layer_log"},
        ],
    }
    crafted_path = sec_dir / MANIFEST_FILENAME
    crafted_path.write_text(json.dumps(crafted), encoding="utf-8")
    sec_res = verify_bundle(crafted_path, raise_on_error=False)
    assert sec_res.valid is False
    assert sec_res.checked_count == 5
    assert "missing.jsonl" in sec_res.missing_files
    assert any("Duplicate artifact entry" in e for e in sec_res.errors)
    assert any("Illegal path traversal" in e for e in sec_res.errors)
    assert any("Illegal path characters" in e for e in sec_res.errors)

    # Symlink escape check (works without Windows Developer Mode via monkeypatch)
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "sym_escape.jsonl")
    resolved, err = _validate_safe_rel_path("sub/sym_escape.jsonl", sec_dir.resolve())
    assert resolved is None
    assert err is not None and "symlink" in err.lower()


# ---------------------------------------------------------------------------
# Unit Tests: Documentation Completeness (docs/final_report/README.md & REPRODUCIBILITY.md)
# ---------------------------------------------------------------------------


def test_reproducibility_and_final_report_docs_complete() -> None:
    repro_path = REPO_ROOT / "REPRODUCIBILITY.md"
    readme_path = REPO_ROOT / "docs" / "final_report" / "README.md"
    assert repro_path.is_file()
    assert readme_path.is_file()

    repro_text = repro_path.read_text(encoding="utf-8")
    readme_text = readme_path.read_text(encoding="utf-8")

    for doc_text in (repro_text, readme_text):
        assert ".venv/Scripts/python.exe" in doc_text
        assert ".venv/Scripts/python.exe -m reqhq.cli " in doc_text
        assert "-m reqhq " not in doc_text
        assert 'REQHQ_ALLOY_JAR_PATH = "C:/tools/alloy/org.alloytools.alloy.dist.jar"' in doc_text
        assert "qwen3:4b" in doc_text
        assert "phi4-mini" in doc_text
        assert "eval run" in doc_text
        assert "eval resume" in doc_text
        assert "eval analyze" in doc_text
        assert "eval report" in doc_text
        assert "eval package" in doc_text
        assert "eval verify-bundle" in doc_text
        assert TEAM_NAME in doc_text
        assert TEAM_INSTITUTION in doc_text
        for author_name in ("Panem Chaitanya Pavan Kumar", "Anjali Yadav", "Anshul Agrawal"):
            assert author_name in doc_text

    assert "data/frozen/pure/FREEZE.json" in repro_text
    assert "data/frozen/promise_nfr/FREEZE.json" in repro_text
