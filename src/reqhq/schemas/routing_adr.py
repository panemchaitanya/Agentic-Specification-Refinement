"""Strict Pydantic v2 schemas for Routing ADRs and end-to-end traceability chains."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256_LEN = 64


class RoutingDecisionKind(str, Enum):
    """Solution routing classification for a set of requirements."""

    PROCESS = "Process"
    SOFTWARE = "Software"
    TRAINING = "Training"
    HYBRID = "Hybrid"


class TraceabilityChain(BaseModel):
    """Cryptographic chain: source_sha256 -> req_id -> adr_id -> alloy_model_ref -> pbt_spec_id."""

    model_config = ConfigDict(extra="forbid")

    source_sha256: str = Field(..., min_length=_SHA256_LEN, max_length=_SHA256_LEN)
    req_id: str = Field(..., min_length=1)
    adr_id: str = Field(..., min_length=1)
    alloy_model_ref: str | None = None
    pbt_spec_id: str | None = None

    @field_validator("source_sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != _SHA256_LEN or any(c not in "0123456789abcdef" for c in normalized):
            raise ValueError("source_sha256 must be a 64-character hex SHA-256 digest")
        return normalized

    @field_validator("req_id", "adr_id")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Identifier must not be blank")
        return cleaned

    @field_validator("alloy_model_ref", "pbt_spec_id")
    @classmethod
    def _normalize_optional_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    def is_complete(self) -> bool:
        """Return True when all 5 links in the chain are populated."""
        return bool(
            self.source_sha256
            and self.req_id
            and self.adr_id
            and self.alloy_model_ref
            and self.pbt_spec_id
        )

    def chain_summary(self) -> str:
        """Format a compact human-readable representation of the traceability chain."""
        alloy = self.alloy_model_ref or "<unlinked>"
        pbt = self.pbt_spec_id or "<unlinked>"
        return f"{self.source_sha256[:12]} -> {self.req_id} -> {self.adr_id} -> {alloy} -> {pbt}"


class RoutingADR(BaseModel):
    """Structured routing decision record linking requirements to solution tracks."""

    model_config = ConfigDict(extra="forbid")

    adr_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    decision: RoutingDecisionKind
    rationale: str = Field(..., min_length=1)
    req_ids: list[str] = Field(..., min_length=1)
    traceability: list[TraceabilityChain] = Field(default_factory=list)
    component_breakdown: dict[RoutingDecisionKind, str] = Field(default_factory=dict)
    risks: list[str] = Field(default_factory=list)
    consequences: list[str] = Field(default_factory=list)
    cloud_verified: bool = False

    @field_validator("adr_id", "title", "rationale")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be blank")
        return cleaned

    @field_validator("req_ids")
    @classmethod
    def _validate_req_ids(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in values:
            stripped = item.strip()
            if not stripped:
                raise ValueError("req_ids must not contain blank entries")
            cleaned.append(stripped)
        return cleaned

    @model_validator(mode="after")
    def _validate_routing_consistency(self) -> RoutingADR:
        if RoutingDecisionKind.HYBRID in self.component_breakdown:
            raise ValueError(
                "component_breakdown must map concrete tracks (Process/Software/Training), not Hybrid"
            )
        if self.decision == RoutingDecisionKind.HYBRID and self.component_breakdown:
            if len(self.component_breakdown) < 2:
                raise ValueError(
                    "Hybrid component_breakdown must specify at least 2 distinct tracks"
                )

        req_set = set(self.req_ids)
        for idx, link in enumerate(self.traceability):
            if link.adr_id != self.adr_id:
                raise ValueError(
                    f"traceability[{idx}].adr_id {link.adr_id!r} does not match RoutingADR.adr_id {self.adr_id!r}"
                )
            if link.req_id not in req_set:
                raise ValueError(
                    f"traceability[{idx}].req_id {link.req_id!r} is not listed in RoutingADR.req_ids"
                )
        return self


__all__ = [
    "RoutingADR",
    "RoutingDecisionKind",
    "TraceabilityChain",
]
