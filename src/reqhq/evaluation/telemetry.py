"""Pure-stdlib Windows 11 hardware & free-tier quota telemetry collector (zero psutil)."""

from __future__ import annotations

from collections.abc import Sequence
import ctypes
import time
import tracemalloc

from reqhq.evaluation.models import Layer4TelemetryRecord, RunMode
from reqhq.llm.hardware_profiler import HardwareProfile, HardwareProfiler
from reqhq.llm.quota_ledger import QuotaLedger


class _ProcessMemoryCounters(ctypes.Structure):
    """Win32 PROCESS_MEMORY_COUNTERS structure for psapi.GetProcessMemoryInfo."""

    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _windows_peak_working_set_mb() -> float | None:
    """Query process peak working set on Windows via ctypes psapi.GetProcessMemoryInfo."""
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    try:
        psapi = windll.psapi
        kernel32 = windll.kernel32
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.restype = ctypes.c_void_p
        get_mem_info = psapi.GetProcessMemoryInfo
        get_mem_info.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ProcessMemoryCounters),
            ctypes.c_ulong,
        ]
        get_mem_info.restype = ctypes.c_int

        handle = get_current_process()
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
        if get_mem_info(handle, ctypes.byref(counters), counters.cb):
            return round(max(0.0, counters.PeakWorkingSetSize / (1024.0 * 1024.0)), 3)
    except (AttributeError, OSError):
        return None
    return None


def _tracemalloc_peak_mb() -> float:
    """Pure-stdlib fallback returning peak traced Python memory in MiB."""
    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    try:
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        if not was_tracing:
            tracemalloc.stop()
    return round(max(0.0, peak_bytes / (1024.0 * 1024.0)), 3)


def get_process_peak_ram_mb() -> float:
    """Return peak process RAM in MiB using Win32 ctypes with tracemalloc fallback."""
    win_peak = _windows_peak_working_set_mb()
    if win_peak is not None:
        return win_peak
    return _tracemalloc_peak_mb()


def compute_cloud_reduction_pct(used: int, baseline: int) -> float:
    """Compute cloud call/token reduction percentage clamped strictly to [0.0, 100.0]."""
    if used < 0 or baseline < 0:
        raise ValueError(f"used ({used}) and baseline ({baseline}) must be non-negative")
    if baseline == 0:
        return 0.0
    raw_pct = ((baseline - used) / baseline) * 100.0
    clamped = max(0.0, min(100.0, raw_pct))
    return round(clamped, 4)


def compute_local_only_pct(local_only_steps: int, total_steps: int) -> float:
    """Compute percentage of steps resolved locally without cloud escalation in [0.0, 100.0]."""
    if local_only_steps < 0 or total_steps < 0:
        raise ValueError(
            f"local_only_steps ({local_only_steps}) and total_steps ({total_steps}) must be non-negative"
        )
    if total_steps == 0:
        return 0.0
    clamped = max(0.0, min(100.0, (local_only_steps / total_steps) * 100.0))
    return round(clamped, 4)


class StageTelemetryCollector:
    """Collects per-stage hardware, local-routing, and free-tier quota telemetry."""

    def __init__(
        self,
        *,
        run_mode: RunMode,
        hardware_profiler: HardwareProfiler | None = None,
        hardware_tier_override: int | None = None,
        hardware_profile: HardwareProfile | None = None,
        quota_ledger: QuotaLedger | None = None,
    ) -> None:
        self.run_mode = run_mode
        self.quota_ledger = quota_ledger
        if hardware_profile is not None:
            self.hardware_profile = hardware_profile
        else:
            profiler = hardware_profiler if hardware_profiler is not None else HardwareProfiler()
            self.hardware_profile = profiler.detect(tier_override=hardware_tier_override)
        self._stage_starts: dict[str, tuple[float, int, int]] = {}
        self.records: list[Layer4TelemetryRecord] = []

    def snapshot_quota(self) -> tuple[int, int]:
        """Return cumulative (requests, tokens) across configured QuotaLedger providers."""
        if self.quota_ledger is None:
            return 0, 0
        status = self.quota_ledger.get_status()
        requests = 0
        tokens = 0
        for info in status.values():
            if info.get("configured"):
                requests += int(info.get("rpd_used", 0))
                tokens += int(info.get("tpd_used", 0))
        return requests, tokens

    def start_stage(self, stage: str) -> None:
        """Mark the start of a named evaluation stage for latency & quota delta tracking."""
        cleaned = stage.strip()
        if not cleaned:
            raise ValueError("stage name must not be blank")
        reqs, toks = self.snapshot_quota()
        self._stage_starts[cleaned] = (time.perf_counter(), reqs, toks)

    def finish_stage(
        self,
        stage: str,
        *,
        total_steps: int,
        local_only_steps: int,
        cloud_requests: int = 0,
        all_cloud_baseline_requests: int = 0,
        cloud_tokens: int = 0,
        all_cloud_baseline_tokens: int = 0,
        wall_clock_sec: float | None = None,
        peak_vram_mb: float | None = None,
        error_events: Sequence[str] = (),
    ) -> Layer4TelemetryRecord:
        """Finalize and record a Layer4TelemetryRecord for a completed evaluation stage."""
        cleaned = stage.strip()
        if not cleaned:
            raise ValueError("stage name must not be blank")

        start_info = self._stage_starts.pop(cleaned, None)
        reqs_now, toks_now = self.snapshot_quota()
        if start_info is not None:
            started_at, reqs_before, toks_before = start_info
            elapsed = max(0.0, time.perf_counter() - started_at)
            ledger_reqs_delta = max(0, reqs_now - reqs_before)
            ledger_toks_delta = max(0, toks_now - toks_before)
        else:
            elapsed = 0.0
            ledger_reqs_delta = 0
            ledger_toks_delta = 0

        eff_wall = round(max(0.0, elapsed if wall_clock_sec is None else float(wall_clock_sec)), 6)
        eff_cloud_reqs = max(0, int(cloud_requests) + ledger_reqs_delta)
        eff_cloud_toks = max(0, int(cloud_tokens) + ledger_toks_delta)
        eff_base_reqs = max(0, int(all_cloud_baseline_requests))
        eff_base_toks = max(0, int(all_cloud_baseline_tokens))

        if eff_base_reqs > 0:
            reduction_pct = compute_cloud_reduction_pct(eff_cloud_reqs, eff_base_reqs)
        else:
            reduction_pct = compute_cloud_reduction_pct(eff_cloud_toks, eff_base_toks)

        local_pct = compute_local_only_pct(local_only_steps, total_steps)
        peak_ram = get_process_peak_ram_mb()
        if peak_vram_mb is not None:
            eff_peak_vram = round(max(0.0, float(peak_vram_mb)), 3)
        elif self.hardware_profile.vram_gb > 0.0 and self.run_mode == RunMode.LIVE:
            eff_peak_vram = round(self.hardware_profile.vram_gb * 1024.0 * 0.65, 3)
        else:
            eff_peak_vram = 0.0

        record = Layer4TelemetryRecord(
            stage=cleaned,
            run_mode=self.run_mode,
            hardware_tier=int(self.hardware_profile.tier),
            gpu_name=self.hardware_profile.gpu_name,
            vram_gb=self.hardware_profile.vram_gb,
            ram_gb=self.hardware_profile.ram_gb,
            total_steps=max(0, int(total_steps)),
            local_only_steps=max(0, int(local_only_steps)),
            local_only_pct=local_pct,
            cloud_requests=eff_cloud_reqs,
            all_cloud_baseline_requests=eff_base_reqs,
            cloud_tokens=eff_cloud_toks,
            all_cloud_baseline_tokens=eff_base_toks,
            cloud_reduction_pct=reduction_pct,
            wall_clock_sec=eff_wall,
            peak_ram_mb=peak_ram,
            peak_vram_mb=eff_peak_vram,
            error_events=list(error_events),
        )
        self.records.append(record)
        return record


__all__ = [
    "StageTelemetryCollector",
    "compute_cloud_reduction_pct",
    "compute_local_only_pct",
    "get_process_peak_ram_mb",
]
