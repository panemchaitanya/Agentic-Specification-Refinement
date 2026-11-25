"""Unit and deterministic Hypothesis PBT suite for Phase 4 evaluation orchestrator."""

from __future__ import annotations

import ast
import ctypes
import json
from pathlib import Path
import sys
import tempfile
import tracemalloc
import types

from hypothesis import given, settings, strategies as st
import pytest

from reqhq.alloy.mock_analyzer import MockAlloyAnalyzer
from reqhq.alloy.models import AlloyRunStatus
from reqhq.evaluation import (
    DEFAULT_CODING_TASKS_DIR,
    EXPECTED_TASK_IDS,
    LAYER_LOG_FILENAMES,
    CodingTaskMeta,
    EvalBundleManifest,
    EvalCheckpoint,
    EvalOrchestrator,
    EvalRunConfig,
    Layer1Record,
    Layer2Record,
    Layer3Record,
    Layer4TelemetryRecord,
    LiveExecutionUnavailableError,
    RunMode,
    StageTelemetryCollector,
    compute_cloud_reduction_pct,
    compute_local_only_pct,
    get_process_peak_ram_mb,
    load_coding_tasks,
    make_unit_key,
    sha256_file,
)
from reqhq.evaluation.runner import (
    _InProcessASTMutationRunner,
    _atomic_write_lf,
    _kind_to_gap_category,
    _read_jsonl,
)
from reqhq.evaluation.telemetry import _tracemalloc_peak_mb, _windows_peak_working_set_mb
from reqhq.harness.experiment import MockCodingAgent
from reqhq.llm.hardware_profiler import HardwareProfile, HardwareProfiler, HardwareTier
from reqhq.llm.providers.mock import MockProvider
from reqhq.llm.quota_ledger import QuotaLedger
from reqhq.llm.router import ProviderRouter


def _fast_profiler(vram_gb: float = 4.0) -> HardwareProfiler:
    """Deterministic offline HardwareProfiler avoiding slow external shell probes."""
    return HardwareProfiler(
        command_runner=lambda _cmd: (1, "", "disabled"),
        os_name="Windows",
        cpu_cores=8,
        ram_gb_provider=lambda: 16.0,
        directml_probe=lambda: ("AMD Radeon (TM) Graphics", vram_gb),
    )


def _assert_lf_only(path: Path) -> None:
    raw = path.read_bytes()
    assert b"\r\n" not in raw, f"CRLF detected in {path}"


# ---------------------------------------------------------------------------
# 1. Greenfield coding tasks loader & validation
# ---------------------------------------------------------------------------


def test_load_all_six_coding_tasks_and_verify_lf_and_ast() -> None:
    tasks = load_coding_tasks()
    assert len(tasks) == 6
    assert tuple(t.task_id for t in tasks) == EXPECTED_TASK_IDS

    for task in tasks:
        assert task.task_dir is not None and task.task_dir.is_dir()
        meta_path = task.task_dir / "task_meta.json"
        raw_prd = task.task_dir / "raw_prd.md"
        nl_spec = task.task_dir / "single_pass_nl_spec.md"
        hidden_test = task.task_dir / "hidden_tests" / "test_acceptance.py"

        for artifact in (meta_path, raw_prd, nl_spec, hidden_test):
            assert artifact.is_file()
            _assert_lf_only(artifact)

        tree = ast.parse(hidden_test.read_text(encoding="utf-8"), filename=str(hidden_test))
        test_funcs = [
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        ]
        assert len(test_funcs) >= 3

    subset = load_coding_tasks(max_tasks=2)
    assert len(subset) == 2
    assert [t.task_id for t in subset] == list(EXPECTED_TASK_IDS[:2])


def test_load_coding_tasks_error_paths(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_coding_tasks(tmp_path / "missing")

    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    with pytest.raises(ValueError, match="max_tasks"):
        load_coding_tasks(empty_root, max_tasks=0)
    with pytest.raises(ValueError, match="No coding tasks found"):
        load_coding_tasks(empty_root)

    missing_file_root = tmp_path / "missing_file"
    (missing_file_root / "task_a").mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match="missing required file"):
        load_coding_tasks(missing_file_root)

    def _make_task(root: Path, folder: str, task_id: str, test_src: str) -> None:
        tdir = root / folder
        (tdir / "hidden_tests").mkdir(parents=True)
        (tdir / "raw_prd.md").write_text("# raw\n", encoding="utf-8")
        (tdir / "single_pass_nl_spec.md").write_text("# spec\n", encoding="utf-8")
        (tdir / "hidden_tests" / "test_acceptance.py").write_text(test_src, encoding="utf-8")
        meta = {
            "task_id": task_id,
            "domain": "custom_domain",
            "title": "Custom Task",
            "target_module": "custom_mod",
            "entry_symbol": "run_custom",
            "required_invariants": ["inv 1"],
            "expected_routes": ["software"],
        }
        (tdir / "task_meta.json").write_text(json.dumps(meta) + "\n", encoding="utf-8")

    bad_syntax_root = tmp_path / "bad_syntax"
    _make_task(bad_syntax_root, "bad_task", "bad_task", "def broken(:\n")
    with pytest.raises(ValueError, match="Invalid Python syntax"):
        load_coding_tasks(bad_syntax_root)

    dup_root = tmp_path / "dup"
    _make_task(dup_root, "dir_1", "same_id", "def test_ok():\n    assert True\n")
    _make_task(dup_root, "dir_2", "same_id", "def test_ok():\n    assert True\n")
    with pytest.raises(ValueError, match="Duplicate coding task_id"):
        load_coding_tasks(dup_root)

    custom_root = tmp_path / "custom"
    _make_task(custom_root, "z_task", "z_custom_task", "def test_ok():\n    assert True\n")
    loaded_custom = load_coding_tasks(custom_root)
    assert [t.task_id for t in loaded_custom] == ["z_custom_task"]


# ---------------------------------------------------------------------------
# 2. Models & telemetry unit tests
# ---------------------------------------------------------------------------


def test_models_validation_and_bounds(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="blank"):
        CodingTaskMeta(
            task_id="   ",
            domain="fintech",
            title="t",
            target_module="m",
            entry_symbol="f",
            required_invariants=["inv"],
            expected_routes=["software"],
        )

    with pytest.raises(ValueError, match="Invalid expected route"):
        CodingTaskMeta(
            task_id="t1",
            domain="fintech",
            title="t",
            target_module="m",
            entry_symbol="f",
            required_invariants=["inv"],
            expected_routes=["unsupported_track"],
        )

    cfg = EvalRunConfig(out_dir=tmp_path, layers=[1, 2, 2, 4])
    assert cfg.layers == [1, 2, 4]

    with pytest.raises(ValueError, match="at least one layer"):
        EvalRunConfig(out_dir=tmp_path, layers=[])

    with pytest.raises(ValueError, match="Unsupported layer"):
        EvalRunConfig(out_dir=tmp_path, layers=[1, 9])

    with pytest.raises(ValueError, match="64-character"):
        EvalBundleManifest(
            run_id="r1",
            run_mode=RunMode.MOCK,
            seed=42,
            replicates=2,
            layers_completed=[1],
            gold_verified=True,
            hardware_profile={},
            log_files={"layer1": "layer1.jsonl"},
            log_sha256={"layer1": "not_a_sha"},
            record_counts={"layer1": 1},
            created_at="2026-11-25T00:00:00Z",
        )


def test_telemetry_ram_probes_and_collector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peak = get_process_peak_ram_mb()
    assert peak >= 0.0

    # Force fallback when ctypes.windll is absent
    monkeypatch.setattr(ctypes, "windll", None, raising=False)
    assert _windows_peak_working_set_mb() is None
    assert get_process_peak_ram_mb() >= 0.0

    # Force fallback when psapi call returns 0
    class _FalsePsapi:
        @staticmethod
        def GetProcessMemoryInfo(*_args: object) -> int:
            return 0

    class _FakeKernel:
        @staticmethod
        def GetCurrentProcess() -> int:
            return 1

    fake_windll = types.SimpleNamespace(psapi=_FalsePsapi(), kernel32=_FakeKernel())
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    assert _windows_peak_working_set_mb() is None

    # Force fallback when psapi raises OSError
    class _BoomPsapi:
        @staticmethod
        def GetProcessMemoryInfo(*_args: object) -> int:
            raise OSError("boom")

    monkeypatch.setattr(
        ctypes,
        "windll",
        types.SimpleNamespace(psapi=_BoomPsapi(), kernel32=_FakeKernel()),
        raising=False,
    )
    assert _windows_peak_working_set_mb() is None

    # Cover tracemalloc already-tracing branch
    tracemalloc.start()
    try:
        assert _tracemalloc_peak_mb() >= 0.0
    finally:
        tracemalloc.stop()

    with pytest.raises(ValueError):
        compute_cloud_reduction_pct(-1, 10)
    with pytest.raises(ValueError):
        compute_local_only_pct(-1, 10)
    assert compute_local_only_pct(0, 0) == 0.0

    ledger = QuotaLedger(tmp_path / "quota.sqlite")
    profile = HardwareProfile(
        os_name="Windows",
        cpu_cores=8,
        ram_gb=16.0,
        gpu_name="Test GPU",
        vram_gb=8.0,
        detection_method="directml-dxgi",
        tier=HardwareTier.TIER_1,
        overridden=False,
    )
    collector = StageTelemetryCollector(
        run_mode=RunMode.LIVE,
        hardware_profile=profile,
        quota_ledger=ledger,
    )
    with pytest.raises(ValueError, match="blank"):
        collector.start_stage("   ")
    with pytest.raises(ValueError, match="blank"):
        collector.finish_stage("   ", total_steps=1, local_only_steps=1)

    collector.start_stage("stage_live")
    ledger.record_usage("gemini", prompt_tokens=100, completion_tokens=50)
    rec = collector.finish_stage(
        "stage_live",
        total_steps=10,
        local_only_steps=8,
        cloud_requests=0,
        all_cloud_baseline_requests=0,
        cloud_tokens=0,
        all_cloud_baseline_tokens=1500,
    )
    assert rec.cloud_requests == 1
    assert rec.cloud_tokens == 150
    assert rec.cloud_reduction_pct == 90.0
    assert rec.local_only_pct == 80.0
    assert rec.peak_vram_mb > 0.0

    # Explicit peak_vram_mb and unstarted stage
    rec2 = collector.finish_stage(
        "stage_unstarted",
        total_steps=4,
        local_only_steps=4,
        cloud_requests=0,
        all_cloud_baseline_requests=4,
        peak_vram_mb=512.0,
    )
    assert rec2.peak_vram_mb == 512.0
    assert rec2.cloud_reduction_pct == 100.0


# ---------------------------------------------------------------------------
# 3. Full offline dry-run (all 4 layers) & checkpoint resume idempotency
# ---------------------------------------------------------------------------


def test_full_mock_run_all_four_layers_and_sha256_manifest(tmp_path: Path) -> None:
    out_dir = tmp_path / "eval_mock_full"
    cfg = EvalRunConfig(
        out_dir=out_dir,
        layers=[1, 2, 3, 4],
        mode=RunMode.MOCK,
        replicates=2,
        seed=42,
    )
    orchestrator = EvalOrchestrator(
        cfg,
        quota_db_path=tmp_path / "quota.sqlite",
        hardware_profiler=_fast_profiler(),
    )
    manifest = orchestrator.run_all()

    assert manifest.run_mode == RunMode.MOCK
    assert manifest.layers_completed == [1, 2, 3, 4]
    assert manifest.gold_verified is True
    assert manifest.record_counts["layer1"] >= 3
    assert manifest.record_counts["layer2"] == 16
    assert manifest.record_counts["layer3"] == 36
    assert manifest.record_counts["layer4_telemetry"] == 3

    # Verify manifest file & all JSONL logs match SHA-256 and use LF line endings
    _assert_lf_only(out_dir / "eval_manifest.json")
    _assert_lf_only(out_dir / "checkpoint.json")
    for key, filename in manifest.log_files.items():
        log_path = out_dir / filename
        assert log_path.is_file()
        _assert_lf_only(log_path)
        assert sha256_file(log_path) == manifest.log_sha256[key]

    l3_records = _read_jsonl(out_dir / LAYER_LOG_FILENAMES["layer3"], Layer3Record)
    cond_a = [r for r in l3_records if r.condition == "raw_prd"]
    cond_c = [r for r in l3_records if r.condition == "reqhq_full_bundle"]
    assert len(cond_a) == 12
    assert len(cond_c) == 12
    assert all(r.lock_verified and r.alloy_formalized and r.alloy_non_vacuous for r in cond_c)
    assert all(r.hidden_pass_at_1 and r.pbt_pass_rate == 1.0 for r in cond_c)
    assert sum(r.mutation_score for r in cond_c) > sum(r.mutation_score for r in cond_a)


def test_checkpoint_resume_idempotency_mid_run_and_crash_recovery(tmp_path: Path) -> None:
    # Baseline full run
    baseline_dir = tmp_path / "baseline"
    base_cfg = EvalRunConfig(
        out_dir=baseline_dir,
        layers=[1, 2, 3, 4],
        mode=RunMode.MOCK,
        replicates=2,
        seed=42,
        max_tasks=2,
    )
    base_manifest = EvalOrchestrator(
        base_cfg,
        hardware_profiler=_fast_profiler(),
    ).run_all()

    # Interrupted run: execute L1+L2+L3, then simulate interruption by truncating half of L2 & L3
    # and rewinding checkpoint.json, PLUS leaving one extra line in L2 not yet recorded in checkpoint!
    resumed_dir = tmp_path / "resumed"
    res_cfg = base_cfg.model_copy(update={"out_dir": resumed_dir})
    orch = EvalOrchestrator(res_cfg, hardware_profiler=_fast_profiler())
    orch.run_layer1()
    l2_all = orch.run_layer2()
    l3_all = orch.run_layer3()

    # Keep first 6 lines of layer2.jsonl, but only mark first 5 in checkpoint (simulates crash
    # between JSONL append and atomic checkpoint write!)
    l2_path = resumed_dir / LAYER_LOG_FILENAMES["layer2"]
    l2_lines = l2_path.read_text(encoding="utf-8").splitlines()
    _atomic_write_lf(l2_path, "\n".join(l2_lines[:6]))

    # Keep first 5 lines of layer3.jsonl
    l3_path = resumed_dir / LAYER_LOG_FILENAMES["layer3"]
    l3_lines = l3_path.read_text(encoding="utf-8").splitlines()
    _atomic_write_lf(l3_path, "\n".join(l3_lines[:5]))

    ckpt_path = resumed_dir / "checkpoint.json"
    ckpt = EvalCheckpoint.model_validate_json(ckpt_path.read_text(encoding="utf-8"))
    kept_l1 = [u for u in ckpt.completed_units if u.startswith("L1:")]
    kept_l2 = [r.record_id for r in l2_all[:5]]
    kept_l3 = [r.record_id for r in l3_all[:5]]
    rewound = ckpt.model_copy(update={"completed_units": kept_l1 + kept_l2 + kept_l3})
    _atomic_write_lf(ckpt_path, json.dumps(rewound.model_dump(mode="json"), indent=2))

    # Resume and verify zero duplicates + identical record IDs to baseline!
    resumed_manifest = EvalOrchestrator.resume(
        resumed_dir,
        hardware_profiler=_fast_profiler(),
    )
    assert resumed_manifest.record_counts == base_manifest.record_counts
    assert resumed_manifest.layers_completed == [1, 2, 3, 4]

    for key, cls in (
        ("layer1", Layer1Record),
        ("layer2", Layer2Record),
        ("layer3", Layer3Record),
    ):
        base_recs = _read_jsonl(baseline_dir / LAYER_LOG_FILENAMES[key], cls)
        res_recs = _read_jsonl(resumed_dir / LAYER_LOG_FILENAMES[key], cls)
        res_ids = [r.record_id for r in res_recs]
        assert len(res_ids) == len(set(res_ids)), f"Duplicate records found in {key}"
        assert res_ids == [r.record_id for r in base_recs]

    # Resuming a second time after completion is a strict no-op with zero duplicates
    resumed_again = EvalOrchestrator.resume(resumed_dir, hardware_profiler=_fast_profiler())
    assert resumed_again.record_counts == base_manifest.record_counts

    with pytest.raises(FileNotFoundError):
        EvalOrchestrator.resume(tmp_path / "nonexistent_dir")


# ---------------------------------------------------------------------------
# 4. Live-mode fail-closed refusal & explicit live hook execution
# ---------------------------------------------------------------------------


def test_live_mode_refuses_silent_mock_fallback(tmp_path: Path) -> None:
    cfg = EvalRunConfig(
        out_dir=tmp_path / "live_fail",
        layers=[1, 2, 3, 4],
        mode=RunMode.LIVE,
        replicates=1,
        max_tasks=1,
    )
    orch = EvalOrchestrator(cfg, hardware_profiler=_fast_profiler())
    with pytest.raises(LiveExecutionUnavailableError, match="Layer 1"):
        orch.run_layer1()
    with pytest.raises(LiveExecutionUnavailableError, match="Layer 2"):
        orch.run_layer2()
    with pytest.raises(LiveExecutionUnavailableError, match="Layer 3"):
        orch.run_layer3()

    # Live router present but missing live_alloy_analyzer still refuses Layer 2
    router = ProviderRouter(
        local_provider=MockProvider(default_response="What is specified for G1?"),
        strict_local=True,
        max_local_retries=0,
        enable_cache=False,
    )
    orch_no_alloy = EvalOrchestrator(
        cfg,
        hardware_profiler=_fast_profiler(),
        live_router=router,
    )
    with pytest.raises(LiveExecutionUnavailableError, match="live_alloy_analyzer"):
        orch_no_alloy.run_layer2()


def test_live_mode_with_explicit_hooks_and_real_mutator(tmp_path: Path) -> None:
    router = ProviderRouter(
        local_provider=MockProvider(default_response="What is specified for G1?\nWhat about G2?"),
        strict_local=True,
        max_local_retries=0,
        enable_cache=False,
    )
    cfg = EvalRunConfig(
        out_dir=tmp_path / "live_ok",
        layers=[1, 2, 3, 4],
        mode=RunMode.LIVE,
        replicates=1,
        max_tasks=1,
    )
    orch = EvalOrchestrator(
        cfg,
        hardware_profiler=_fast_profiler(),
        live_router=router,
        live_coding_agent=MockCodingAgent(),
        live_alloy_analyzer=MockAlloyAnalyzer(default_status=AlloyRunStatus.VERIFIED),
    )
    manifest = orch.run_all()
    assert manifest.run_mode == RunMode.LIVE
    assert manifest.layers_completed == [1, 2, 3, 4]
    assert manifest.record_counts["layer3"] == 3


# ---------------------------------------------------------------------------
# 5. Edge-case coverage for 100% line coverage across src/reqhq/evaluation/
# ---------------------------------------------------------------------------


def test_edge_branches_error_events_and_helpers(tmp_path: Path) -> None:
    # Empty frozen root -> fail closed
    empty_frozen = tmp_path / "empty_frozen"
    (empty_frozen / "gold" / "dev").mkdir(parents=True)
    orch_empty = EvalOrchestrator(
        EvalRunConfig(out_dir=tmp_path / "out_empty", layers=[1], mode=RunMode.MOCK),
        frozen_root=empty_frozen,
        hardware_profiler=_fast_profiler(),
    )
    with pytest.raises(ValueError, match="zero human-verified FROZEN gold documents"):
        orch_empty.run_layer1()

    # Layer 4 standalone with no prior layer logs
    l4_only = EvalOrchestrator(
        EvalRunConfig(out_dir=tmp_path / "out_l4", layers=[4], mode=RunMode.MOCK),
        hardware_profiler=_fast_profiler(),
    )
    l4_manifest = l4_only.run_all()
    assert l4_manifest.record_counts == {"layer4_telemetry": 1}

    # Layer 2 non-verified Alloy statuses (COMPILE_ERROR, VACUOUS_UNSAT, COUNTEREXAMPLE_FOUND)
    scripted = [
        AlloyRunStatus.COMPILE_ERROR,
        AlloyRunStatus.COMPILE_ERROR,
        AlloyRunStatus.COMPILE_ERROR,
        AlloyRunStatus.VACUOUS_UNSAT,
        AlloyRunStatus.COUNTEREXAMPLE_FOUND,
    ]
    router = ProviderRouter(
        local_provider=MockProvider(default_response=""),
        strict_local=True,
        max_local_retries=0,
        enable_cache=False,
    )
    orch_l2_err = EvalOrchestrator(
        EvalRunConfig(out_dir=tmp_path / "out_l2_err", layers=[2], mode=RunMode.LIVE),
        hardware_profiler=_fast_profiler(),
        live_router=router,
        live_alloy_analyzer=MockAlloyAnalyzer(scripted_results=scripted),
    )
    l2_recs = orch_l2_err.run_layer2()
    all_events = {evt for r in l2_recs for evt in r.error_events}
    assert {"ALLOY_SYNTAX", "ALLOY_VACUOUS", "UNSAT_CONTRADICTION"}.issubset(all_events)

    # Layer 3 vacuous Alloy + use_real_mutator=True
    orch_l3_vac = EvalOrchestrator(
        EvalRunConfig(
            out_dir=tmp_path / "out_l3_vac",
            layers=[3],
            mode=RunMode.LIVE,
            replicates=1,
            max_tasks=1,
        ),
        hardware_profiler=_fast_profiler(),
        live_router=router,
        live_coding_agent=MockCodingAgent(),
        live_alloy_analyzer=MockAlloyAnalyzer(default_status=AlloyRunStatus.VACUOUS_UNSAT),
    )
    l3_vac_recs = orch_l3_vac.run_layer3()
    cond_c_vac = [r for r in l3_vac_recs if r.condition == "reqhq_full_bundle"][0]
    assert "ALLOY_VACUOUS" in cond_c_vac.error_events

    # _kind_to_gap_category all branches
    assert _kind_to_gap_category("security").value == "security"
    assert _kind_to_gap_category("process").value == "process"
    assert _kind_to_gap_category("training").value == "training"
    assert _kind_to_gap_category("performance").value == "nfr"
    assert _kind_to_gap_category("FR").value == "boundary"

    # _atomic_write_lf without trailing newline + _read_jsonl with blank line
    sample_jsonl = tmp_path / "sample.jsonl"
    _atomic_write_lf(sample_jsonl, "\n")
    assert _read_jsonl(sample_jsonl, Layer1Record) == []
    _atomic_write_lf(tmp_path / "no_nl.txt", "hello")
    assert (tmp_path / "no_nl.txt").read_text(encoding="utf-8") == "hello\n"

    # _InProcessASTMutationRunner with pre-existing sys.modules entry & max_mutants
    ws = tmp_path / "mut_ws"
    ws.mkdir()
    (ws / "temp_mod_eval.py").write_text("def f(x: int) -> bool:\n    return x <= 5\n", encoding="utf-8")
    (ws / "test_temp_mod_eval.py").write_text(
        "from temp_mod_eval import f\ndef test_f():\n    assert f(1) is True\n",
        encoding="utf-8",
    )
    sentinel = types.ModuleType("temp_mod_eval")
    sys.modules["temp_mod_eval"] = sentinel
    try:
        report = _InProcessASTMutationRunner().run(
            ws,
            ["temp_mod_eval.py"],
            ["test_temp_mod_eval.py"],
            max_mutants=1,
        )
        assert report.total_mutants == 1
        assert sys.modules["temp_mod_eval"] is sentinel
    finally:
        sys.modules.pop("temp_mod_eval", None)

    # Verify _build_and_write_manifest gracefully skips missing log file
    orch_skip = EvalOrchestrator(
        EvalRunConfig(out_dir=tmp_path / "out_skip", layers=[1], mode=RunMode.MOCK),
        hardware_profiler=_fast_profiler(),
    )
    manifest_missing_log = orch_skip._build_and_write_manifest([1])
    assert manifest_missing_log.log_files == {}


# ---------------------------------------------------------------------------
# 6. Mandatory deterministic Hypothesis PBTs
# ---------------------------------------------------------------------------


@settings(derandomize=True, max_examples=50)
@given(
    used=st.integers(min_value=0, max_value=1_000_000),
    extra_used=st.integers(min_value=0, max_value=1_000_000),
    baseline=st.integers(min_value=0, max_value=1_000_000),
)
def test_pbt_cloud_reduction_pct_bounds_and_monotonicity(
    used: int,
    extra_used: int,
    baseline: int,
) -> None:
    pct = compute_cloud_reduction_pct(used, baseline)
    assert 0.0 <= pct <= 100.0

    if baseline == 0:
        assert pct == 0.0
    elif used == 0:
        assert pct == 100.0
    elif used >= baseline:
        assert pct == 0.0

    higher_used_pct = compute_cloud_reduction_pct(used + extra_used, baseline)
    assert higher_used_pct <= pct


@settings(derandomize=True, max_examples=50)
@given(
    total_steps=st.integers(min_value=0, max_value=10_000),
    local_only_steps=st.integers(min_value=0, max_value=10_000),
    cloud_requests=st.integers(min_value=0, max_value=10_000),
    baseline_requests=st.integers(min_value=0, max_value=10_000),
    cloud_tokens=st.integers(min_value=0, max_value=1_000_000),
    baseline_tokens=st.integers(min_value=0, max_value=1_000_000),
    wall_clock=st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False, allow_infinity=False),
)
def test_pbt_telemetry_bounds_invariants(
    total_steps: int,
    local_only_steps: int,
    cloud_requests: int,
    baseline_requests: int,
    cloud_tokens: int,
    baseline_tokens: int,
    wall_clock: float,
) -> None:
    profile = HardwareProfile(
        os_name="Windows",
        cpu_cores=4,
        ram_gb=16.0,
        gpu_name="Integrated",
        vram_gb=4.0,
        detection_method="override",
        tier=HardwareTier.TIER_0,
        overridden=True,
    )
    collector = StageTelemetryCollector(run_mode=RunMode.MOCK, hardware_profile=profile)
    rec = collector.finish_stage(
        "pbt_stage",
        total_steps=total_steps,
        local_only_steps=local_only_steps,
        cloud_requests=cloud_requests,
        all_cloud_baseline_requests=baseline_requests,
        cloud_tokens=cloud_tokens,
        all_cloud_baseline_tokens=baseline_tokens,
        wall_clock_sec=wall_clock,
    )
    assert 0.0 <= rec.local_only_pct <= 100.0
    assert 0.0 <= rec.cloud_reduction_pct <= 100.0
    assert rec.peak_ram_mb >= 0.0
    assert rec.peak_vram_mb >= 0.0
    assert rec.wall_clock_sec >= 0.0


@settings(derandomize=True, max_examples=50, deadline=None)
@given(
    precompleted_indices=st.lists(
        st.integers(min_value=0, max_value=5),
        min_size=0,
        max_size=6,
        unique=True,
    ),
)
def test_pbt_checkpoint_unit_dedup_idempotency(precompleted_indices: list[int]) -> None:
    with tempfile.TemporaryDirectory(prefix="reqhq_ckpt_pbt_", ignore_cleanup_errors=True) as tmp:
        out_dir = Path(tmp) / "run"
        cfg = EvalRunConfig(
            out_dir=out_dir,
            layers=[3],
            mode=RunMode.MOCK,
            replicates=2,
            seed=42,
            max_tasks=1,
        )
        orch = EvalOrchestrator(cfg, hardware_profiler=_fast_profiler())
        all_records = orch.run_layer3()
        assert len(all_records) == 6

        # Simulate partial checkpoint containing an arbitrary subset of completed units
        selected = sorted(precompleted_indices)
        kept_records = [all_records[i] for i in selected]
        log_path = out_dir / LAYER_LOG_FILENAMES["layer3"]
        if kept_records:
            lines = [
                json.dumps(r.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
                for r in kept_records
            ]
            _atomic_write_lf(log_path, "\n".join(lines))
        else:
            log_path.unlink()

        ckpt_path = out_dir / "checkpoint.json"
        ckpt = EvalCheckpoint.model_validate_json(ckpt_path.read_text(encoding="utf-8"))
        rewound = ckpt.model_copy(
            update={"completed_units": [r.record_id for r in kept_records]}
        )
        _atomic_write_lf(ckpt_path, json.dumps(rewound.model_dump(mode="json"), indent=2))

        manifest = EvalOrchestrator.resume(out_dir, hardware_profiler=_fast_profiler())
        final_records = _read_jsonl(log_path, Layer3Record)
        final_ids = [r.record_id for r in final_records]

        assert manifest.record_counts["layer3"] == 6
        assert len(final_ids) == 6
        assert len(set(final_ids)) == 6
        assert set(final_ids) == {r.record_id for r in all_records}
