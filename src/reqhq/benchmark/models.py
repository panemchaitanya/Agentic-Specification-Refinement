"""Strict Pydantic v2 models for reqhq Phase 3 benchmarks, gap injection, and adjudication."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256_LEN = 64

DomainName = Literal[
    "fintech_settlements",
    "healthcare_operations",
    "inventory_logistics",
    "enterprise_it_hr",
]
InputKind = Literal["one_liner", "multi_paragraph"]
RouteTrack = Literal["software", "process", "training", "hybrid"]

EXPECTED_DOMAINS: tuple[DomainName, ...] = (
    "fintech_settlements",
    "healthcare_operations",
    "inventory_logistics",
    "enterprise_it_hr",
)


def _require_non_empty(value: str, field_name: str = "Field") -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be blank or whitespace-only")
    return cleaned


class GapCategory(str, Enum):
    """Category of a requirement clause eligible for controlled gap injection."""

    NFR = "nfr"
    BOUNDARY = "boundary"
    PROCESS = "process"
    TRAINING = "training"
    SECURITY = "security"


class GapClause(BaseModel):
    """Single requirement clause that can be retained or stripped during gap injection."""

    model_config = ConfigDict(extra="forbid")

    clause_id: str = Field(..., min_length=1)
    category: GapCategory
    text: str = Field(..., min_length=1)
    keywords: list[str] = Field(default_factory=list)

    @field_validator("clause_id", "text")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        return _require_non_empty(value)

    @field_validator("keywords")
    @classmethod
    def _clean_keywords(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in values:
            stripped = item.strip()
            if stripped and stripped.lower() not in seen:
                cleaned.append(stripped)
                seen.add(stripped.lower())
        return cleaned


class GoldGapManifest(BaseModel):
    """Ground-truth manifest recording stripped and retained clauses for a gap-injection run."""

    model_config = ConfigDict(extra="forbid")

    spec_id: str = Field(..., min_length=1)
    seed: int
    drop_ratio: float = Field(..., ge=0.20, le=0.40)
    total_clauses: int = Field(..., ge=0)
    stripped_clauses: list[GapClause]
    retained_clauses: list[GapClause]

    @field_validator("spec_id")
    @classmethod
    def _validate_spec_id(cls, value: str) -> str:
        return _require_non_empty(value, "spec_id")

    @model_validator(mode="after")
    def _validate_clause_partition(self) -> GoldGapManifest:
        combined = len(self.stripped_clauses) + len(self.retained_clauses)
        if combined != self.total_clauses:
            raise ValueError(
                f"total_clauses ({self.total_clauses}) must equal "
                f"len(stripped_clauses) + len(retained_clauses) ({combined})"
            )
        stripped_ids = [c.clause_id for c in self.stripped_clauses]
        retained_ids = [c.clause_id for c in self.retained_clauses]
        all_ids = stripped_ids + retained_ids
        if len(set(all_ids)) != len(all_ids):
            raise ValueError("clause_id values must be unique across stripped and retained clauses")
        return self


class GapInjectionResult(BaseModel):
    """Output of deterministic gap injection: degraded PRD + hidden gold manifest."""

    model_config = ConfigDict(extra="forbid")

    degraded_prd: str
    manifest: GoldGapManifest
    leakage_verified: bool = True


class OracleAnswer(BaseModel):
    """Single question-answer exchange recorded by the frozen oracle simulator."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., min_length=1)
    answer: str = Field(..., min_length=1)
    matched_clause_ids: list[str] = Field(default_factory=list)
    is_redundant: bool = False
    found_in_ground_truth: bool = False

    @field_validator("question", "answer")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _require_non_empty(value)


class OracleEvalMetrics(BaseModel):
    """Aggregate session metrics computed by the frozen oracle simulator."""

    model_config = ConfigDict(extra="forbid")

    gap_recall: float = Field(..., ge=0.0, le=1.0)
    precision: float = Field(..., ge=0.0, le=1.0)
    f1: float = Field(..., ge=0.0, le=1.0)
    questions_to_convergence: int = Field(..., ge=0)
    redundant_question_rate: float = Field(..., ge=0.0, le=1.0)
    total_questions: int = Field(..., ge=0)
    matched_gaps: int = Field(..., ge=0)
    total_gaps: int = Field(..., ge=0)


class CrossDomainCase(BaseModel):
    """One benchmark scenario in the 16-case cross-domain socio-technical suite."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(..., min_length=1)
    domain: DomainName
    input_kind: InputKind
    title: str = Field(..., min_length=1)
    prompt_text: str = Field(..., min_length=1)
    gold_requirements: list[str] = Field(..., min_length=1)
    expected_routes: dict[str, RouteTrack] = Field(..., min_length=1)
    adr_rubric_anchors: list[str] = Field(..., min_length=1)

    @field_validator("case_id", "title", "prompt_text")
    @classmethod
    def _validate_strings(cls, value: str) -> str:
        return _require_non_empty(value)

    @field_validator("gold_requirements", "adr_rubric_anchors")
    @classmethod
    def _validate_non_empty_lists(cls, values: list[str]) -> list[str]:
        return [_require_non_empty(v) for v in values]

    @field_validator("expected_routes")
    @classmethod
    def _validate_expected_routes(cls, mapping: dict[str, RouteTrack]) -> dict[str, RouteTrack]:
        cleaned: dict[str, RouteTrack] = {}
        for req_id, track in mapping.items():
            cleaned[_require_non_empty(req_id, "route key")] = track
        return cleaned


class CrossDomainSuite(BaseModel):
    """Validated 16-case cross-domain socio-technical benchmark suite."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(..., min_length=1)
    cases: list[CrossDomainCase]

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        return _require_non_empty(value, "version")

    @model_validator(mode="after")
    def _validate_suite_balance(self) -> CrossDomainSuite:
        if len(self.cases) != 16:
            raise ValueError(f"CrossDomainSuite must contain exactly 16 cases, got {len(self.cases)}")

        case_ids = [c.case_id for c in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("CrossDomainSuite case_id values must be unique")

        one_liners = sum(1 for c in self.cases if c.input_kind == "one_liner")
        multi_para = sum(1 for c in self.cases if c.input_kind == "multi_paragraph")
        if one_liners != 8 or multi_para != 8:
            raise ValueError(
                f"CrossDomainSuite must have exactly 8 one_liner and 8 multi_paragraph cases "
                f"(got one_liner={one_liners}, multi_paragraph={multi_para})"
            )

        domain_counts = {d: 0 for d in EXPECTED_DOMAINS}
        for case in self.cases:
            domain_counts[case.domain] += 1
        bad_domains = {d: cnt for d, cnt in domain_counts.items() if cnt != 4}
        if bad_domains:
            raise ValueError(
                f"CrossDomainSuite must have exactly 4 cases per domain; imbalanced={bad_domains}"
            )
        return self


class AnnotationItem(BaseModel):
    """Single annotator label record used for inter-rater adjudication and Cohen's kappa."""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    skipped: bool = False
    verified: bool = True

    @field_validator("item_id", "label")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        return _require_non_empty(value)


class KappaReport(BaseModel):
    """Deterministic inter-rater reliability report produced by adjudicate_annotations."""

    model_config = ConfigDict(extra="forbid")

    kappa: float = Field(..., ge=-1.0, le=1.0)
    observed_agreement: float = Field(..., ge=0.0, le=1.0)
    expected_agreement: float = Field(..., ge=0.0, le=1.0)
    shared_count: int = Field(..., ge=0)
    agreed_count: int = Field(..., ge=0)
    disagreed_ids: list[str] = Field(default_factory=list)


class FrozenGoldDocument(BaseModel):
    """Read-only verified gold document loaded from data/frozen/gold/<split>/."""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(..., min_length=1)
    split: Literal["dev", "test"]
    prd_path: str = Field(..., min_length=1)
    source_sha256: str = Field(..., min_length=_SHA256_LEN, max_length=_SHA256_LEN)
    status: str = Field(..., min_length=1)
    verified_by: list[str] = Field(..., min_length=1)
    implied_categories: list[str] = Field(default_factory=list)
    requirements: list[dict[str, str]] = Field(..., min_length=1)

    @field_validator("item_id", "prd_path", "status")
    @classmethod
    def _validate_required_str(cls, value: str) -> str:
        return _require_non_empty(value)

    @field_validator("source_sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != _SHA256_LEN or any(c not in "0123456789abcdef" for c in normalized):
            raise ValueError("source_sha256 must be a 64-character hex SHA-256 digest")
        return normalized


__all__ = [
    "AnnotationItem",
    "CrossDomainCase",
    "CrossDomainSuite",
    "DomainName",
    "EXPECTED_DOMAINS",
    "FrozenGoldDocument",
    "GapCategory",
    "GapClause",
    "GapInjectionResult",
    "GoldGapManifest",
    "InputKind",
    "KappaReport",
    "OracleAnswer",
    "OracleEvalMetrics",
    "RouteTrack",
]
