"""Phase 4/5 Layers 1-4 evaluation orchestrator, corpus scalability, ablations, telemetry, and coding tasks."""

from __future__ import annotations

from reqhq.evaluation.ablations import (
    ABLATION_ARMS,
    AblationArmName,
    AblationArmSummary,
    AblationItemRecord,
    AblationReport,
    AblationRunner,
    build_critics_for_arm,
    summarize_ablation_records,
)
from reqhq.evaluation.corpus_bench import (
    CorpusBenchmarkRunner,
    CorpusBenchSummary,
    CorpusDocRecord,
    V1_TRUNCATION_THRESHOLD_CHARS,
    summarize_corpus_records,
)
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
    EXTENDED_CODING_TASKS_DIR,
    EXTENDED_TASK_IDS,
    load_coding_suite,
    load_coding_tasks,
)
from reqhq.evaluation.telemetry import (
    StageTelemetryCollector,
    compute_cloud_reduction_pct,
    compute_local_only_pct,
    get_process_peak_ram_mb,
)

__all__ = [
    "ABLATION_ARMS",
    "AblationArmName",
    "AblationArmSummary",
    "AblationItemRecord",
    "AblationReport",
    "AblationRunner",
    "CONDITION_SPEC",
    "CodingTaskMeta",
    "CorpusBenchSummary",
    "CorpusBenchmarkRunner",
    "CorpusDocRecord",
    "DEFAULT_CODING_TASKS_DIR",
    "EXPECTED_TASK_IDS",
    "EXTENDED_CODING_TASKS_DIR",
    "EXTENDED_TASK_IDS",
    "EvalBundleManifest",
    "EvalCheckpoint",
    "EvalRunConfig",
    "LAYER_LOG_FILENAMES",
    "Layer1Record",
    "Layer2Record",
    "Layer3Record",
    "Layer4TelemetryRecord",
    "LiveExecutionUnavailableError",
    "RunMode",
    "StageTelemetryCollector",
    "V1_TRUNCATION_THRESHOLD_CHARS",
    "build_critics_for_arm",
    "compute_cloud_reduction_pct",
    "compute_local_only_pct",
    "get_process_peak_ram_mb",
    "load_coding_suite",
    "load_coding_tasks",
    "make_unit_key",
    "sha256_file",
    "summarize_ablation_records",
    "summarize_corpus_records",
]
