"""Offline unit and property tests for HardwareProfiler and model_registry."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from reqhq.llm.hardware_profiler import (
    HardwareProfile,
    HardwareProfiler,
    HardwareTier,
    _default_command_runner,
    _parse_memory_string_gb,
    _windows_global_memory_gb,
    classify_vram_tier,
)
from reqhq.llm.model_registry import (
    MODEL_TIERS,
    ModelSpec,
    get_model_for_tier,
    list_model_tiers,
)


def _make_runner(responses: dict[str, tuple[int, str, str]]):
    """Build an injectable command runner keyed by substring match on the joined command."""

    def _runner(cmd: list[str]) -> tuple[int, str, str]:
        joined = " ".join(cmd)
        for pattern, reply in responses.items():
            if pattern in joined:
                return reply
        return (1, "", "command not found")

    return _runner


# ---------------------------------------------------------------------------
# Tier classification & model registry tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("vram_gb", "expected"),
    [
        (0.0, HardwareTier.TIER_0),
        (2.0, HardwareTier.TIER_0),
        (4.0, HardwareTier.TIER_0),
        (4.5, HardwareTier.TIER_1),
        (6.0, HardwareTier.TIER_1),
        (8.0, HardwareTier.TIER_1),
        (11.9, HardwareTier.TIER_1),
        (12.0, HardwareTier.TIER_2),
        (16.0, HardwareTier.TIER_2),
        (23.5, HardwareTier.TIER_2),
        (24.0, HardwareTier.TIER_3),
        (48.0, HardwareTier.TIER_3),
    ],
)
def test_classify_vram_tier_boundaries(vram_gb: float, expected: HardwareTier) -> None:
    assert classify_vram_tier(vram_gb) == expected


@given(
    v1=st.floats(min_value=0.0, max_value=128.0, allow_nan=False, allow_infinity=False),
    v2=st.floats(min_value=0.0, max_value=128.0, allow_nan=False, allow_infinity=False),
)
def test_hypothesis_vram_tier_monotonicity(v1: float, v2: float) -> None:
    low, high = min(v1, v2), max(v1, v2)
    assert classify_vram_tier(low) <= classify_vram_tier(high)


def test_model_registry_tiers_and_lookup() -> None:
    assert set(MODEL_TIERS.keys()) == {
        HardwareTier.TIER_0,
        HardwareTier.TIER_1,
        HardwareTier.TIER_2,
        HardwareTier.TIER_3,
    }
    t0 = get_model_for_tier(HardwareTier.TIER_0)
    assert t0.model_name == "qwen3:4b"
    assert t0.quantization == "Q4_K_M"
    assert t0.fallback_model == "phi4-mini"

    assert get_model_for_tier(1).model_name == "qwen3:8b"
    assert get_model_for_tier(2).model_name == "qwen3:14b"
    assert get_model_for_tier(3).model_name == "gpt-oss:20b"
    assert [spec.tier for spec in list_model_tiers()] == [
        HardwareTier.TIER_0,
        HardwareTier.TIER_1,
        HardwareTier.TIER_2,
        HardwareTier.TIER_3,
    ]

    with pytest.raises(ValueError, match="Unsupported hardware tier"):
        get_model_for_tier(9)
    with pytest.raises(ValueError, match="Unsupported hardware tier"):
        get_model_for_tier(True)

    with pytest.raises(ValidationError):
        ModelSpec(
            tier=HardwareTier.TIER_0,
            model_name="qwen3:4b",
            min_vram_gb=0.0,
            context_window=8192,
            recommended_ram_gb=8.0,
            extra_field="nope",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Deterministic override & dual class/instance detect tests
# ---------------------------------------------------------------------------


def test_hardware_profiler_override_class_and_instance() -> None:
    # Called directly on the class per shared interface contract
    profile = HardwareProfiler.detect(tier_override=2)
    assert isinstance(profile, HardwareProfile)
    assert profile.tier == HardwareTier.TIER_2
    assert profile.overridden is True
    assert profile.detection_method == "override"
    assert profile.cpu_cores >= 1

    # Called on an instance with deterministic mock host
    profiler = HardwareProfiler(
        command_runner=_make_runner({}),
        os_name="Windows",
        cpu_cores=8,
        ram_gb_provider=lambda: 16.0,
    )
    for tier_val in (0, 1, 2, 3):
        p = profiler.detect(tier_override=tier_val)
        assert p.tier == HardwareTier(tier_val)
        assert p.overridden is True
        assert p.ram_gb == pytest.approx(16.0)
        assert p.cpu_cores == 8

    with pytest.raises(ValueError, match="Invalid hardware tier override"):
        profiler.detect(tier_override=5)
    with pytest.raises(ValueError, match="Invalid hardware tier override"):
        profiler.detect(tier_override=True)

    with pytest.raises(ValidationError):
        HardwareProfile(
            os_name="Windows",
            cpu_cores=4,
            ram_gb=16.0,
            gpu_name="Test",
            vram_gb=8.0,
            detection_method="override",
            tier=HardwareTier.TIER_1,
            unknown_key=1,  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Windows 11 detection chain: nvidia-smi -> DirectML/DXGI -> PowerShell CIM
# ---------------------------------------------------------------------------


def test_windows_nvidia_smi_multi_gpu_and_ram_cim() -> None:
    runner = _make_runner(
        {
            "TotalPhysicalMemory": (0, str(16 * 1024**3) + "\n", ""),
            "nvidia-smi": (
                0,
                "malformed_line\n"
                "NVIDIA GeForce GTX 1650, 4096\n"
                "NVIDIA GeForce RTX 4060 Laptop GPU, 8192\n"
                "Broken Card, not_a_number\n",
                "",
            ),
        }
    )
    profile = HardwareProfiler(command_runner=runner, os_name="Windows", cpu_cores=12).detect()
    assert profile.os_name == "Windows"
    assert profile.cpu_cores == 12
    assert profile.ram_gb == pytest.approx(16.0)
    assert profile.gpu_name == "NVIDIA GeForce RTX 4060 Laptop GPU"
    assert profile.vram_gb == pytest.approx(8.0)
    assert profile.detection_method == "nvidia-smi"
    assert profile.tier == HardwareTier.TIER_1
    assert profile.overridden is False


def test_windows_directml_dxgi_callback_and_registry() -> None:
    # 1. Injectable DirectML callback probe
    profiler_cb = HardwareProfiler(
        command_runner=_make_runner({"TotalPhysicalMemory": (0, str(32 * 1024**3), "")}),
        os_name="Windows",
        cpu_cores=16,
        directml_probe=lambda: ("AMD Radeon RX 7900 XTX", 24.0),
    )
    p_cb = profiler_cb.detect()
    assert p_cb.detection_method == "directml-dxgi"
    assert p_cb.gpu_name == "AMD Radeon RX 7900 XTX"
    assert p_cb.vram_gb == pytest.approx(24.0)
    assert p_cb.tier == HardwareTier.TIER_3

    # 2. PowerShell DXGI DisplayClass registry probe (>4GB qwMemorySize)
    dxgi_json = json.dumps(
        [
            {"DriverDesc": "Intel Iris Xe", "HardwareInformation.qwMemorySize": 1 * 1024**3},
            {"DriverDesc": "AMD Radeon RX 7800 XT", "HardwareInformation.qwMemorySize": 16 * 1024**3},
        ]
    )
    profiler_reg = HardwareProfiler(
        command_runner=_make_runner({"qwMemorySize": (0, dxgi_json, "")}),
        os_name="Windows",
        cpu_cores=8,
        ram_gb_provider=lambda: 16.0,
    )
    p_reg = profiler_reg.detect()
    assert p_reg.detection_method == "directml-dxgi"
    assert p_reg.gpu_name == "AMD Radeon RX 7800 XT"
    assert p_reg.vram_gb == pytest.approx(16.0)
    assert p_reg.tier == HardwareTier.TIER_2

    # 3. PowerShell DXGI DisplayClass registry probe with REG_BINARY byte-array (8 GiB LE)
    eight_gib_le_bytes = list((8 * 1024**3).to_bytes(8, byteorder="little", signed=False))
    dxgi_bin_json = json.dumps(
        {"DriverDesc": "Intel Arc A750", "HardwareInformation.qwMemorySize": eight_gib_le_bytes}
    )
    p_bin = HardwareProfiler(
        command_runner=_make_runner({"qwMemorySize": (0, dxgi_bin_json, "")}),
        os_name="Windows",
        cpu_cores=8,
        ram_gb_provider=lambda: 16.0,
    ).detect()
    assert p_bin.detection_method == "directml-dxgi"
    assert p_bin.gpu_name == "Intel Arc A750"
    assert p_bin.vram_gb == pytest.approx(8.0)
    assert p_bin.tier == HardwareTier.TIER_1


def test_windows_powershell_cim_fallback() -> None:
    cim_json = json.dumps({"Name": "Intel(R) UHD Graphics", "AdapterRAM": 2 * 1024**3})
    runner = _make_runner({"Win32_VideoController": (0, cim_json, "")})
    profile = HardwareProfiler(
        command_runner=runner,
        os_name="Windows",
        cpu_cores=8,
        ram_gb_provider=lambda: 16.0,
    ).detect()
    assert profile.detection_method == "powershell-cim"
    assert profile.gpu_name == "Intel(R) UHD Graphics"
    assert profile.vram_gb == pytest.approx(2.0)
    assert profile.tier == HardwareTier.TIER_0

    # Multiple adapters with one null AdapterRAM
    cim_list = json.dumps(
        [
            {"Name": "Basic Display", "AdapterRAM": None},
            {"Name": "NVIDIA RTX 3050", "AdapterRAM": 4 * 1024**3},
        ]
    )
    profile2 = HardwareProfiler(
        command_runner=_make_runner({"Win32_VideoController": (0, cim_list, "")}),
        os_name="Windows",
        cpu_cores=8,
        ram_gb_provider=lambda: 16.0,
    ).detect()
    assert profile2.gpu_name == "NVIDIA RTX 3050"
    assert profile2.vram_gb == pytest.approx(4.0)
    assert profile2.tier == HardwareTier.TIER_0


# ---------------------------------------------------------------------------
# Linux, macOS, and CPU-only fallback paths
# ---------------------------------------------------------------------------


def test_linux_rocm_lspci_and_proc_meminfo(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       33554432 kB\nMemFree:        1000000 kB\n", encoding="utf-8")

    rocm_json = json.dumps({"card0": {"VRAM Total Memory (B)": str(16 * 1024**3)}})
    p_rocm = HardwareProfiler(
        command_runner=_make_runner({"rocm-smi": (0, rocm_json, "")}),
        os_name="Linux",
        cpu_cores=16,
        proc_meminfo_path=meminfo,
    ).detect()
    assert p_rocm.ram_gb == pytest.approx(32.0)
    assert p_rocm.detection_method == "rocm-smi"
    assert p_rocm.vram_gb == pytest.approx(16.0)
    assert p_rocm.tier == HardwareTier.TIER_2

    lspci_out = "00:02.0 VGA compatible controller: Intel Corporation Alder Lake-P Integrated Graphics\n"
    p_lspci = HardwareProfiler(
        command_runner=_make_runner({"lspci": (0, lspci_out, "")}),
        os_name="Linux",
        cpu_cores=8,
        proc_meminfo_path=meminfo,
    ).detect()
    assert p_lspci.detection_method == "lspci"
    assert "Intel Corporation" in p_lspci.gpu_name
    assert p_lspci.tier == HardwareTier.TIER_0


def test_macos_apple_silicon_and_discrete_gpu() -> None:
    apple_json = json.dumps(
        {"SPDisplaysDataType": [{"sppci_model": "Apple M3 Pro", "_name": "Apple M3 Pro"}]}
    )
    p_m3 = HardwareProfiler(
        command_runner=_make_runner(
            {
                "sysctl": (0, str(16 * 1024**3) + "\n", ""),
                "system_profiler": (0, apple_json, ""),
            }
        ),
        os_name="Darwin",
        cpu_cores=12,
    ).detect()
    assert p_m3.ram_gb == pytest.approx(16.0)
    assert p_m3.detection_method == "system_profiler"
    assert p_m3.vram_gb == pytest.approx(12.0)  # 75% unified memory budget
    assert p_m3.tier == HardwareTier.TIER_2

    discrete_json = json.dumps(
        {"SPDisplaysDataType": [{"sppci_model": "AMD Radeon Pro 5500M", "spdisplays_vram": "8 GB"}]}
    )
    p_disc = HardwareProfiler(
        command_runner=_make_runner(
            {
                "sysctl": (0, str(16 * 1024**3), ""),
                "system_profiler": (0, discrete_json, ""),
            }
        ),
        os_name="Darwin",
        cpu_cores=8,
    ).detect()
    assert p_disc.vram_gb == pytest.approx(8.0)
    assert p_disc.tier == HardwareTier.TIER_1


def test_cpu_fallback_memory_parser_and_default_runner(tmp_path: Path) -> None:
    p_cpu = HardwareProfiler(
        command_runner=_make_runner({}),
        os_name="Linux",
        cpu_cores=4,
        proc_meminfo_path=tmp_path / "missing_meminfo",
    ).detect()
    assert p_cpu.detection_method == "fallback-cpu"
    assert p_cpu.gpu_name == "CPU-only / Unknown"
    assert p_cpu.vram_gb == 0.0
    assert p_cpu.ram_gb == 0.0
    assert p_cpu.tier == HardwareTier.TIER_0

    assert _parse_memory_string_gb("1 TB") == pytest.approx(1024.0)
    assert _parse_memory_string_gb("8192 MB") == pytest.approx(8.0)
    assert _parse_memory_string_gb("1048576 KB") == pytest.approx(1.0)
    assert _parse_memory_string_gb(str(4 * 1024**3)) == pytest.approx(4.0)
    assert _parse_memory_string_gb("512 B") == pytest.approx(512.0)
    assert _parse_memory_string_gb("not-a-size") == 0.0
    assert _parse_memory_string_gb("") == 0.0

    rc_ok, out_ok, _ = _default_command_runner([sys.executable, "-c", "print('probe-ok')"])
    assert rc_ok == 0
    assert "probe-ok" in out_ok

    rc, out, err = _default_command_runner(["definitely_nonexistent_binary_xyz_12345"])
    assert rc == 1
    assert out == ""
    assert err != ""


def test_malformed_probe_outputs_fall_back_gracefully() -> None:
    # Windows malformed JSON on both DXGI and CIM + bad RAM string
    bad_win = HardwareProfiler(
        command_runner=_make_runner(
            {
                "TotalPhysicalMemory": (0, "not-an-int", ""),
                "qwMemorySize": (0, "{broken json", ""),
                "Win32_VideoController": (0, "{broken json", ""),
            }
        ),
        os_name="Windows",
        cpu_cores=4,
    ).detect()
    assert bad_win.detection_method == "fallback-cpu"
    assert bad_win.tier == HardwareTier.TIER_0

    # Windows DXGI & CIM valid JSON but non-dict / invalid field entries
    weird_win = HardwareProfiler(
        command_runner=_make_runner(
            {
                "qwMemorySize": (
                    0,
                    json.dumps(["not-a-dict", {"DriverDesc": "", "HardwareInformation.qwMemorySize": 100}, {"DriverDesc": "GPU", "HardwareInformation.qwMemorySize": "bad"}]),
                    "",
                ),
                "Win32_VideoController": (
                    0,
                    json.dumps(["not-a-dict", {"Name": "", "AdapterRAM": 100}, {"Name": "WeirdGPU", "AdapterRAM": "bad"}]),
                    "",
                ),
            }
        ),
        os_name="Windows",
        cpu_cores=4,
        ram_gb_provider=lambda: 8.0,
    ).detect()
    assert weird_win.detection_method == "powershell-cim"
    assert weird_win.gpu_name == "WeirdGPU"
    assert weird_win.vram_gb == 0.0

    # Linux malformed rocm-smi JSON falls through to CPU fallback
    bad_linux = HardwareProfiler(
        command_runner=_make_runner({"rocm-smi": (0, "{invalid", "")}),
        os_name="Linux",
        cpu_cores=4,
        ram_gb_provider=lambda: 8.0,
    ).detect()
    assert bad_linux.detection_method == "fallback-cpu"

    # macOS malformed sysctl & system_profiler JSON + unknown display entry
    bad_mac = HardwareProfiler(
        command_runner=_make_runner(
            {
                "sysctl": (0, "nan", ""),
                "system_profiler": (0, "{invalid", ""),
            }
        ),
        os_name="Darwin",
        cpu_cores=4,
    ).detect()
    assert bad_mac.ram_gb == 0.0
    assert bad_mac.detection_method == "fallback-cpu"

    weird_mac = HardwareProfiler(
        command_runner=_make_runner(
            {
                "system_profiler": (
                    0,
                    json.dumps(
                        {
                            "SPDisplaysDataType": [
                                "not-a-dict",
                                {"sppci_model": ""},
                                {"sppci_model": "Generic Display"},
                            ]
                        }
                    ),
                    "",
                )
            }
        ),
        os_name="Darwin",
        cpu_cores=4,
        ram_gb_provider=lambda: 8.0,
    ).detect()
    assert weird_mac.detection_method == "system_profiler"
    assert weird_mac.gpu_name == "Generic Display"
    assert weird_mac.vram_gb == 0.0


def test_platform_probe_error_branches_and_ctypes_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import ctypes
    from types import SimpleNamespace

    # 1. Windows CIM returns non-zero & ctypes windll absent or failing
    monkeypatch.setattr(ctypes, "windll", None, raising=False)
    assert _windows_global_memory_gb() is None

    fake_windll = SimpleNamespace(
        kernel32=SimpleNamespace(GlobalMemoryStatusEx=lambda _ptr: 0)
    )
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    assert _windows_global_memory_gb() is None

    # Windows RAM fallback returns 0.0 when both CIM and GlobalMemoryStatusEx fail
    win_fail = HardwareProfiler(
        command_runner=_make_runner({}),
        os_name="Windows",
        cpu_cores=4,
    ).detect()
    assert win_fail.ram_gb == 0.0
    assert win_fail.detection_method == "fallback-cpu"

    # 2. Linux /proc/meminfo unreadable (directory raises OSError)
    unreadable_dir = tmp_path / "meminfo_dir"
    unreadable_dir.mkdir()
    linux_err = HardwareProfiler(
        command_runner=_make_runner({}),
        os_name="Linux",
        cpu_cores=4,
        proc_meminfo_path=unreadable_dir,
    ).detect()
    assert linux_err.ram_gb == 0.0
    assert linux_err.detection_method == "fallback-cpu"

    # 3. macOS system_profiler failure & non-list SPDisplaysDataType
    mac_fail = HardwareProfiler(
        command_runner=_make_runner({}),
        os_name="Darwin",
        cpu_cores=4,
        ram_gb_provider=lambda: 16.0,
    ).detect()
    assert mac_fail.detection_method == "fallback-cpu"

    mac_non_list = HardwareProfiler(
        command_runner=_make_runner(
            {"system_profiler": (0, json.dumps({"SPDisplaysDataType": "unexpected"}), "")}
        ),
        os_name="Darwin",
        cpu_cores=4,
        ram_gb_provider=lambda: 16.0,
    ).detect()
    assert mac_non_list.detection_method == "fallback-cpu"


