"""Strict Pydantic v2 models for Stage 4a/5a socio-technical synthesis."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reqhq.schemas.routing_adr import RoutingADR, RoutingDecisionKind

_SHA256_LEN = 64


class TriageTrack(str, Enum):
    """Socio-technical solution routing track for an active requirement."""

    SOFTWARE = "software"
    PROCESS = "process"
    TRAINING = "training"
    HYBRID = "hybrid"

    def to_routing_kind(self) -> RoutingDecisionKind:
        """Convert this lowercase triage track to the Phase 1 RoutingDecisionKind."""
        mapping: dict[TriageTrack, RoutingDecisionKind] = {
            TriageTrack.SOFTWARE: RoutingDecisionKind.SOFTWARE,
            TriageTrack.PROCESS: RoutingDecisionKind.PROCESS,
            TriageTrack.TRAINING: RoutingDecisionKind.TRAINING,
            TriageTrack.HYBRID: RoutingDecisionKind.HYBRID,
        }
        return mapping[self]

    @classmethod
    def from_routing_kind(cls, kind: RoutingDecisionKind) -> TriageTrack:
        """Convert a Phase 1 RoutingDecisionKind to a TriageTrack."""
        mapping: dict[RoutingDecisionKind, TriageTrack] = {
            RoutingDecisionKind.SOFTWARE: cls.SOFTWARE,
            RoutingDecisionKind.PROCESS: cls.PROCESS,
            RoutingDecisionKind.TRAINING: cls.TRAINING,
            RoutingDecisionKind.HYBRID: cls.HYBRID,
        }
        return mapping[kind]


@runtime_checkable
class AssumptionLike(Protocol):
    """Structural protocol compatible with reqhq.elicitation.models.AssumptionRecord."""

    assumption_id: str
    title: str
    statement: str
    req_ids: list[str]
    finding_ids: list[str]
    source_sha256: str
    rationale: str
    type: str
    user_confirmed: bool


def _coerce_routing_kind_key(raw_key: Any) -> RoutingDecisionKind:
    if isinstance(raw_key, RoutingDecisionKind):
        return raw_key
    if isinstance(raw_key, TriageTrack):
        return raw_key.to_routing_kind()
    if isinstance(raw_key, str):
        normalized = raw_key.strip().lower()
        lookup = {
            "software": RoutingDecisionKind.SOFTWARE,
            "process": RoutingDecisionKind.PROCESS,
            "training": RoutingDecisionKind.TRAINING,
            "hybrid": RoutingDecisionKind.HYBRID,
        }
        if normalized in lookup:
            return lookup[normalized]
    raise ValueError(f"Unsupported component_breakdown key: {raw_key!r}")


class TriageItem(BaseModel):
    """Socio-technical triage classification and metadata for one requirement."""

    model_config = ConfigDict(extra="forbid")

    req_id: str = Field(..., min_length=1)
    track: TriageTrack
    rationale: str = Field(..., min_length=1)
    rejected_alternatives: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    component_breakdown: dict[RoutingDecisionKind, str] = Field(default_factory=dict)
    human_roles: list[str] = Field(default_factory=list)
    software_module: str = ""
    cloud_verified: bool = False

    @field_validator("req_id", "rationale")
    @classmethod
    def _strip_required_str(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be blank or whitespace-only")
        return cleaned

    @field_validator("software_module")
    @classmethod
    def _strip_software_module(cls, value: str) -> str:
        return value.strip()

    @field_validator("rejected_alternatives", "prerequisites", "human_roles")
    @classmethod
    def _clean_string_list(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in values:
            stripped = item.strip()
            if stripped:
                cleaned.append(stripped)
        return cleaned

    @field_validator("component_breakdown", mode="before")
    @classmethod
    def _normalize_component_breakdown(
        cls, value: Any
    ) -> dict[RoutingDecisionKind, str]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("component_breakdown must be a mapping")
        normalized: dict[RoutingDecisionKind, str] = {}
        for key, desc in value.items():
            kind = _coerce_routing_kind_key(key)
            cleaned_desc = str(desc).strip()
            if not cleaned_desc:
                raise ValueError(
                    f"component_breakdown description for {kind.value!r} must not be blank"
                )
            normalized[kind] = cleaned_desc
        return normalized

    @model_validator(mode="after")
    def _validate_breakdown_consistency(self) -> TriageItem:
        if RoutingDecisionKind.HYBRID in self.component_breakdown:
            raise ValueError(
                "component_breakdown must map concrete tracks (Process/Software/Training), not Hybrid"
            )
        if self.track == TriageTrack.HYBRID and self.component_breakdown:
            if len(self.component_breakdown) < 2:
                raise ValueError(
                    "Hybrid component_breakdown must specify at least 2 distinct concrete tracks"
                )
        return self


class TriagePlan(BaseModel):
    """Complete socio-technical triage plan for a ReqHQ synthesis session."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=1)
    items: list[TriageItem] = Field(default_factory=list)

    @field_validator("session_id")
    @classmethod
    def _strip_session_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("session_id must not be blank")
        return cleaned

    @model_validator(mode="after")
    def _validate_unique_req_ids(self) -> TriagePlan:
        seen: set[str] = set()
        for item in self.items:
            if item.req_id in seen:
                raise ValueError(f"Duplicate req_id in TriagePlan: {item.req_id!r}")
            seen.add(item.req_id)
        return self

    def by_track(self, track: TriageTrack) -> list[TriageItem]:
        """Return all TriageItems assigned to the specified track."""
        return [item for item in self.items if item.track == track]

    def for_req(self, req_id: str) -> TriageItem | None:
        """Return the TriageItem for req_id, or None if not present."""
        for item in self.items:
            if item.req_id == req_id:
                return item
        return None


class EmittedADRFile(BaseModel):
    """Rendered target-system Routing or Assumption ADR file with parsed frontmatter."""

    model_config = ConfigDict(extra="forbid")

    adr_id: str = Field(..., min_length=1)
    kind: Literal["routing", "assumption"]
    filename: str = Field(..., min_length=1)
    path: Path | None = None
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    markdown: str = Field(..., min_length=1)
    routing_adr: RoutingADR | None = None

    @field_validator("adr_id", "filename", "markdown")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be blank")
        return value if value.endswith("\n") else cleaned


class TraceabilityRow(BaseModel):
    """Single row of the end-to-end requirement traceability matrix."""

    model_config = ConfigDict(extra="forbid")

    req_id: str = Field(..., min_length=1)
    source_sha256: str = Field(..., min_length=_SHA256_LEN, max_length=_SHA256_LEN)
    span: str = ""
    track: str = Field(..., min_length=1)
    adr_ids: list[str] = Field(default_factory=list)
    alloy_refs: list[str] = Field(default_factory=list)
    pbt_refs: list[str] = Field(default_factory=list)
    user_confirmed: bool = True

    @field_validator("source_sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != _SHA256_LEN or any(
            c not in "0123456789abcdef" for c in normalized
        ):
            raise ValueError("source_sha256 must be a 64-character hex SHA-256 digest")
        return normalized

    @field_validator("req_id", "track")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be blank")
        return cleaned


class TraceabilityMatrix(BaseModel):
    """End-to-end traceability report with explicit orphaned requirement tracking."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=1)
    rows: list[TraceabilityRow] = Field(default_factory=list)
    orphaned_req_ids: list[str] = Field(default_factory=list)

    @field_validator("session_id")
    @classmethod
    def _strip_session_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("session_id must not be blank")
        return cleaned

    def is_complete(self) -> bool:
        """Return True iff every active requirement is traced with zero orphans."""
        return len(self.orphaned_req_ids) == 0


class SynthesisBundleResult(BaseModel):
    """Paths and traceability status for a written Phase 2 synthesis export bundle."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=1)
    out_dir: Path
    adr_files: list[Path] = Field(default_factory=list)
    design_md: Path
    process_training_md: Path
    traceability_matrix_json: Path
    spec_md: Path | None = None
    orphaned_req_ids: list[str] = Field(default_factory=list)

    @field_validator("session_id")
    @classmethod
    def _strip_session_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("session_id must not be blank")
        return cleaned


__all__ = [
    "AssumptionLike",
    "EmittedADRFile",
    "SynthesisBundleResult",
    "TraceabilityMatrix",
    "TraceabilityRow",
    "TriageItem",
    "TriagePlan",
    "TriageTrack",
]
