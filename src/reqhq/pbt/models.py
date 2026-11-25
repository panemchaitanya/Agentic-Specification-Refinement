"""Strict Pydantic v2 data models for Phase 3 PBT contract emission and counterexample feedback."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reqhq.schemas.pbt import HypothesisStrategySpec, PBTSpec

_SHA256_LEN = 64


def _validate_sha256(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != _SHA256_LEN or any(c not in "0123456789abcdef" for c in normalized):
        raise ValueError("source_sha256 must be a 64-character hex SHA-256 digest")
    return normalized


def _validate_non_blank(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Field must not be blank or whitespace-only")
    return cleaned


class PropertyKind(str, Enum):
    """Classification of an executable property-based test contract."""

    INVARIANT = "invariant"
    POSTCONDITION = "postcondition"
    IDEMPOTENCY = "idempotency"
    ROUNDTRIP = "roundtrip"
    STATEFUL_TRANSITION = "stateful_transition"


class GuardrailEnforcement(str, Enum):
    """Dual-layer enforcement mode for a PBT property contract."""

    RUNTIME_ASSERT = "runtime_assert"
    PBT_ONLY = "pbt_only"
    BOTH = "both"


class CounterexampleClassification(str, Enum):
    """Root-cause classification for a shrunk Hypothesis counterexample."""

    CODE_DEFECT = "code_defect"
    SPEC_AMBIGUITY = "spec_ambiguity"
    STRATEGY_MISMATCH = "strategy_mismatch"


class PBTPropertyContract(BaseModel):
    """Single traceable PBT property contract linking source, requirement, ADR, Alloy, and Python."""

    model_config = ConfigDict(extra="forbid")

    property_id: str = Field(..., min_length=1)
    req_id: str = Field(..., min_length=1)
    adr_id: str = Field(..., min_length=1)
    source_sha256: str = Field(..., min_length=_SHA256_LEN, max_length=_SHA256_LEN)
    alloy_ref: str = Field(..., min_length=1)
    target_symbol: str = Field(..., min_length=1)
    kind: PropertyKind
    title: str = Field(..., min_length=1)
    precondition: str = ""
    assertion_expr: str = Field(..., min_length=1)
    input_strategies: list[HypothesisStrategySpec] = Field(default_factory=list)
    guardrail_enforcement: GuardrailEnforcement = GuardrailEnforcement.BOTH
    max_examples: int = Field(default=200, ge=200, le=10000)

    @field_validator(
        "property_id",
        "req_id",
        "adr_id",
        "alloy_ref",
        "target_symbol",
        "title",
        "assertion_expr",
    )
    @classmethod
    def _strip_required_fields(cls, value: str) -> str:
        return _validate_non_blank(value)

    @field_validator("precondition")
    @classmethod
    def _strip_precondition(cls, value: str) -> str:
        return value.strip()

    @field_validator("source_sha256")
    @classmethod
    def _check_sha256(cls, value: str) -> str:
        return _validate_sha256(value)

    @model_validator(mode="after")
    def _validate_unique_strategy_names(self) -> PBTPropertyContract:
        seen: set[str] = set()
        for strategy in self.input_strategies:
            if strategy.parameter_name in seen:
                raise ValueError(
                    f"Duplicate strategy parameter_name {strategy.parameter_name!r} "
                    f"in property {self.property_id!r}"
                )
            seen.add(strategy.parameter_name)
        return self


class PBTContractDocument(BaseModel):
    """Top-level executable PBT contract document backed by a validated Phase 1 PBTSpec."""

    model_config = ConfigDict(extra="forbid")

    spec: PBTSpec
    session_id: str = Field(..., min_length=1)
    source_sha256: str = Field(..., min_length=_SHA256_LEN, max_length=_SHA256_LEN)
    properties: list[PBTPropertyContract] = Field(default_factory=list)
    seed: int = 42

    @field_validator("session_id")
    @classmethod
    def _strip_session_id(cls, value: str) -> str:
        return _validate_non_blank(value)

    @field_validator("source_sha256")
    @classmethod
    def _check_doc_sha256(cls, value: str) -> str:
        return _validate_sha256(value)

    @model_validator(mode="after")
    def _validate_properties_integrity(self) -> PBTContractDocument:
        seen_ids: set[str] = set()
        req_set = set(self.spec.req_ids)
        for prop in self.properties:
            if prop.property_id in seen_ids:
                raise ValueError(f"Duplicate property_id: {prop.property_id!r}")
            seen_ids.add(prop.property_id)
            if prop.req_id not in req_set:
                raise ValueError(
                    f"Property {prop.property_id!r} references req_id {prop.req_id!r} "
                    "not present in spec.req_ids"
                )
        return self

    def to_pbt_refs_by_req(self) -> dict[str, list[str]]:
        """Map each requirement ID to its ordered, deduplicated list of PBT property IDs."""
        refs: dict[str, list[str]] = {}
        for prop in self.properties:
            bucket = refs.setdefault(prop.req_id, [])
            if prop.property_id not in bucket:
                bucket.append(prop.property_id)
        return dict(sorted(refs.items()))


class GeneratedGuardrailBundle(BaseModel):
    """File manifest for an exported PBT contract, guardrail, test, and lock bundle."""

    model_config = ConfigDict(extra="forbid")

    out_dir: Path
    pbt_spec_yaml: Path
    guardrail_files: list[Path] = Field(default_factory=list)
    test_files: list[Path] = Field(default_factory=list)
    lock_file: Path


class CounterexampleRepairReport(BaseModel):
    """Structured repair diagnostic parsed from a shrunk Hypothesis failure."""

    model_config = ConfigDict(extra="forbid")

    test_name: str = Field(..., min_length=1)
    property_id: str | None = None
    req_id: str | None = None
    falsifying_example: str = Field(..., min_length=1)
    assertion_error: str = Field(..., min_length=1)
    classification: CounterexampleClassification
    suggested_action: str = Field(..., min_length=1)

    @field_validator("test_name", "falsifying_example", "assertion_error", "suggested_action")
    @classmethod
    def _strip_report_fields(cls, value: str) -> str:
        return _validate_non_blank(value)


__all__ = [
    "CounterexampleClassification",
    "CounterexampleRepairReport",
    "GeneratedGuardrailBundle",
    "GuardrailEnforcement",
    "PBTContractDocument",
    "PBTPropertyContract",
    "PropertyKind",
]
