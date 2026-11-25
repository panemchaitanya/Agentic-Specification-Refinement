"""Strict Pydantic v2 schemas for Stage 3 elicitation, critics, and convergence."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from reqhq.schemas.requirements import RequirementGraph

_SHA256_LEN = 64
_HEX_CHARS = frozenset("0123456789abcdef")


class ConvergenceGateError(ValueError):
    """Raised when finalize() is called before convergence without accept_defaults=True."""


class FindingSeverity(str, Enum):
    """Severity tier assigned to a specialist critic finding."""

    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"


SEVERITY_RANK: dict[FindingSeverity, int] = {
    FindingSeverity.BLOCKER: 3,
    FindingSeverity.MAJOR: 2,
    FindingSeverity.MINOR: 1,
}


class CriticDomain(str, Enum):
    """Five sequential specialist critic domains for TIER_0 low-memory evaluation."""

    FUNCTIONAL_COMPLETENESS = "functional_completeness"
    NON_FUNCTIONAL_QUALITY = "non_functional_quality"
    LOGICAL_CONSISTENCY = "logical_consistency"
    PROCESS_OPERATIONAL = "process_operational"
    TRAINING_ADOPTION = "training_adoption"


def _validate_sha256_hex(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != _SHA256_LEN or any(ch not in _HEX_CHARS for ch in normalized):
        raise ValueError("source_sha256 must be a 64-character hex SHA-256 digest")
    return normalized


class CriticFinding(BaseModel):
    """Single actionable gap, contradiction, or risk flagged by a specialist critic."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(..., min_length=1)
    req_id: str = Field(..., min_length=1)
    critic: CriticDomain
    severity: FindingSeverity
    title: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    suggested_question: str = Field(..., min_length=1)
    recommended_default: str = ""
    resolved: bool = False
    resolved_by_turn: str | None = None
    source_sha256: str | None = None

    @field_validator("finding_id", "req_id", "title", "description", "suggested_question")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be blank or whitespace-only")
        return cleaned

    @field_validator("source_sha256")
    @classmethod
    def _validate_optional_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_sha256_hex(value)


class CriticDisagreement(BaseModel):
    """Recorded conflict between two or more critics evaluating the same requirement."""

    model_config = ConfigDict(extra="forbid")

    req_id: str = Field(..., min_length=1)
    critics: list[CriticDomain] = Field(..., min_length=2)
    finding_ids: list[str] = Field(default_factory=list)
    summary: str = Field(..., min_length=1)
    resolution: str = ""
    escalated: bool = False
    cloud_verified: bool = False

    @field_validator("req_id", "summary")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be blank or whitespace-only")
        return cleaned


class ElicitationQuestion(BaseModel):
    """Batched high-information-gain user question derived from critic findings."""

    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    req_ids: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    severity: FindingSeverity
    info_gain_score: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    default_assumption: str = ""

    @field_validator("question_id", "prompt")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be blank or whitespace-only")
        return cleaned


class UserAnswer(BaseModel):
    """User answer recorded during an elicitation turn with full provenance."""

    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(..., min_length=1)
    answer: str = Field(..., min_length=1)
    turn_id: str = Field(..., min_length=1)
    source: str = Field(..., min_length=1)

    @field_validator("question_id", "answer", "turn_id", "source")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be blank or whitespace-only")
        return cleaned


class AssumptionRecord(BaseModel):
    """Unconfirmed assumption generated when finalizing with accept_defaults=True."""

    model_config = ConfigDict(extra="forbid")

    assumption_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    statement: str = Field(..., min_length=1)
    req_ids: list[str] = Field(..., min_length=1)
    finding_ids: list[str] = Field(default_factory=list)
    source_sha256: str = Field(..., min_length=_SHA256_LEN, max_length=_SHA256_LEN)
    rationale: str = Field(..., min_length=1)
    type: Literal["assumption"] = "assumption"
    user_confirmed: Literal[False] = False

    @field_validator("assumption_id", "title", "statement", "rationale")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be blank or whitespace-only")
        return cleaned

    @field_validator("source_sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        return _validate_sha256_hex(value)


class ElicitationState(BaseModel):
    """Persistent Stage 3 elicitation session state."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=1)
    graph: RequirementGraph
    findings: list[CriticFinding] = Field(default_factory=list)
    disagreements: list[CriticDisagreement] = Field(default_factory=list)
    pending_questions: list[ElicitationQuestion] = Field(default_factory=list)
    answers: list[UserAnswer] = Field(default_factory=list)
    assumptions: list[AssumptionRecord] = Field(default_factory=list)
    stale_req_ids: list[str] = Field(default_factory=list)
    turn_count: int = Field(default=0, ge=0)
    converged: bool = False
    finalized: bool = False
    accepted_defaults: bool = False

    @field_validator("session_id")
    @classmethod
    def _strip_session_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("session_id must not be blank")
        return cleaned


__all__ = [
    "AssumptionRecord",
    "ConvergenceGateError",
    "CriticDisagreement",
    "CriticDomain",
    "CriticFinding",
    "ElicitationQuestion",
    "ElicitationState",
    "FindingSeverity",
    "SEVERITY_RANK",
    "UserAnswer",
]
