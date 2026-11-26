"""Offline unit and Hypothesis property tests for local Ollama auto-bootstrap & readiness."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import reqhq.llm as llm_pkg
from reqhq.llm.hardware_profiler import HardwareProfile, HardwareProfiler, HardwareTier
from reqhq.llm.model_registry import get_model_for_tier
from reqhq.llm.ollama_runtime import (
    OllamaReadinessReport,
    OllamaRuntimeError,
    OllamaRuntimeManager,
    ensure_ollama_ready,
    normalize_model_tag,
)


def _mock_client(handler) -> httpx.Client:
    """Create an offline httpx.Client backed exclusively by MockTransport."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def _tags_response(model_names: list[str]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"models": [{"name": name} for name in model_names]},
    )


# ---------------------------------------------------------------------------
# Public re-exports, schemas, error formatting, and tag normalization
# ---------------------------------------------------------------------------


def test_public_reexports_and_schema_strictness() -> None:
    for symbol in (
        "OllamaReadinessReport",
        "OllamaRuntimeError",
        "OllamaRuntimeManager",
        "ensure_ollama_ready",
        "normalize_model_tag",
    ):
        assert symbol in llm_pkg.__all__
        assert hasattr(llm_pkg, symbol)

    with pytest.raises(ValidationError):
        OllamaReadinessReport(
            base_url="http://127.0.0.1:11434",
            hardware_tier=0,
            daemon_running=True,
            required_models=["qwen3:4b"],
            installed_models=["qwen3:4b"],
            missing_models=[],
            active_model="qwen3:4b",
            ready=True,
            unexpected_extra="forbidden",  # type: ignore[call-arg]
        )

    err_plain = OllamaRuntimeError("daemon offline")
    assert err_plain.guidance == []
    assert err_plain.report is None
    assert str(err_plain) == "daemon offline"

    err_guided = OllamaRuntimeError("daemon offline", guidance=["ollama serve", "ollama pull qwen3:4b"])
    assert err_guided.guidance == ["ollama serve", "ollama pull qwen3:4b"]
    assert "Recovery steps:" in str(err_guided)
    assert "ollama serve" in str(err_guided)
    assert "ollama pull qwen3:4b" in str(err_guided)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("   ", ""),
        (":latest", ""),
        (" : ", ""),
        ("phi4-mini", "phi4-mini:latest"),
        ("phi4-mini:latest", "phi4-mini:latest"),
        ("  PHI4-MINI : LATEST  ", "phi4-mini:latest"),
        ("qwen3:4b", "qwen3:4b"),
        ("  Qwen3:4B ", "qwen3:4b"),
        ("qwen3:", "qwen3:latest"),
        ("localhost:5000/team/qwen3", "localhost:5000/team/qwen3:latest"),
        ("registry.ollama.ai/library/qwen3:4b", "registry.ollama.ai/library/qwen3:4b"),
    ],
)
def test_normalize_model_tag_cases(raw: str, expected: str) -> None:
    assert normalize_model_tag(raw) == expected


# ---------------------------------------------------------------------------
# Binary discovery
# ---------------------------------------------------------------------------


def test_find_ollama_binary_path_windows_fallback_and_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manager = OllamaRuntimeManager()

    # 1. Discovered via PATH
    monkeypatch.setattr(shutil, "which", lambda cmd: "C:/Program Files/Ollama/ollama.exe" if cmd == "ollama" else None)
    assert manager.find_ollama_binary() == Path("C:/Program Files/Ollama/ollama.exe")

    # 2. Missing from PATH -> Windows LOCALAPPDATA fallback present
    monkeypatch.setattr(shutil, "which", lambda _cmd: None)
    fallback_exe = tmp_path / "Programs" / "Ollama" / "ollama.exe"
    fallback_exe.parent.mkdir(parents=True)
    fallback_exe.write_text("stub", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert manager.find_ollama_binary() == fallback_exe

    # 3. LOCALAPPDATA set, but ollama.exe missing
    fallback_exe.unlink()
    assert manager.find_ollama_binary() is None

    # 4. LOCALAPPDATA unset
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert manager.find_ollama_binary() is None


# ---------------------------------------------------------------------------
# HTTP tag probing, error resilience, and default client creation path
# ---------------------------------------------------------------------------


def test_daemon_probes_and_malformed_http_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    # Valid tags with both `name` and `model` keys, plus ignored entries
    def _ok_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "qwen3:4b"},
                    {"model": "phi4-mini:latest"},
                    {"name": "   "},
                    "not-a-dict",
                ]
            },
        )

    mgr = OllamaRuntimeManager(client=_mock_client(_ok_handler))
    assert mgr.is_daemon_running() is True
    assert mgr.list_installed_models() == ["qwen3:4b", "phi4-mini:latest"]

    # Non-list `models` field still means daemon is alive with empty model list
    mgr_non_list = OllamaRuntimeManager(
        client=_mock_client(lambda _req: httpx.Response(200, json={"models": "invalid"}))
    )
    assert mgr_non_list.is_daemon_running() is True
    assert mgr_non_list.list_installed_models() == []

    # Connection error
    def _connect_err(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    mgr_conn = OllamaRuntimeManager(client=_mock_client(_connect_err))
    assert mgr_conn.is_daemon_running() is False
    assert mgr_conn.list_installed_models() == []

    # Non-200 status
    mgr_503 = OllamaRuntimeManager(client=_mock_client(lambda _req: httpx.Response(503)))
    assert mgr_503.is_daemon_running() is False

    # Invalid JSON
    mgr_bad_json = OllamaRuntimeManager(
        client=_mock_client(lambda _req: httpx.Response(200, text="not json"))
    )
    assert mgr_bad_json.is_daemon_running() is False

    # JSON array instead of dict
    mgr_list_root = OllamaRuntimeManager(
        client=_mock_client(lambda _req: httpx.Response(200, json=["qwen3:4b"]))
    )
    assert mgr_list_root.is_daemon_running() is False

    # Ephemeral httpx.Client path (client=None) verified completely offline via MockTransport
    real_client_cls = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *, timeout: real_client_cls(
            timeout=timeout,
            transport=httpx.MockTransport(lambda _req: _tags_response(["qwen3:4b"])),
        ),
    )
    mgr_default_client = OllamaRuntimeManager(base_url="http://127.0.0.1:11434/")
    assert mgr_default_client.base_url == "http://127.0.0.1:11434"
    assert mgr_default_client.list_installed_models() == ["qwen3:4b"]


# ---------------------------------------------------------------------------
# Daemon start, model pull, and warmup primitives
# ---------------------------------------------------------------------------


def test_start_daemon_windows_posix_errors_and_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_bin = tmp_path / "ollama.exe"
    fake_bin.write_text("stub", encoding="utf-8")

    # 1. Missing binary -> False immediately
    mgr_no_bin = OllamaRuntimeManager(client=_mock_client(lambda _req: _tags_response([])))
    monkeypatch.setattr(mgr_no_bin, "find_ollama_binary", lambda: None)
    assert mgr_no_bin.start_daemon() is False

    # 2. Windows detached launch -> succeeds on poll #2
    popen_calls: list[tuple[list[str], dict[str, object]]] = []

    def _fake_popen(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        popen_calls.append((cmd, kwargs))
        return SimpleNamespace(pid=4242)

    polls = {"count": 0}

    def _poll_handler(request: httpx.Request) -> httpx.Response:
        polls["count"] += 1
        if polls["count"] < 2:
            raise httpx.ConnectError("starting up", request=request)
        return _tags_response(["qwen3:4b"])

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    mgr_win = OllamaRuntimeManager(
        client=_mock_client(_poll_handler),
        startup_timeout_seconds=2.0,
        poll_interval_seconds=0.05,
    )
    assert mgr_win.start_daemon(binary_path=fake_bin) is True
    assert polls["count"] == 2
    assert popen_calls[0][0] == [str(fake_bin), "serve"]
    expected_flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
        subprocess, "CREATE_NO_WINDOW", 0x08000000
    )
    assert popen_calls[0][1]["creationflags"] == expected_flags

    # 3. POSIX detached launch -> start_new_session=True
    popen_calls.clear()
    monkeypatch.setattr(os, "name", "posix")
    mgr_posix = OllamaRuntimeManager(client=_mock_client(lambda _req: _tags_response([])))
    assert mgr_posix.start_daemon(binary_path=fake_bin) is True
    assert popen_calls[0][1]["start_new_session"] is True

    # 4. Popen OSError -> False
    def _boom_popen(_cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        raise OSError("cannot execute")

    monkeypatch.setattr(subprocess, "Popen", _boom_popen)
    assert mgr_posix.start_daemon(binary_path=fake_bin) is False

    # 5. Timeout expires while daemon stays unreachable -> False
    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    clock = iter([100.0, 100.1, 100.5, 101.2])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))

    def _always_down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("still down", request=request)

    mgr_timeout = OllamaRuntimeManager(
        client=_mock_client(_always_down),
        startup_timeout_seconds=1.0,
        poll_interval_seconds=0.25,
    )
    assert mgr_timeout.start_daemon(binary_path=fake_bin) is False


def test_pull_model_and_warmup_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_bin = tmp_path / "ollama"
    fake_bin.write_text("stub", encoding="utf-8")

    # pull_model: missing binary
    mgr = OllamaRuntimeManager(client=_mock_client(lambda _req: httpx.Response(200)))
    monkeypatch.setattr(mgr, "find_ollama_binary", lambda: None)
    assert mgr.pull_model("qwen3:4b") is False

    # pull_model: success & non-zero return code
    recorded_cmds: list[list[str]] = []

    def _run_ok(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        recorded_cmds.append(cmd)
        return SimpleNamespace(returncode=0, stdout="success", stderr="")

    monkeypatch.setattr(subprocess, "run", _run_ok)
    assert mgr.pull_model("qwen3:4b", binary_path=fake_bin) is True
    assert recorded_cmds[-1] == [str(fake_bin), "pull", "qwen3:4b"]

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda _cmd, **_kw: SimpleNamespace(returncode=1, stdout="", stderr="pull failed"),
    )
    assert mgr.pull_model("qwen3:4b", binary_path=fake_bin) is False

    # pull_model: timeout & OSError
    def _run_timeout(_cmd: list[str], **_kw: object) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd=["ollama", "pull", "qwen3:4b"], timeout=1.0)

    monkeypatch.setattr(subprocess, "run", _run_timeout)
    assert mgr.pull_model("qwen3:4b", binary_path=fake_bin) is False

    def _run_oserr(_cmd: list[str], **_kw: object) -> SimpleNamespace:
        raise OSError("binary broken")

    monkeypatch.setattr(subprocess, "run", _run_oserr)
    assert mgr.pull_model("qwen3:4b", binary_path=fake_bin) is False

    # warmup_model: success, HTTP error status, network exception
    seen_payloads: list[dict[str, object]] = []

    def _warmup_ok(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/generate"
        seen_payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"done": True})

    mgr_warm = OllamaRuntimeManager(client=_mock_client(_warmup_ok))
    assert mgr_warm.warmup_model("qwen3:4b", keep_alive="15m") is True
    assert seen_payloads == [
        {"model": "qwen3:4b", "prompt": "", "stream": False, "keep_alive": "15m"}
    ]

    mgr_warm_500 = OllamaRuntimeManager(client=_mock_client(lambda _req: httpx.Response(500)))
    assert mgr_warm_500.warmup_model("qwen3:4b") is False

    def _warmup_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    mgr_warm_exc = OllamaRuntimeManager(client=_mock_client(_warmup_timeout))
    assert mgr_warm_exc.warmup_model("qwen3:4b") is False


# ---------------------------------------------------------------------------
# End-to-end check_and_bootstrap and ensure_ollama_ready scenarios
# ---------------------------------------------------------------------------


def test_bootstrap_running_all_models_installed_and_single_model_warmup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_bin = tmp_path / "ollama.exe"
    fake_bin.write_text("stub", encoding="utf-8")
    warmup_calls: list[dict[str, object]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return _tags_response(["qwen3:4b", "phi4-mini:latest"])
        if request.url.path == "/api/generate":
            warmup_calls.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"done": True})
        raise AssertionError(f"Unexpected path: {request.url.path}")

    mgr = OllamaRuntimeManager(client=_mock_client(_handler))
    monkeypatch.setattr(mgr, "find_ollama_binary", lambda: fake_bin)

    # Also exercise tier=None -> HardwareProfiler.detect()
    fake_profile = HardwareProfile(
        os_name="Windows",
        cpu_cores=8,
        ram_gb=16.0,
        gpu_name="Test iGPU",
        vram_gb=2.0,
        detection_method="mock",
        tier=HardwareTier.TIER_0,
        overridden=False,
    )
    monkeypatch.setattr(HardwareProfiler, "detect", classmethod(lambda cls: fake_profile))

    report = mgr.check_and_bootstrap(tier=None, auto_start=True, auto_pull=False, warmup=True)
    assert report.ready is True
    assert report.hardware_tier == 0
    assert report.daemon_running is True
    assert report.daemon_auto_started is False
    assert report.required_models == ["qwen3:4b", "phi4-mini"]
    assert report.missing_models == []
    assert report.active_model == "qwen3:4b"
    assert report.warmed_up is True
    assert report.guidance == []
    # Memory safety: ONLY primary model warmed up, never fallback!
    assert len(warmup_calls) == 1
    assert warmup_calls[0]["model"] == "qwen3:4b"


def test_bootstrap_daemon_down_auto_starts_on_poll_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_bin = tmp_path / "ollama.exe"
    fake_bin.write_text("stub", encoding="utf-8")
    state = {"tag_calls": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        state["tag_calls"] += 1
        # Call 1 (initial check) + Call 2 (poll #1) fail; Call 3 (poll #2) & Call 4 (list) succeed
        if state["tag_calls"] <= 2:
            raise httpx.ConnectError("refused", request=request)
        return _tags_response(["qwen3:4b", "phi4-mini:latest"])

    monkeypatch.setattr(subprocess, "Popen", lambda _cmd, **_kw: SimpleNamespace(pid=99))
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    mgr = OllamaRuntimeManager(
        client=_mock_client(_handler),
        startup_timeout_seconds=5.0,
        poll_interval_seconds=0.01,
    )
    monkeypatch.setattr(mgr, "find_ollama_binary", lambda: fake_bin)

    report = mgr.check_and_bootstrap(tier=HardwareTier.TIER_0, auto_start=True)
    assert report.daemon_running is True
    assert report.daemon_auto_started is True
    assert report.ready is True
    assert report.missing_models == []
    assert report.guidance == []


def test_bootstrap_daemon_down_timeout_and_missing_binary_guidance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_bin = tmp_path / "ollama.exe"
    fake_bin.write_text("stub", encoding="utf-8")

    def _always_down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(subprocess, "Popen", lambda _cmd, **_kw: SimpleNamespace(pid=101))
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    # Case A: binary exists, daemon start times out
    mgr_timeout = OllamaRuntimeManager(
        client=_mock_client(_always_down),
        startup_timeout_seconds=0.0,
        poll_interval_seconds=0.01,
    )
    monkeypatch.setattr(mgr_timeout, "find_ollama_binary", lambda: fake_bin)

    report_timeout = ensure_ollama_ready(
        tier=HardwareTier.TIER_0,
        auto_start=True,
        raise_on_unready=False,
        manager=mgr_timeout,
    )
    assert report_timeout.ready is False
    assert report_timeout.daemon_running is False
    assert report_timeout.daemon_auto_started is False
    assert any("ollama serve" in line for line in report_timeout.guidance)
    assert any("ollama pull qwen3:4b" in line for line in report_timeout.guidance)

    with pytest.raises(OllamaRuntimeError) as exc_info:
        ensure_ollama_ready(
            tier=HardwareTier.TIER_0,
            auto_start=True,
            raise_on_unready=True,
            manager=mgr_timeout,
        )
    assert exc_info.value.guidance == report_timeout.guidance
    assert exc_info.value.report is not None
    assert "not reachable" in str(exc_info.value)

    # Case B: binary missing -> winget install guidance
    mgr_missing_bin = OllamaRuntimeManager(client=_mock_client(_always_down))
    monkeypatch.setattr(mgr_missing_bin, "find_ollama_binary", lambda: None)

    report_missing = mgr_missing_bin.check_and_bootstrap(tier=HardwareTier.TIER_0, auto_start=True)
    assert report_missing.ready is False
    assert report_missing.binary_path is None
    assert any("winget install Ollama.Ollama" in line for line in report_missing.guidance)
    assert any("ollama serve" in line for line in report_missing.guidance)
    assert any("ollama pull qwen3:4b" in line for line in report_missing.guidance)
    assert any("ollama pull phi4-mini" in line for line in report_missing.guidance)


def test_bootstrap_missing_models_auto_pull_and_require_fallback_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_bin = tmp_path / "ollama.exe"
    fake_bin.write_text("stub", encoding="utf-8")

    # 1. Missing models without auto_pull -> exact guidance & OllamaRuntimeError
    mgr_missing = OllamaRuntimeManager(
        client=_mock_client(lambda _req: _tags_response(["qwen3:4b"]))
    )
    monkeypatch.setattr(mgr_missing, "find_ollama_binary", lambda: fake_bin)

    report_no_pull = mgr_missing.check_and_bootstrap(tier=0, auto_pull=False)
    assert report_no_pull.ready is False
    assert report_no_pull.missing_models == ["phi4-mini"]
    assert report_no_pull.guidance == ["ollama pull phi4-mini"]

    with pytest.raises(OllamaRuntimeError) as exc_info:
        ensure_ollama_ready(tier=0, auto_pull=False, raise_on_unready=True, manager=mgr_missing)
    assert "Missing required Ollama model(s)" in str(exc_info.value)
    assert "ollama pull phi4-mini" in exc_info.value.guidance

    # 2. require_fallback_model=False -> primary model alone is ready!
    report_primary_only = mgr_missing.check_and_bootstrap(
        tier=0, auto_pull=False, require_fallback_model=False
    )
    assert report_primary_only.ready is True
    assert report_primary_only.required_models == ["qwen3:4b"]
    assert report_primary_only.missing_models == []
    assert report_primary_only.guidance == []

    # 3. auto_pull=True -> pull succeeds -> ready!
    installed_state = ["qwen3:4b"]
    mgr_pull_ok = OllamaRuntimeManager(
        client=_mock_client(lambda _req: _tags_response(installed_state))
    )
    monkeypatch.setattr(mgr_pull_ok, "find_ollama_binary", lambda: fake_bin)

    def _run_pull_ok(cmd: list[str], **_kw: object) -> SimpleNamespace:
        installed_state.append(f"{cmd[2]}:latest")
        return SimpleNamespace(returncode=0, stdout="pulled", stderr="")

    monkeypatch.setattr(subprocess, "run", _run_pull_ok)
    report_pulled = mgr_pull_ok.check_and_bootstrap(tier=0, auto_pull=True)
    assert report_pulled.ready is True
    assert report_pulled.pulled_models == ["phi4-mini"]
    assert report_pulled.missing_models == []
    assert report_pulled.guidance == []

    # 4. auto_pull=True -> pull fails -> still missing & guidance populated
    mgr_pull_fail = OllamaRuntimeManager(
        client=_mock_client(lambda _req: _tags_response(["qwen3:4b"]))
    )
    monkeypatch.setattr(mgr_pull_fail, "find_ollama_binary", lambda: fake_bin)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda _cmd, **_kw: SimpleNamespace(returncode=1, stdout="", stderr="network error"),
    )
    report_pull_fail = mgr_pull_fail.check_and_bootstrap(tier=0, auto_pull=True)
    assert report_pull_fail.ready is False
    assert report_pull_fail.pulled_models == []
    assert report_pull_fail.missing_models == ["phi4-mini"]
    assert report_pull_fail.guidance == ["ollama pull phi4-mini"]


def test_ensure_ollama_ready_default_manager_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = OllamaReadinessReport(
        base_url="http://127.0.0.1:11434",
        hardware_tier=1,
        binary_path="/usr/bin/ollama",
        daemon_running=True,
        required_models=["qwen3:8b", "qwen3:4b"],
        installed_models=["qwen3:8b", "qwen3:4b"],
        missing_models=[],
        active_model="qwen3:8b",
        ready=True,
    )
    monkeypatch.setattr(
        OllamaRuntimeManager,
        "check_and_bootstrap",
        lambda self, **_kwargs: expected,
    )
    got = ensure_ollama_ready(tier=1, raise_on_unready=True)
    assert got == expected


# ---------------------------------------------------------------------------
# Deterministic Hypothesis PBTs
# ---------------------------------------------------------------------------


_TAGLESS_NAME_STRATEGY = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"),
    min_size=1,
    max_size=32,
)

_ALL_TIER_MODELS = sorted(
    {
        model
        for tier in HardwareTier
        for spec in (get_model_for_tier(tier),)
        for model in ([spec.model_name] + ([spec.fallback_model] if spec.fallback_model else []))
    }
)


@given(raw=st.text(max_size=64))
@settings(derandomize=True, max_examples=50)
def test_pbt_normalize_model_tag_idempotency(raw: str) -> None:
    once = normalize_model_tag(raw)
    twice = normalize_model_tag(once)
    assert twice == once


@given(
    name=_TAGLESS_NAME_STRATEGY,
    pad_left=st.text(alphabet=" \t", max_size=3),
    pad_right=st.text(alphabet=" \t", max_size=3),
)
@settings(derandomize=True, max_examples=50)
def test_pbt_normalize_model_tag_latest_equivalence(
    name: str, pad_left: str, pad_right: str
) -> None:
    bare = normalize_model_tag(f"{pad_left}{name}{pad_right}")
    explicit = normalize_model_tag(f"{pad_left}{name}:latest{pad_right}")
    upper = normalize_model_tag(f"{name.upper()}:LATEST")
    assert bare == explicit == upper
    assert bare.endswith(":latest")


@given(
    tier=st.sampled_from(list(HardwareTier)),
    daemon_running=st.booleans(),
    installed_subset=st.lists(st.sampled_from(_ALL_TIER_MODELS), unique=True),
    use_explicit_latest=st.booleans(),
    require_fallback=st.booleans(),
)
@settings(derandomize=True, max_examples=50)
def test_pbt_check_and_bootstrap_readiness_and_guidance_invariants(
    tier: HardwareTier,
    daemon_running: bool,
    installed_subset: list[str],
    use_explicit_latest: bool,
    require_fallback: bool,
) -> None:
    rendered_installed = [
        f"{m}:latest" if (use_explicit_latest and ":" not in m) else m
        for m in installed_subset
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        if not daemon_running:
            raise httpx.ConnectError("offline", request=request)
        return _tags_response(rendered_installed)

    mgr = OllamaRuntimeManager(client=_mock_client(_handler))
    mgr.find_ollama_binary = lambda: None  # type: ignore[method-assign]

    report = mgr.check_and_bootstrap(
        tier=tier,
        auto_start=False,
        auto_pull=False,
        warmup=False,
        require_fallback_model=require_fallback,
    )

    assert report.ready == (report.daemon_running and len(report.missing_models) == 0)
    assert report.hardware_tier == int(tier)
    for missing in report.missing_models:
        assert any(missing in line for line in report.guidance)
    if report.ready:
        assert report.guidance == []
    else:
        assert len(report.guidance) > 0
