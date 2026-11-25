"""Phase 4 Layers 1-4 evaluation orchestrator, telemetry, and greenfield coding tasks."""

from __future__ import annotations

from reqhq.evaluation.models import (
    CodingTaskMeta,
    EvalBundleManifest,
    EvalCheckpoint,
    EvalRunConfig,
    Layer1Record,
    Layer2Record,
    Layer3Record,
    Layer4TelemetryRecord,
    RunMode,
)
from reqhq.evaluation.runner import (
    CONDITION_SPEC,
    LAYER_LOG_FILENAMES,
    EvalOrchestrator,
    LiveExecutionUnavailableError,
    make_unit_key,
    sha256_file,
)
from reqhq.evaluation.tasks import (
    DEFAULT_CODING_TASKS_DIR,
    EXPECTED_TASK_IDS,
    load_coding_tasks,
)
from reqhq.evaluation.telemetry import (
    StageTelemetryCollector,
    compute_cloud_reduction_pct,
    compute_local_only_pct,
    get_process_peak_ram_mb,
)

__all__ = [
    "CONDITION_SPEC",
    "CodingTaskMeta",
    "DEFAULT_CODING_TASKS_DIR",
    "EXPECTED_TASK_IDS",
    "EvalBundleManifest",
    "EvalCheckpoint",
    "EvalOrchestrator",
    "EvalRunConfig",
    "LAYER_LOG_FILENAMES",
    "Layer1Record",
    "Layer2Record",
    "Layer3Record",
    "Layer4TelemetryRecord",
    "LiveExecutionUnavailableError",
    "RunMode",
    "StageTelemetryCollector",
    "compute_cloud_reduction_pct",
    "compute_local_only_pct",
    "get_process_peak_ram_mb",
    "load_coding_tasks",
    "make_unit_key",
    "sha256_file",
]
