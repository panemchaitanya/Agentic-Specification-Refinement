"""Offline unit tests and Hypothesis PBTs for Phase 5 LiveLLMCodingAgent and live eval CLI wiring."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st
import pytest
from typer.testing import CliRunner

import reqhq.cli as cli_mod
from reqhq.alloy import AlloyRunStatus, MockAlloyAnalyzer
from reqhq.cli import app
from reqhq.evaluation import (
    EvalOrchestrator,
    EvalRunConfig,
    LiveExecutionUnavailableError,
    RunMode,
)
from reqhq.evaluation.tasks import EXPECTED_TASK_IDS, load_coding_tasks
from reqhq.harness import (
    CodingAgent,
    ExperimentCondition,
    LiveLLMCodingAgent,
    extract_code_payload,
    parse_pytest_pass_rate,
)
from reqhq.llm import MockProvider, ProviderError, ProviderRouter
from reqhq.llm.hardware_profiler import HardwareProfile, HardwareTier

runner = CliRunner()

_GOOD_REFUND_MODULE = """
def evaluate_refund_sla(
    amount_cents: int,
    elapsed_hours: int,
    is_chargeback: bool,
    idempotency_key: str,
) -> dict[str, object]:
    if amount_cents <= 0 or elapsed_hours < 0 or not idempotency_key.strip():
        raise ValueError("invalid refund request")
    sla_breached = elapsed_hours > 72
    if is_chargeback or amount_cents > 50000:
        decision = "dual_control_review"
    elif elapsed_hours <= 72:
        decision = "auto_approve"
    else:
        decision = "sla_escalate"
    return {
        "decision": decision,
        "sla_breached": sla_breached,
        "idempotency_key": idempotency_key.strip(),
    }
""".strip()

_BUGGY_REFUND_MODULE = """
def evaluate_refund_sla(
    amount_cents: int,
    elapsed_hours: int,
    is_chargeback: bool,
    idempotency_key: str,
) -> dict[str, object]:
    return {"decision": "auto_approve", "sla_breached": False, "idempotency_key": idempotency_key}
""".strip()

_GOOD_REFUND_TESTS = """
import pytest
from refund_sla_engine import evaluate_refund_sla

def test_basic_refund() -> None:
    res = evaluate_refund_sla(50000, 72, False, "k1")
    assert res["decision"] == "auto_approve"
    assert res["sla_breached"] is False

def test_invalid_refund() -> None:
    with pytest.raises(ValueError):
        evaluate_refund_sla(0, 1, False, "k1")
""".strip()


def _fast_profiler() -> Any:
    class _P:
        @staticmethod
        def detect(tier_override: int | None = None) -> HardwareProfile:
            return HardwareProfile(
                tier=HardwareTier(tier_override if tier_override is not None else 0),
                gpu_name="Test-GPU",
                vram_gb=2.0,
                ram_gb=16.0,
                cpu_cores=8,
                os_name="Windows",
                detection_method="test",
            )

    return _P()


def test_live_coding_agent_cond_a_b_c_isolation_repair_and_zero_hidden_leakage(
    tmp_path: Path,
) -> None:
    """Verify Cond A/B/C prompt isolation, syntax/test repair loop, Cond C read-only PBT, and hidden acceptance."""
    with pytest.raises(ValueError, match="max_repair_rounds"):
        LiveLLMCodingAgent(
            ProviderRouter(MockProvider(), strict_local=True, enable_cache=False),
            max_repair_rounds=-1,
        )

    # 1. Condition A happy path with empty test_code -> auto-stubbed test, passes real hidden acceptance!
    local_a = MockProvider(
        "ollama",
        is_local=True,
        responses=[json.dumps({"module_code": _GOOD_REFUND_MODULE, "test_code": ""})],
    )
    agent_a = LiveLLMCodingAgent(
        ProviderRouter(local_a, strict_local=True, enable_cache=False),
        max_repair_rounds=1,
    )
    assert isinstance(agent_a, CodingAgent)

    with pytest.raises(KeyError, match="Unknown coding task_id"):
        agent_a.build_condition_prompt(
            case_id="nonexistent_task",
            condition=ExperimentCondition.COND_A,
            bundle_dir=None,
        )
    with pytest.raises(KeyError, match="Unknown coding task_id"):
        agent_a.run_trial(
            case_id="nonexistent_task",
            condition=ExperimentCondition.COND_A,
            seed=42,
            workspace_dir=tmp_path / "missing",
            bundle_dir=None,
            timeout_seconds=15.0,
        )

    sys_a, prompt_a = agent_a.build_condition_prompt(
        case_id="refund_sla_engine",
        condition=ExperimentCondition.COND_A,
        bundle_dir=None,
    )
    assert "Condition A" in prompt_a
    assert "single_pass_nl_spec" not in prompt_a
    assert "test_auto_approve_exact_boundaries" not in prompt_a
    assert "JSON" in sys_a

    res_a = agent_a.run_trial(
        case_id="refund_sla_engine",
        condition=ExperimentCondition.COND_A,
        seed=42,
        workspace_dir=tmp_path / "ws_a",
        bundle_dir=None,
        timeout_seconds=20.0,
    )
    assert res_a.hidden_pass_at_1 is True
    assert res_a.pbt_pass_rate == 1.0
    assert res_a.stall_count == 0
    assert res_a.target_files == ["refund_sla_engine.py"]
    assert res_a.test_files == ["test_refund_sla_engine.py"]
    assert "+++ b/refund_sla_engine.py" in res_a.diff_patch

    # 2. Condition B syntax error -> repair round -> valid code passes hidden acceptance!
    local_b = MockProvider(
        "ollama",
        is_local=True,
        responses=[
            json.dumps({"module_code": "def evaluate_refund_sla(:", "test_code": _GOOD_REFUND_TESTS}),
            json.dumps({"module_code": _GOOD_REFUND_MODULE, "test_code": _GOOD_REFUND_TESTS}),
        ],
    )
    cloud_b = MockProvider(
        "gemini",
        is_local=False,
        responses=[
            json.dumps({"module_code": _GOOD_REFUND_MODULE, "test_code": _GOOD_REFUND_TESTS}),
        ],
    )
    agent_b = LiveLLMCodingAgent(
        ProviderRouter(local_b, [cloud_b], enable_cache=False),
        max_repair_rounds=2,
        escalate_repairs=True,
    )
    _sys_b, prompt_b = agent_b.build_condition_prompt(
        case_id="refund_sla_engine",
        condition=ExperimentCondition.COND_B,
        bundle_dir=None,
    )
    assert "Condition B" in prompt_b
    assert "test_auto_approve_exact_boundaries" not in prompt_b

    res_b = agent_b.run_trial(
        case_id="refund_sla_engine",
        condition=ExperimentCondition.COND_B,
        seed=42,
        workspace_dir=tmp_path / "ws_b",
        bundle_dir=None,
        timeout_seconds=20.0,
    )
    assert res_b.hidden_pass_at_1 is True
    assert res_b.stall_count == 1
    assert res_b.quota_requests_used == 1  # Only cloud_b counts; local ollama round 0 is local-only

    # 3. Condition C with read-only bundle_dir + failing round 0 -> repair round 1 passes locked PBT!
    bundle_dir = tmp_path / "bundle_c"
    (bundle_dir / "SPEC.md").parent.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "SPEC.md").write_text("# Alloy SPEC\n", encoding="utf-8")
    (bundle_dir / "PBT_Spec.yaml").write_text("properties:\n  - id: PBT-001\n", encoding="utf-8")
    props_dir = bundle_dir / "tests" / "properties"
    props_dir.mkdir(parents=True, exist_ok=True)
    locked_pbt = props_dir / "test_contract_pbt.py"
    locked_pbt.write_text(
        "import pytest\n"
        "from refund_sla_engine import evaluate_refund_sla\n\n"
        "def test_contract_rejects_zero() -> None:\n"
        "    with pytest.raises(ValueError):\n"
        "        evaluate_refund_sla(0, 1, False, 'k1')\n",
        encoding="utf-8",
    )
    pbt_bytes_before = locked_pbt.read_bytes()

    local_c = MockProvider(
        "ollama",
        is_local=True,
        responses=[
            json.dumps({"module_code": _BUGGY_REFUND_MODULE, "test_code": _GOOD_REFUND_TESTS}),
            json.dumps({"module_code": _GOOD_REFUND_MODULE, "test_code": _GOOD_REFUND_TESTS}),
        ],
    )
    agent_c = LiveLLMCodingAgent(
        ProviderRouter(local_c, strict_local=True, enable_cache=False),
        max_repair_rounds=1,
    )
    res_c = agent_c.run_trial(
        case_id="refund_sla_engine",
        condition=ExperimentCondition.COND_C,
        seed=42,
        workspace_dir=tmp_path / "ws_c",
        bundle_dir=bundle_dir,
        timeout_seconds=20.0,
    )
    assert res_c.hidden_pass_at_1 is True
    assert res_c.pbt_pass_rate == 1.0
    assert res_c.stall_count == 1
    assert locked_pbt.read_bytes() == pbt_bytes_before


def test_live_coding_agent_error_fallback_timeout_and_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover extract_code_payload fallbacks, ProviderError stall, empty syntax, and pytest TimeoutExpired."""
    # extract_code_payload branches: invalid parsed dict -> 2 fences, 1 fence, plain text
    m2, t2 = extract_code_payload(
        "```python\nx = 1\n```\n```python\ndef test_x(): assert True\n```",
        {"bad_field": "oops"},
    )
    assert m2 == "x = 1" and "test_x" in t2
    m1, t1 = extract_code_payload("```py\ny = 2\n```", None)
    assert m1 == "y = 2" and t1 == ""
    m0, t0 = extract_code_payload("z = 3", None)
    assert m0 == "z = 3" and t0 == ""

    # parse_pytest_pass_rate branches: mixed counts, no counts w/ rc=0 and rc=1
    assert parse_pytest_pass_rate("3 passed, 1 failed in 0.12s", 1) == 0.75
    assert parse_pytest_pass_rate("1 error in 0.05s", 2) == 0.0
    assert parse_pytest_pass_rate("no summary", 0) == 1.0
    assert parse_pytest_pass_rate("no summary", 1) == 0.0

    # ProviderError on round 0 -> degrades cleanly with hidden_pass_at_1=False and stall_count=1
    err_local = MockProvider("ollama", is_local=True, responses=[ProviderError("down", provider="ollama")])
    err_agent = LiveLLMCodingAgent(
        ProviderRouter(err_local, strict_local=True, enable_cache=False),
        max_repair_rounds=0,
    )
    err_res = err_agent.run_trial(
        case_id="refund_sla_engine",
        condition=ExperimentCondition.COND_A,
        seed=1,
        workspace_dir=tmp_path / "ws_err",
        bundle_dir=None,
        timeout_seconds=10.0,
    )
    assert err_res.hidden_pass_at_1 is False
    assert err_res.stall_count == 1
    assert err_res.target_files == []

    # Empty module syntax failure on final round (0 repair rounds) -> clean break without writing file
    empty_local = MockProvider("ollama", is_local=True, responses=["   "])
    empty_agent = LiveLLMCodingAgent(
        ProviderRouter(empty_local, strict_local=True, enable_cache=False, max_local_retries=0),
        max_repair_rounds=0,
    )
    empty_res = empty_agent.run_trial(
        case_id="refund_sla_engine",
        condition=ExperimentCondition.COND_A,
        seed=2,
        workspace_dir=tmp_path / "ws_empty",
        bundle_dir=None,
        timeout_seconds=10.0,
    )
    assert empty_res.hidden_pass_at_1 is False
    assert empty_res.stall_count == 1

    # Subprocess TimeoutExpired inside _run_pytest handled cleanly
    def _boom_run(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=5.0)

    monkeypatch.setattr(subprocess, "run", _boom_run)
    rc, out = err_agent._run_pytest(
        [tmp_path / "t.py"],
        cwd=tmp_path,
        pythonpath_entries=[tmp_path],
        timeout_seconds=5.0,
    )
    assert rc == 124
    assert "timed out" in out


def test_eval_split_dev_vs_all_layer1_provider_error_and_cli_live_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify EvalRunConfig.split ('dev' vs 'all'), Layer 1 ProviderError resilience, and CLI live hook wiring."""
    # 1. Synthetic frozen gold dir with both dev and test splits (never touches real data/frozen/gold/test!)
    real_dev_gold = Path("data/frozen/gold/dev/pure-0000_-_cctns.gold.json")
    gold_payload = json.loads(real_dev_gold.read_text(encoding="utf-8"))

    syn_frozen = tmp_path / "frozen"
    (syn_frozen / "gold" / "dev").mkdir(parents=True)
    (syn_frozen / "gold" / "test").mkdir(parents=True)
    (syn_frozen / "gold" / "dev" / "dev_doc.gold.json").write_text(
        json.dumps(gold_payload), encoding="utf-8"
    )
    test_payload = dict(gold_payload, item_id="synthetic-test-doc", split="test")
    (syn_frozen / "gold" / "test" / "test_doc.gold.json").write_text(
        json.dumps(test_payload), encoding="utf-8"
    )

    # Default split="dev" loads ONLY dev split
    orch_dev = EvalOrchestrator(
        EvalRunConfig(out_dir=tmp_path / "ev_dev", layers=[1], mode=RunMode.MOCK),
        frozen_root=syn_frozen,
        hardware_profiler=_fast_profiler(),
    )
    dev_recs = orch_dev.run_layer1()
    assert [r.split for r in dev_recs] == ["dev"]

    # Explicit split="all" loads both dev + test splits
    orch_all = EvalOrchestrator(
        EvalRunConfig(out_dir=tmp_path / "ev_all", layers=[1], mode=RunMode.MOCK, split="all"),
        frozen_root=syn_frozen,
        hardware_profiler=_fast_profiler(),
    )
    all_recs = orch_all.run_layer1()
    assert sorted(r.split for r in all_recs) == ["dev", "test"]

    # Layer 1 live ProviderError recorded cleanly in error_events without crashing
    failing_router = ProviderRouter(
        MockProvider("ollama", is_local=True, responses=[ProviderError("503", provider="ollama")]),
        strict_local=True,
        enable_cache=False,
    )
    orch_l1_err = EvalOrchestrator(
        EvalRunConfig(out_dir=tmp_path / "ev_l1_err", layers=[1], mode=RunMode.LIVE),
        frozen_root=syn_frozen,
        hardware_profiler=_fast_profiler(),
        live_router=failing_router,
    )
    l1_err_recs = orch_l1_err.run_layer1()
    assert len(l1_err_recs) == 1
    assert any("QUOTA_FALLBACK" in e for e in l1_err_recs[0].error_events)

    # 2. _build_live_eval_hooks fails closed when Alloy analyzer is unavailable
    monkeypatch.setattr(cli_mod, "_run_live_startup_preflight", lambda **kwargs: None)

    class _UnavailableAlloy:
        def is_available(self) -> bool:
            return False

    monkeypatch.setattr(cli_mod, "_build_alloy_analyzer", lambda **kwargs: _UnavailableAlloy())
    with pytest.raises(LiveExecutionUnavailableError, match="Alloy 6.2 JAR"):
        cli_mod._build_live_eval_hooks(sessions_dir=tmp_path / "s", strict_local=True)

    # 3. _build_live_eval_hooks succeeds when Alloy is available & wires CLI `eval run --mode live` + `eval resume`
    class _AvailableAlloy(MockAlloyAnalyzer):
        def is_available(self) -> bool:
            return True

    monkeypatch.setattr(cli_mod, "_build_alloy_analyzer", lambda **kwargs: _AvailableAlloy())
    fake_router = ProviderRouter(
        MockProvider("ollama", is_local=True, default_response="What SLA applies?"),
        strict_local=True,
        enable_cache=False,
    )
    monkeypatch.setattr(cli_mod, "_build_router", lambda **kwargs: fake_router)
    hooks = cli_mod._build_live_eval_hooks(sessions_dir=tmp_path / "s", strict_local=True)
    assert hooks["live_router"] is fake_router
    assert isinstance(hooks["live_coding_agent"], LiveLLMCodingAgent)
    assert hooks["use_real_mutator"] is True

    live_out = tmp_path / "cli_live_ev"
    res_run = runner.invoke(
        app,
        [
            "eval",
            "run",
            "--mode",
            "live",
            "--layers",
            "1,4",
            "--split",
            "dev",
            "--strict-local",
            "--out",
            str(live_out),
            "--sessions-dir",
            str(tmp_path / "s"),
        ],
    )
    assert res_run.exit_code == 0, res_run.output
    assert "mode=live" in res_run.output

    # Resume the completed live run via CLI -> invokes _build_live_eval_hooks cleanly with zero dupes!
    res_resume = runner.invoke(
        app,
        ["eval", "resume", "--out", str(live_out), "--sessions-dir", str(tmp_path / "s")],
    )
    assert res_resume.exit_code == 0, res_resume.output
    assert "Evaluation resumed:" in res_resume.output


def test_live_coding_agent_relative_paths_and_cloud_only_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (DEF-P5-003/004): CWD-relative workspace/bundle paths resolve cleanly & local calls count 0 cloud."""
    real_tasks_root = Path("data/benchmarks/coding_tasks").resolve()
    monkeypatch.chdir(tmp_path)

    rel_bundle = Path("rel_eval/bundles/refund_sla_engine")
    props_dir = rel_bundle / "tests" / "properties"
    props_dir.mkdir(parents=True, exist_ok=True)
    (rel_bundle / "SPEC.md").write_text("# Alloy SPEC\n", encoding="utf-8")
    (props_dir / "test_locked_pbt.py").write_text(
        "import pytest\n"
        "from refund_sla_engine import evaluate_refund_sla\n\n"
        "def test_locked_rejects_negative() -> None:\n"
        "    with pytest.raises(ValueError):\n"
        "        evaluate_refund_sla(-1, 1, False, 'k1')\n",
        encoding="utf-8",
    )

    local_only = MockProvider(
        "ollama",
        is_local=True,
        responses=[json.dumps({"module_code": _GOOD_REFUND_MODULE, "test_code": _GOOD_REFUND_TESTS})],
    )
    agent = LiveLLMCodingAgent(
        ProviderRouter(local_only, strict_local=True, enable_cache=False),
        tasks_root=real_tasks_root,
        max_repair_rounds=1,
    )
    res = agent.run_trial(
        case_id="refund_sla_engine",
        condition=ExperimentCondition.COND_C,
        seed=42,
        workspace_dir=Path("rel_eval/trials/r1/workspace"),
        bundle_dir=rel_bundle,
        timeout_seconds=20.0,
    )
    assert res.hidden_pass_at_1 is True
    assert res.pbt_pass_rate == 1.0
    assert res.stall_count == 0
    assert res.quota_requests_used == 0
    assert res.quota_tokens_used == 0
    assert "not found" not in res.test_output.lower()
    assert "not found" not in res.pbt_output.lower()


# ---------------------------------------------------------------------------
# Deterministic Hypothesis PBTs (ADR-0014 & ADR-0021)
# ---------------------------------------------------------------------------


@settings(derandomize=True, max_examples=50)
@given(
    passed=st.integers(min_value=0, max_value=200),
    failed=st.integers(min_value=0, max_value=200),
    errors=st.integers(min_value=0, max_value=50),
    rc=st.integers(min_value=0, max_value=2),
)
def test_pbt_parse_pytest_pass_rate_always_bounded(
    passed: int,
    failed: int,
    errors: int,
    rc: int,
) -> None:
    """PBT: parse_pytest_pass_rate always returns a finite ratio in [0.0, 1.0]."""
    summary = f"{passed} passed, {failed} failed, {errors} errors in 0.42s"
    rate = parse_pytest_pass_rate(summary, rc)
    assert 0.0 <= rate <= 1.0
    total = passed + failed + errors
    if total > 0:
        assert rate == round(passed / total, 4)


@settings(derandomize=True, max_examples=50)
@given(
    mod_code=st.text(min_size=1, max_size=120).filter(lambda s: "```" not in s and s.strip() != ""),
    test_code=st.text(min_size=1, max_size=120).filter(lambda s: "```" not in s and s.strip() != ""),
)
def test_pbt_extract_code_payload_roundtrip(mod_code: str, test_code: str) -> None:
    """PBT: extract_code_payload recovers stripped module_code and test_code from schema or fences."""
    m_p, t_p = extract_code_payload("", {"module_code": mod_code, "test_code": test_code})
    assert m_p == mod_code.strip()
    assert t_p == test_code.strip()

    fenced = f"```python\n{mod_code}\n```\n\n```python\n{test_code}\n```"
    m_f, t_f = extract_code_payload(fenced, None)
    assert m_f == mod_code.strip()
    assert t_f == test_code.strip()


@settings(derandomize=True, max_examples=50, deadline=None)
@given(
    task_id=st.sampled_from(EXPECTED_TASK_IDS),
    condition=st.sampled_from(list(ExperimentCondition)),
)
def test_pbt_condition_prompts_never_leak_hidden_tests(
    task_id: str,
    condition: ExperimentCondition,
) -> None:
    """PBT: Across all 6 coding tasks x 3 conditions, prompts NEVER contain hidden acceptance test code."""
    tasks = {t.task_id: t for t in load_coding_tasks()}
    hidden_src = (tasks[task_id].task_dir / "hidden_tests" / "test_acceptance.py").read_text(  # type: ignore[operator]
        encoding="utf-8"
    )
    hidden_test_names = [
        line.split("def ", 1)[1].split("(", 1)[0].strip()
        for line in hidden_src.splitlines()
        if line.strip().startswith("def test_")
    ]
    assert hidden_test_names

    agent = LiveLLMCodingAgent(
        ProviderRouter(MockProvider(), strict_local=True, enable_cache=False)
    )
    sys_p, user_p = agent.build_condition_prompt(
        case_id=task_id,
        condition=condition,
        bundle_dir=None,
    )
    combined = sys_p + "\n" + user_p
    for fn_name in hidden_test_names:
        assert fn_name not in combined, f"Hidden test {fn_name!r} leaked into {condition.value} prompt!"


@settings(derandomize=True, max_examples=50, deadline=None)
@given(
    segments=st.lists(
        st.from_regex(r"[a-z][a-z0-9_]{1,8}", fullmatch=True),
        min_size=1,
        max_size=3,
    ),
)
def test_pbt_run_pytest_always_resolves_relative_paths(segments: list[str]) -> None:
    """PBT (DEF-P5-003): _run_pytest always resolves cwd, targets, and PYTHONPATH to absolute paths."""
    agent = LiveLLMCodingAgent(
        ProviderRouter(MockProvider(), strict_local=True, enable_cache=False)
    )
    rel_dir = Path(*segments)
    rel_target = rel_dir / "test_mod.py"
    captured: dict[str, Any] = {}

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["cwd"] = kwargs["cwd"]
        captured["pythonpath"] = kwargs["env"]["PYTHONPATH"]
        return subprocess.CompletedProcess(cmd, 0, stdout="1 passed in 0.01s\n", stderr="")

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(subprocess, "run", _fake_run)
        rc, out = agent._run_pytest(
            [rel_target],
            cwd=rel_dir,
            pythonpath_entries=[rel_dir],
            timeout_seconds=10.0,
        )
    finally:
        mp.undo()

    assert rc == 0
    assert "1 passed" in out
    assert Path(captured["cwd"]).is_absolute()
    assert Path(captured["cmd"][-1]).is_absolute()
    assert Path(captured["pythonpath"].split(os.pathsep)[0]).is_absolute()
