"""Strict Pydantic v2 data models for Stage 4b Alloy 6 emission and verification."""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AlloyCompatMode(str, Enum):
    """Target Alloy language dialect for Layer 2 behavioral specifications."""

    ALLOY6 = "alloy6"
    LEGACY = "legacy"


class AlloyRunStatus(str, Enum):
    """Normalized verification outcome status from an Alloy analyzer run."""

    COMPILE_ERROR = "COMPILE_ERROR"
    VACUOUS_UNSAT = "VACUOUS_UNSAT"
    COUNTEREXAMPLE_FOUND = "COUNTEREXAMPLE_FOUND"
    VERIFIED = "VERIFIED"


class ExtractedAlloyBlock(BaseModel):
    """An Alloy module block extracted from literate Spec.md with @trace line maps."""

    model_config = ConfigDict(extra="forbid")

    module_name: str = Field(..., min_length=1)
    code: str
    start_line: int = Field(..., ge=1)
    end_line: int = Field(..., ge=1)
    line_to_req_ids: dict[int, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_line_bounds(self) -> ExtractedAlloyBlock:
        if self.end_line < self.start_line:
            raise ValueError(
                f"end_line ({self.end_line}) must be >= start_line ({self.start_line})"
            )
        return self

    def req_ids_for_lines(self, lines: Iterable[int]) -> list[str]:
        """Return deduplicated REQ-IDs traced to any of the given 1-based code lines."""
        seen: set[str] = set()
        ordered: list[str] = []
        for line_no in lines:
            for req_id in self.line_to_req_ids.get(line_no, []):
                if req_id not in seen:
                    seen.add(req_id)
                    ordered.append(req_id)
        return ordered

    def all_req_ids(self) -> list[str]:
        """Return all unique REQ-IDs referenced anywhere in this extracted block."""
        seen: set[str] = set()
        ordered: list[str] = []
        for line_no in sorted(self.line_to_req_ids):
            for req_id in self.line_to_req_ids[line_no]:
                if req_id not in seen:
                    seen.add(req_id)
                    ordered.append(req_id)
        return ordered


class AlloyRunResult(BaseModel):
    """Structured output of a single headless or mock Alloy Analyzer execution."""

    model_config = ConfigDict(extra="forbid")

    status: AlloyRunStatus
    command: str = ""
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    vacuity_passed: bool = False
    unsat_lines: list[int] = Field(default_factory=list)
    unsat_facts: list[str] = Field(default_factory=list)
    counterexample_trace: list[dict[str, Any]] = Field(default_factory=list)
    elapsed_ms: float = Field(default=0.0, ge=0.0)


class ConflictQuestion(BaseModel):
    """Plain-language conflict resolution question mapped from an Alloy UNSAT core."""

    model_config = ConfigDict(extra="forbid")

    conflict_id: str = Field(..., min_length=1)
    req_ids: list[str] = Field(default_factory=list)
    unsat_facts: list[str] = Field(default_factory=list)
    prompt: str = Field(..., min_length=1)
    suggested_resolutions: list[str] = Field(default_factory=list)


class CounterexamplePrompt(BaseModel):
    """Concrete user-facing scenario translated from an Alloy counterexample trace."""

    model_config = ConfigDict(extra="forbid")

    assertion_name: str = Field(..., min_length=1)
    req_ids: list[str] = Field(default_factory=list)
    scenario_steps: list[str] = Field(default_factory=list)
    question: str = Field(..., min_length=1)


class AlloyVerificationOutcome(BaseModel):
    """End-to-end outcome of the closed-loop Alloy neuro-symbolic verification gate."""

    model_config = ConfigDict(extra="forbid")

    status: AlloyRunStatus
    compat_mode: AlloyCompatMode
    spec_markdown: str
    core_domain_als: str
    behavior_als: str
    repair_attempts: int = Field(default=0, ge=0)
    escalated: bool = False
    cloud_verified: bool = False
    vacuity_checked: bool = False
    run_results: list[AlloyRunResult] = Field(default_factory=list)
    conflict_questions: list[ConflictQuestion] = Field(default_factory=list)
    counterexample_prompts: list[CounterexamplePrompt] = Field(default_factory=list)
    req_to_alloy_refs: dict[str, list[str]] = Field(default_factory=dict)


__all__ = [
    "AlloyCompatMode",
    "AlloyRunResult",
    "AlloyRunStatus",
    "AlloyVerificationOutcome",
    "ConflictQuestion",
    "CounterexamplePrompt",
    "ExtractedAlloyBlock",
]
