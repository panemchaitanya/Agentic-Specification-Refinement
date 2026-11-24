"""Cross-platform hardware profiler with Windows 11 VRAM detection and tier mapping."""

from __future__ import annotations

import ctypes
import functools
import json
import os
import platform
import re
import subprocess
from collections.abc import Callable
from enum import IntEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

CommandRunner = Callable[[list[str]], tuple[int, str, str]]
DirectMLProbe = Callable[[], tuple[str, float] | None]


class HardwareTier(IntEnum):
    """VRAM-based hardware capability tiers for local SLM selection."""

    TIER_0 = 0  # <= 4 GB VRAM (or CPU-only)
    TIER_1 = 1  # 5-8 GB VRAM (and up to <12 GB)
    TIER_2 = 2  # 12-16 GB VRAM (and up to <24 GB)
    TIER_3 = 3  # 24+ GB VRAM


class HardwareProfile(BaseModel):
    """Detected (or overridden) host hardware capabilities."""

    model_config = ConfigDict(extra="forbid")

    os_name: str = Field(..., min_length=1)
    cpu_cores: int = Field(..., ge=1)
    ram_gb: float = Field(..., ge=0.0)
    gpu_name: str = Field(..., min_length=1)
    vram_gb: float = Field(..., ge=0.0)
    detection_method: str = Field(..., min_length=1)
    tier: HardwareTier
    overridden: bool = False


def classify_vram_tier(vram_gb: float) -> HardwareTier:
    """Map detected GPU VRAM (GB) to a deterministic HardwareTier."""
    if vram_gb >= 24.0:
        return HardwareTier.TIER_3
    if vram_gb >= 12.0:
        return HardwareTier.TIER_2
    if vram_gb > 4.0:
        return HardwareTier.TIER_1
    return HardwareTier.TIER_0


def _default_command_runner(cmd: list[str]) -> tuple[int, str, str]:
    """Run an external probe command with strict timeout and zero shell interpolation."""
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return completed.returncode, completed.stdout or "", completed.stderr or ""
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def _windows_global_memory_gb() -> float | None:
    """Query total physical RAM on Windows via GlobalMemoryStatusEx (stdlib ctypes)."""

    class _MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    kernel32 = getattr(ctypes, "windll", None)
    if kernel32 is None:
        return None
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    if kernel32.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return round(status.ullTotalPhys / (1024**3), 2)
    return None


def _parse_memory_string_gb(raw: str) -> float:
    """Parse strings like '8 GB', '16384 MB', or raw byte counts into gigabytes."""
    cleaned = raw.strip()
    if not cleaned:
        return 0.0
    match = re.search(r"(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB|B)?", cleaned, re.IGNORECASE)
    if not match:
        return 0.0
    value = float(match.group(1))
    unit = (match.group(2) or "B").upper()
    if unit == "TB":
        return round(value * 1024.0, 2)
    if unit == "GB":
        return round(value, 2)
    if unit == "MB":
        return round(value / 1024.0, 2)
    if unit == "KB":
        return round(value / (1024.0**2), 2)
    if value > 1024**2:
        return round(value / (1024.0**3), 2)
    return round(value, 2)


class _DualMethod:
    """Allow HardwareProfiler.detect(...) to be called on either the class or an instance."""

    def __init__(self, func: Callable[..., HardwareProfile]) -> None:
        self._func = func

    def __get__(
        self,
        instance: HardwareProfiler | None,
        owner: type[HardwareProfiler],
    ) -> Callable[..., HardwareProfile]:
        target = instance if instance is not None else owner()
        return functools.partial(self._func, target)


class HardwareProfiler:
    """Detects host OS, CPU, RAM, GPU, and VRAM across Windows 11, Linux, and macOS."""

    def __init__(
        self,
        *,
        command_runner: CommandRunner | None = None,
        os_name: str | None = None,
        cpu_cores: int | None = None,
        ram_gb_provider: Callable[[], float] | None = None,
        directml_probe: DirectMLProbe | None = None,
        proc_meminfo_path: Path | None = None,
    ) -> None:
        self._run = command_runner or _default_command_runner
        self._os_name = os_name or platform.system() or "Unknown"
        self._cpu_cores = cpu_cores
        self._ram_gb_provider = ram_gb_provider
        self._directml_probe = directml_probe
        self._proc_meminfo_path = proc_meminfo_path or Path("/proc/meminfo")

    def _detect_cpu_cores(self) -> int:
        if self._cpu_cores is not None:
            return max(1, int(self._cpu_cores))
        return max(1, os.cpu_count() or 1)

    def _detect_ram_gb(self) -> float:
        if self._ram_gb_provider is not None:
            return round(max(0.0, float(self._ram_gb_provider())), 2)

        os_lower = self._os_name.lower()
        if os_lower == "windows":
            # Prefer stdlib ctypes GlobalMemoryStatusEx on live Windows hosts;
            # fall back to PowerShell CIM when running non-Windows tests simulating Windows.
            if platform.system().lower() == "windows" and self._run is _default_command_runner:
                mem = _windows_global_memory_gb()
                if mem is not None and mem > 0:
                    return mem
            rc, out, _ = self._run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory",
                ]
            )
            if rc == 0 and out.strip():
                try:
                    return round(int(out.strip().splitlines()[0]) / (1024**3), 2)
                except ValueError:
                    pass
            if platform.system().lower() == "windows":
                mem = _windows_global_memory_gb()
                if mem is not None:
                    return mem
            return 0.0

        if os_lower == "darwin":
            rc, out, _ = self._run(["sysctl", "-n", "hw.memsize"])
            if rc == 0 and out.strip():
                try:
                    return round(int(out.strip().splitlines()[0]) / (1024**3), 2)
                except ValueError:
                    pass
            return 0.0

        # Linux / POSIX
        if self._proc_meminfo_path.exists():
            try:
                text = self._proc_meminfo_path.read_text(encoding="utf-8", errors="ignore")
                match = re.search(r"MemTotal:\s*(\d+)\s*kB", text)
                if match:
                    return round(int(match.group(1)) / (1024**2), 2)
            except OSError:
                pass
        return 0.0

    def _probe_nvidia_smi(self) -> tuple[str, float] | None:
        rc, out, _ = self._run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ]
        )
        if rc != 0 or not out.strip():
            return None

        best_gpu: tuple[str, float] | None = None
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                continue
            name = parts[0]
            try:
                mib = float(parts[1])
            except ValueError:
                continue
            vram_gb = round(mib / 1024.0, 2)
            if best_gpu is None or vram_gb > best_gpu[1]:
                best_gpu = (name, vram_gb)
        return best_gpu

    def _probe_windows_directml_dxgi(self) -> tuple[str, float] | None:
        if self._directml_probe is not None:
            result = self._directml_probe()
            if result is not None:
                return result[0], round(max(0.0, float(result[1])), 2)

        # Query 64-bit DXGI display class memory size via PowerShell (avoids 32-bit WMI cap).
        ps_cmd = (
            "Get-ItemProperty "
            "'HKLM:\\SYSTEM\\ControlSet001\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}\\0*' "
            "-ErrorAction SilentlyContinue | "
            "Select-Object DriverDesc, 'HardwareInformation.qwMemorySize' | "
            "ConvertTo-Json -Compress"
        )
        rc, out, _ = self._run(["powershell", "-NoProfile", "-Command", ps_cmd])
        if rc != 0 or not out.strip():
            return None

        try:
            parsed: Any = json.loads(out)
        except json.JSONDecodeError:
            return None

        items = parsed if isinstance(parsed, list) else [parsed]
        best_gpu: tuple[str, float] | None = None
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("DriverDesc") or "").strip()
            raw_mem = item.get("HardwareInformation.qwMemorySize")
            if not name or raw_mem is None:
                continue
            try:
                if isinstance(raw_mem, list) and raw_mem and all(
                    isinstance(b, int) and 0 <= b <= 255 for b in raw_mem
                ):
                    raw_bytes = int.from_bytes(bytes(raw_mem), byteorder="little", signed=False)
                else:
                    raw_bytes = int(raw_mem)
                vram_gb = round(raw_bytes / (1024**3), 2)
            except (TypeError, ValueError):
                continue
            if vram_gb > 0 and (best_gpu is None or vram_gb > best_gpu[1]):
                best_gpu = (name, vram_gb)
        return best_gpu

    def _probe_windows_powershell_cim(self) -> tuple[str, float] | None:
        ps_cmd = (
            "Get-CimInstance Win32_VideoController | "
            "Select-Object Name,AdapterRAM | ConvertTo-Json -Compress"
        )
        rc, out, _ = self._run(["powershell", "-NoProfile", "-Command", ps_cmd])
        if rc != 0 or not out.strip():
            return None
        try:
            parsed: Any = json.loads(out)
        except json.JSONDecodeError:
            return None

        items = parsed if isinstance(parsed, list) else [parsed]
        best_gpu: tuple[str, float] | None = None
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("Name") or "").strip()
            raw_ram = item.get("AdapterRAM")
            if not name:
                continue
            vram_gb = 0.0
            if raw_ram is not None:
                try:
                    vram_gb = round(max(0, int(raw_ram)) / (1024**3), 2)
                except (TypeError, ValueError):
                    vram_gb = 0.0
            if best_gpu is None or vram_gb > best_gpu[1]:
                best_gpu = (name, vram_gb)
        return best_gpu

    def _probe_linux_rocm_or_lspci(self) -> tuple[str, float, str] | None:
        rc, out, _ = self._run(["rocm-smi", "--showmeminfo", "vram", "--json"])
        if rc == 0 and out.strip():
            try:
                data = json.loads(out)
                best_vram = 0.0
                for card_data in data.values():
                    if isinstance(card_data, dict):
                        total_bytes = card_data.get("VRAM Total Memory (B)")
                        if total_bytes is not None:
                            best_vram = max(best_vram, round(int(total_bytes) / (1024**3), 2))
                if best_vram > 0:
                    return ("AMD ROCm GPU", best_vram, "rocm-smi")
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

        rc, out, _ = self._run(["lspci"])
        if rc == 0 and out.strip():
            for line in out.splitlines():
                if any(tag in line for tag in ("VGA compatible controller", "3D controller")):
                    desc = line.split(":", 2)[-1].strip()
                    if desc:
                        return (desc, 0.0, "lspci")
        return None

    def _probe_macos_displays(self, ram_gb: float) -> tuple[str, float] | None:
        rc, out, _ = self._run(["system_profiler", "SPDisplaysDataType", "-json"])
        if rc != 0 or not out.strip():
            return None
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return None

        displays = data.get("SPDisplaysDataType", [])
        if not isinstance(displays, list):
            return None

        best_gpu: tuple[str, float] | None = None
        for entry in displays:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("sppci_model") or entry.get("_name") or "").strip()
            if not name:
                continue
            raw_vram = str(
                entry.get("spdisplays_vram") or entry.get("spdisplays_vram_shared") or ""
            ).strip()
            if raw_vram:
                vram_gb = _parse_memory_string_gb(raw_vram)
            elif "apple" in name.lower() and ram_gb > 0:
                # Apple Silicon unified memory budget (~75% usable by Metal/GPU)
                vram_gb = round(ram_gb * 0.75, 2)
            else:
                vram_gb = 0.0
            if best_gpu is None or vram_gb > best_gpu[1]:
                best_gpu = (name, vram_gb)
        return best_gpu

    def _detect_impl(self, tier_override: int | None = None) -> HardwareProfile:
        cpu_cores = self._detect_cpu_cores()
        ram_gb = self._detect_ram_gb()

        if tier_override is not None:
            if isinstance(tier_override, bool):
                raise ValueError(
                    f"Invalid hardware tier override {tier_override!r}: expected 0, 1, 2, or 3"
                )
            try:
                resolved_tier = HardwareTier(int(tier_override))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid hardware tier override {tier_override!r}: expected 0, 1, 2, or 3"
                ) from exc
            return HardwareProfile(
                os_name=self._os_name,
                cpu_cores=cpu_cores,
                ram_gb=ram_gb,
                gpu_name="Overridden",
                vram_gb=0.0,
                detection_method="override",
                tier=resolved_tier,
                overridden=True,
            )

        # 1. Cross-platform NVIDIA probe
        nvidia = self._probe_nvidia_smi()
        if nvidia is not None:
            gpu_name, vram_gb = nvidia
            return HardwareProfile(
                os_name=self._os_name,
                cpu_cores=cpu_cores,
                ram_gb=ram_gb,
                gpu_name=gpu_name,
                vram_gb=vram_gb,
                detection_method="nvidia-smi",
                tier=classify_vram_tier(vram_gb),
                overridden=False,
            )

        os_lower = self._os_name.lower()

        # 2. Windows 11 probes: DirectML/DXGI -> PowerShell CIM Win32_VideoController
        if os_lower == "windows":
            dxgi = self._probe_windows_directml_dxgi()
            if dxgi is not None:
                gpu_name, vram_gb = dxgi
                return HardwareProfile(
                    os_name=self._os_name,
                    cpu_cores=cpu_cores,
                    ram_gb=ram_gb,
                    gpu_name=gpu_name,
                    vram_gb=vram_gb,
                    detection_method="directml-dxgi",
                    tier=classify_vram_tier(vram_gb),
                    overridden=False,
                )

            cim = self._probe_windows_powershell_cim()
            if cim is not None:
                gpu_name, vram_gb = cim
                return HardwareProfile(
                    os_name=self._os_name,
                    cpu_cores=cpu_cores,
                    ram_gb=ram_gb,
                    gpu_name=gpu_name,
                    vram_gb=vram_gb,
                    detection_method="powershell-cim",
                    tier=classify_vram_tier(vram_gb),
                    overridden=False,
                )

        # 3. macOS probe
        if os_lower == "darwin":
            mac = self._probe_macos_displays(ram_gb)
            if mac is not None:
                gpu_name, vram_gb = mac
                return HardwareProfile(
                    os_name=self._os_name,
                    cpu_cores=cpu_cores,
                    ram_gb=ram_gb,
                    gpu_name=gpu_name,
                    vram_gb=vram_gb,
                    detection_method="system_profiler",
                    tier=classify_vram_tier(vram_gb),
                    overridden=False,
                )

        # 4. Linux probe
        if os_lower == "linux":
            linux_gpu = self._probe_linux_rocm_or_lspci()
            if linux_gpu is not None:
                gpu_name, vram_gb, method = linux_gpu
                return HardwareProfile(
                    os_name=self._os_name,
                    cpu_cores=cpu_cores,
                    ram_gb=ram_gb,
                    gpu_name=gpu_name,
                    vram_gb=vram_gb,
                    detection_method=method,
                    tier=classify_vram_tier(vram_gb),
                    overridden=False,
                )

        # 5. Safe CPU-only fallback
        return HardwareProfile(
            os_name=self._os_name,
            cpu_cores=cpu_cores,
            ram_gb=ram_gb,
            gpu_name="CPU-only / Unknown",
            vram_gb=0.0,
            detection_method="fallback-cpu",
            tier=HardwareTier.TIER_0,
            overridden=False,
        )

    detect = _DualMethod(_detect_impl)


__all__ = [
    "CommandRunner",
    "DirectMLProbe",
    "HardwareProfile",
    "HardwareProfiler",
    "HardwareTier",
    "classify_vram_tier",
]
