"""Strict Pydantic v2 data models for Phase 4 Layers 1-4 evaluation orchestration."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_VALID_LAYERS: frozenset[int] = frozenset({1, 2, 3, 4})
_VALID_ROUTES: frozenset[str] = frozenset({"software", "process", "training", "hybrid"})
_SHA256_LEN = 64


def _require_non_empty(value: str, field_name: str = "Field") -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be blank or whitespace-only")
    return cleaned


class RunMode(str, Enum):
    """Execution mode enforcing strict fail-closed separation between mock and live runs."""

    MOCK = "mock"
    LIVE = "live"


class CodingTaskMeta(BaseModel):
    """Metadata descriptor for a greenfield Layer 3 benchmark coding task."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(..., min_length=1)
    domain: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    target_module: str = Field(..., min_length=1)
    entry_symbol: str = Field(..., min_length=1)
    required_invariants: list[str] = Field(..., min_length=1)
    expected_routes: list[str] = Field(..., min_length=1)
    task_dir: Path | None = None

    @field_validator("task_id", "domain", "title", "target_module", "entry_symbol")
    @classmethod
    def _validate_non_empty_str(cls, value: str) -> str:
        return _require_non_empty(value)

    @field_validator("required_invariants")
    @classmethod
    def _validate_invariants(cls, values: list[str]) -> list[str]:
        return [_require_non_empty(item, "required_invariant") for item in values]

    @field_validator("expected_routes")
    @classmethod
    def _validate_routes(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for route in values:
            norm = _require_non_empty(route, "expected_route").lower()
            if norm not in _VALID_ROUTES:
                raise ValueError(
                    f"Invalid expected route {route!r}; must be one of {sorted(_VALID_ROUTES)}"
                )
            cleaned.append(norm)
        return cleaned


class EvalRunConfig(BaseModel):
    """Configuration for a resumable Layers 1-4 evaluation run."""

    model_config = ConfigDict(extra="forbid")

    out_dir: Path
    layers: list[int] = Field(default_factory=lambda: [1, 2, 3, 4])
    mode: RunMode = RunMode.MOCK
    replicates: int = Field(default=2, ge=1, le=20)
    seed: int = 42
    max_tasks: int | None = Field(default=None, ge=1)
    hardware_tier_override: int | None = Field(default=None, ge=0, le=3)
    strict_local: bool = False
    alloy_compat: Literal["alloy6", "legacy"] = "alloy6"
    split: Literal["dev", "all"] = "dev"

    @field_validator("layers")
    @classmethod
    def _validate_layers(cls, values: list[int]) -> list[int]:
        if not values:
            raise ValueError("layers must contain at least one layer in {1, 2, 3, 4}")
        deduped: list[int] = []
        for layer in values:
            if layer not in _VALID_LAYERS:
                raise ValueError(f"Unsupported layer {layer!r}; expected values in 1..4")
            if layer not in deduped:
                deduped.append(layer)
        return deduped


class Layer1Record(BaseModel):
    """Per-document elicitation & gap-injection record (Layer 1, H1/H4)."""

    model_config = ConfigDict(extra="forbid")

    record_id: str = Field(..., min_length=1)
    item_id: str = Field(..., min_length=1)
    split: str = Field(..., min_length=1)
    seed: int
    run_mode: RunMode
    gold_verified: bool
    single_pass_recall: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    iterative_recall: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    iterative_precision: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    iterative_f1: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    all_cloud_f1: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    questions_asked: int = Field(..., ge=0)
    exhaustive_baseline_questions: int = Field(..., ge=1)
    redundant_question_rate: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    cloud_requests: int = Field(..., ge=0)
    all_cloud_requests: int = Field(..., ge=1)
    cloud_tokens: int = Field(..., ge=0)
    all_cloud_tokens: int = Field(..., ge=1)
    wall_clock_sec: float = Field(..., ge=0.0, allow_inf_nan=False)
    error_events: list[str] = Field(default_factory=list)


class Layer2Record(BaseModel):
    """Per-case cross-domain socio-technical routing & Alloy contradiction record (Layer 2, H2)."""

    model_config = ConfigDict(extra="forbid")

    record_id: str = Field(..., min_length=1)
    case_id: str = Field(..., min_length=1)
    domain: str = Field(..., min_length=1)
    input_kind: str = Field(..., min_length=1)
    run_mode: RunMode
    route_accuracy: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    alloy_contradictions_caught: int = Field(..., ge=0)
    llm_only_contradictions_caught: int = Field(..., ge=0)
    alloy_caught_binary: bool
    llm_only_caught_binary: bool
    routing_adr_rubric_score: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    software_only_rubric_score: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    cohens_kappa: float | None = Field(default=None, ge=-1.0, le=1.0, allow_inf_nan=False)
    wall_clock_sec: float = Field(..., ge=0.0, allow_inf_nan=False)
    error_events: list[str] = Field(default_factory=list)


class Layer3Record(BaseModel):
    """Per-(task, condition, replicate) downstream coding guardrail record (Layer 3, H3)."""

    model_config = ConfigDict(extra="forbid")

    record_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    domain: str = Field(..., min_length=1)
    condition: Literal["raw_prd", "single_pass_nl_spec", "reqhq_full_bundle"]
    replicate: int = Field(..., ge=1)
    seed: int
    run_mode: RunMode
    alloy_formalized: bool
    alloy_non_vacuous: bool
    lock_verified: bool
    hidden_pass_at_1: bool
    pbt_pass_rate: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    mutation_score: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    stall_count: int = Field(..., ge=0)
    wall_clock_sec: float = Field(..., ge=0.0, allow_inf_nan=False)
    cloud_requests: int = Field(..., ge=0)
    cloud_tokens: int = Field(..., ge=0)
    error_events: list[str] = Field(default_factory=list)


class Layer4TelemetryRecord(BaseModel):
    """Per-stage hardware & free-tier cloud reduction telemetry record (Layer 4, H4)."""

    model_config = ConfigDict(extra="forbid")

    stage: str = Field(..., min_length=1)
    run_mode: RunMode
    hardware_tier: int = Field(..., ge=0, le=3)
    gpu_name: str = Field(..., min_length=1)
    vram_gb: float = Field(..., ge=0.0, allow_inf_nan=False)
    ram_gb: float = Field(..., ge=0.0, allow_inf_nan=False)
    total_steps: int = Field(..., ge=0)
    local_only_steps: int = Field(..., ge=0)
    local_only_pct: float = Field(..., ge=0.0, le=100.0, allow_inf_nan=False)
    cloud_requests: int = Field(..., ge=0)
    all_cloud_baseline_requests: int = Field(..., ge=0)
    cloud_tokens: int = Field(..., ge=0)
    all_cloud_baseline_tokens: int = Field(..., ge=0)
    cloud_reduction_pct: float = Field(..., ge=0.0, le=100.0, allow_inf_nan=False)
    wall_clock_sec: float = Field(..., ge=0.0, allow_inf_nan=False)
    peak_ram_mb: float = Field(..., ge=0.0, allow_inf_nan=False)
    peak_vram_mb: float = Field(..., ge=0.0, allow_inf_nan=False)
    error_events: list[str] = Field(default_factory=list)


class EvalCheckpoint(BaseModel):
    """Atomic resumable execution checkpoint tracking completed evaluation units."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., min_length=1)
    config: EvalRunConfig
    completed_units: list[str] = Field(default_factory=list)
    updated_at: str = Field(..., min_length=1)


class EvalBundleManifest(BaseModel):
    """Tamper-evident manifest describing all JSONL logs emitted by an evaluation run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., min_length=1)
    run_mode: RunMode
    seed: int
    replicates: int = Field(..., ge=1)
    layers_completed: list[int]
    gold_verified: bool
    hardware_profile: dict[str, Any]
    log_files: dict[str, str]
    log_sha256: dict[str, str]
    record_counts: dict[str, int]
    created_at: str = Field(..., min_length=1)

    @field_validator("log_sha256")
    @classmethod
    def _validate_sha256_map(cls, mapping: dict[str, str]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for key, digest in mapping.items():
            norm = digest.strip().lower()
            if len(norm) != _SHA256_LEN or any(ch not in "0123456789abcdef" for ch in norm):
                raise ValueError(
                    f"log_sha256[{key!r}] must be a 64-character lowercase hex SHA-256 digest"
                )
            cleaned[key] = norm
        return cleaned


__all__ = [
    "CodingTaskMeta",
    "EvalBundleManifest",
    "EvalCheckpoint",
    "EvalRunConfig",
    "Layer1Record",
    "Layer2Record",
    "Layer3Record",
    "Layer4TelemetryRecord",
    "RunMode",
]
