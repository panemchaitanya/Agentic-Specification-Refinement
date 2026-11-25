"""Strict Pydantic v2 schemas for Phase 4 report generation and release bundle packaging."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256_LEN = 64
_HEX_CHARS = frozenset("0123456789abcdef")

VerdictStatus = Literal["VERIFIED_LIVE", "DRY_RUN_UNSCORED"]


class ReportGenerationResult(BaseModel):
    """Output descriptor returned by generate_final_report."""

    model_config = ConfigDict(extra="forbid")

    out_dir: Path
    report_md: Path | None = None
    tables_tex: Path | None = None
    verdict_status: VerdictStatus
    quarantined: bool

    @model_validator(mode="after")
    def _enforce_quarantine_verdict(self) -> ReportGenerationResult:
        if self.quarantined and self.verdict_status == "VERIFIED_LIVE":
            raise ValueError("Quarantined report cannot carry VERIFIED_LIVE verdict_status")
        return self


class ReleaseArtifactEntry(BaseModel):
    """Single tracked file inside a tamper-evident release bundle."""

    model_config = ConfigDict(extra="forbid")

    rel_path: str = Field(..., min_length=1)
    sha256: str = Field(..., min_length=_SHA256_LEN, max_length=_SHA256_LEN)
    bytes: int = Field(..., ge=0)
    category: str = Field(..., min_length=1)

    @field_validator("rel_path", "category")
    @classmethod
    def _validate_non_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be blank or whitespace-only")
        return cleaned

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != _SHA256_LEN or any(ch not in _HEX_CHARS for ch in normalized):
            raise ValueError("sha256 must be a 64-character lowercase hex digest")
        return normalized


class ArtifactsManifest(BaseModel):
    """Top-level manifest pinning model registry, frozen dataset hashes, and artifact digests."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0.0"
    run_id: str = Field(..., min_length=1)
    run_mode: str = Field(..., min_length=1)
    verdict_status: VerdictStatus
    created_at: str = Field(..., min_length=1)
    seed: int
    pinned_models: dict[str, str]
    frozen_dataset_hashes: dict[str, str]
    artifacts: list[ReleaseArtifactEntry]

    @field_validator("run_id", "run_mode", "created_at", "schema_version")
    @classmethod
    def _validate_non_blank_strings(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("String field must not be blank or whitespace-only")
        return cleaned

    @model_validator(mode="after")
    def _enforce_live_verdict_invariant(self) -> ArtifactsManifest:
        if self.run_mode.lower() != "live" and self.verdict_status == "VERIFIED_LIVE":
            raise ValueError("Non-live run_mode cannot carry VERIFIED_LIVE verdict_status")
        return self


class BundleVerificationResult(BaseModel):
    """Outcome of verifying a release bundle against its artifacts_manifest.json."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    checked_count: int = Field(..., ge=0)
    missing_files: list[str] = Field(default_factory=list)
    mismatched_files: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


__all__ = [
    "ArtifactsManifest",
    "BundleVerificationResult",
    "ReleaseArtifactEntry",
    "ReportGenerationResult",
    "VerdictStatus",
]
