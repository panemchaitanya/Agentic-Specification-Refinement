"""Strict Pydantic v2 models for Phase 3 handoff bundling, mutation testing, and experiments."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ExperimentCondition(str, Enum):
    """Experimental treatment condition for coding agent trials."""

    COND_A = "raw_prd"
    COND_B = "single_pass_nl_spec"
    COND_C = "reqhq_full_bundle"


class MutantOutcome(str, Enum):
    """Classification of a single mutation test execution."""

    KILLED = "killed"
    SURVIVED = "survived"
    TIMEOUT = "timeout"
    ERROR = "error"


def compute_mutation_score(
    killed: int,
    survived: int,
    timeout: int,
    errors: int = 0,
) -> float:
    """Compute bounded mutation score in [0.0, 1.0].

    Defined as ``killed / (killed + survived + timeout)`` when at least one
    evaluable mutant ran, and ``0.0`` otherwise (e.g. zero mutants or all
    mutants errored before test evaluation).
    """
    if killed < 0 or survived < 0 or timeout < 0 or errors < 0:
        raise ValueError("Mutant outcome counts must be non-negative")
    evaluable = killed + survived + timeout
    if evaluable == 0:
        return 0.0
    score = round(killed / evaluable, 6)
    return min(1.0, max(0.0, score))


class MutantRecord(BaseModel):
    """Single executed mutant with location, operator, diff snippets, and outcome."""

    model_config = ConfigDict(extra="forbid")

    mutant_id: str = Field(..., min_length=1)
    file_path: str = Field(..., min_length=1)
    line_no: int = Field(..., ge=1)
    operator: str = Field(..., min_length=1)
    original_snippet: str
    mutated_snippet: str
    outcome: MutantOutcome
    elapsed_ms: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)

    @field_validator("mutant_id", "file_path", "operator")
    @classmethod
    def _strip_required_str(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be blank or whitespace-only")
        return cleaned


class MutationReport(BaseModel):
    """Aggregated mutation testing report produced by a MutationRunner."""

    model_config = ConfigDict(extra="forbid")

    runner_name: str = Field(..., min_length=1)
    target_files: list[str] = Field(default_factory=list)
    total_mutants: int = Field(..., ge=0)
    killed: int = Field(..., ge=0)
    survived: int = Field(..., ge=0)
    timeout: int = Field(..., ge=0)
    errors: int = Field(default=0, ge=0)
    mutation_score: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    mutants: list[MutantRecord] = Field(default_factory=list)

    @field_validator("runner_name")
    @classmethod
    def _strip_runner_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("runner_name must not be blank or whitespace-only")
        return cleaned

    @model_validator(mode="after")
    def _validate_counts(self) -> MutationReport:
        accounted = self.killed + self.survived + self.timeout + self.errors
        if accounted != self.total_mutants:
            raise ValueError(
                f"Mutant count mismatch: killed({self.killed}) + survived({self.survived}) "
                f"+ timeout({self.timeout}) + errors({self.errors}) = {accounted} "
                f"!= total_mutants({self.total_mutants})"
            )
        if self.mutants and len(self.mutants) != self.total_mutants:
            raise ValueError(
                f"Length of mutants ({len(self.mutants)}) != total_mutants ({self.total_mutants})"
            )
        return self


class HandoffBundleManifest(BaseModel):
    """Manifest of all artifacts written into an Antigravity handoff bundle."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=1)
    out_dir: Path
    instructions_md: Path
    spec_md: Path
    design_md: Path
    process_training_md: Path
    pbt_spec_yaml: Path
    traceability_matrix_json: Path
    adr_files: list[Path] = Field(default_factory=list)
    guardrail_files: list[Path] = Field(default_factory=list)
    pbt_test_files: list[Path] = Field(default_factory=list)
    lock_file: Path

    @field_validator("session_id")
    @classmethod
    def _strip_session_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("session_id must not be blank or whitespace-only")
        return cleaned


RESULTS_CSV_FIELDNAMES: tuple[str, ...] = (
    "trial_id",
    "case_id",
    "condition",
    "seed",
    "hidden_pass_at_1",
    "pbt_pass_rate",
    "mutation_score",
    "lock_verified",
    "stall_count",
    "wall_clock_seconds",
    "quota_tokens_used",
    "quota_requests_used",
    "artifacts_dir",
)


class TrialResultRow(BaseModel):
    """Single row recorded to results.csv for an experimental trial."""

    model_config = ConfigDict(extra="forbid")

    trial_id: str = Field(..., min_length=1)
    case_id: str = Field(..., min_length=1)
    condition: ExperimentCondition
    seed: int
    hidden_pass_at_1: bool
    pbt_pass_rate: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    mutation_score: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    lock_verified: bool
    stall_count: int = Field(default=0, ge=0)
    wall_clock_seconds: float = Field(..., ge=0.0, allow_inf_nan=False)
    quota_tokens_used: int = Field(default=0, ge=0)
    quota_requests_used: int = Field(default=0, ge=0)
    artifacts_dir: str = Field(..., min_length=1)

    @field_validator("trial_id", "case_id", "artifacts_dir")
    @classmethod
    def _strip_non_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be blank or whitespace-only")
        return cleaned

    @classmethod
    def csv_fieldnames(cls) -> tuple[str, ...]:
        """Return the strict ordered CSV column headers."""
        return RESULTS_CSV_FIELDNAMES

    def to_csv_dict(self) -> dict[str, Any]:
        """Serialize this row into a deterministic dictionary matching RESULTS_CSV_FIELDNAMES."""
        return {
            "trial_id": self.trial_id,
            "case_id": self.case_id,
            "condition": self.condition.value,
            "seed": self.seed,
            "hidden_pass_at_1": "true" if self.hidden_pass_at_1 else "false",
            "pbt_pass_rate": f"{(self.pbt_pass_rate + 0.0):.6f}",
            "mutation_score": f"{(self.mutation_score + 0.0):.6f}",
            "lock_verified": "true" if self.lock_verified else "false",
            "stall_count": self.stall_count,
            "wall_clock_seconds": f"{(self.wall_clock_seconds + 0.0):.6f}",
            "quota_tokens_used": self.quota_tokens_used,
            "quota_requests_used": self.quota_requests_used,
            "artifacts_dir": self.artifacts_dir,
        }


__all__ = [
    "ExperimentCondition",
    "HandoffBundleManifest",
    "MutantOutcome",
    "MutantRecord",
    "MutationReport",
    "RESULTS_CSV_FIELDNAMES",
    "TrialResultRow",
    "compute_mutation_score",
]
