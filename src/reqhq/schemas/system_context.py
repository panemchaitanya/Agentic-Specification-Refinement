"""Abstract SystemContext contract for Phase 1 greenfield & Phase 2 brownfield readiness."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SHA256_LEN = 64


@runtime_checkable
class SystemContext(Protocol):
    """Protocol satisfied by Phase 1 greenfield and Phase 2 brownfield contexts."""

    @property
    def mode(self) -> str:
        """Context mode identifier (e.g. 'greenfield' or 'brownfield')."""
        ...

    def is_empty(self) -> bool:
        """Return True when no existing system/codebase artifacts are loaded."""
        ...

    def to_prompt_context(self) -> str:
        """Render a concise deterministic context block for LLM prompts."""
        ...

    def summary_metadata(self) -> dict[str, Any]:
        """Return structured metadata safe for event-log persistence."""
        ...


class BrownfieldArtifactRef(BaseModel):
    """Phase 2 extension descriptor referencing an existing system artifact."""

    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal[
        "architecture_md",
        "ast_summary",
        "alloy_model",
        "superseded_adr",
    ]
    path: str = Field(..., min_length=1)
    sha256: str | None = None
    summary: str = ""

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("path must not be blank or whitespace-only")
        return cleaned

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if len(normalized) != _SHA256_LEN or any(c not in "0123456789abcdef" for c in normalized):
            raise ValueError("sha256 must be a 64-character hex SHA-256 digest")
        return normalized


class SystemContextBase(BaseModel, ABC):
    """Pydantic ABC providing shared validation and Phase 2 brownfield hook declarations."""

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def brownfield_extension_points(cls) -> tuple[str, ...]:
        """Named Phase 2 hooks where brownfield loaders plug in without breaking Phase 1."""
        return (
            "architecture_md",
            "ast_summary",
            "legacy_alloy_models",
            "superseded_adrs",
        )

    @abstractmethod
    def is_empty(self) -> bool:
        """Return True if no existing system artifacts are attached."""

    @abstractmethod
    def to_prompt_context(self) -> str:
        """Render prompt-ready system context text."""

    @abstractmethod
    def summary_metadata(self) -> dict[str, Any]:
        """Return JSON-serializable metadata describing this context."""


class GreenfieldEmptyContext(SystemContextBase):
    """Phase 1 default empty SystemContext for greenfield specifications."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["greenfield"] = "greenfield"
    project_name: str = ""
    notes: str = ""

    def is_empty(self) -> bool:
        """Greenfield context always reports empty legacy system state."""
        return True

    def to_prompt_context(self) -> str:
        """Render deterministic greenfield prompt preamble."""
        parts = [
            "[SystemContext: greenfield / empty — no existing codebase, architecture, or legacy constraints]"
        ]
        if self.project_name.strip():
            parts.append(f"Project: {self.project_name.strip()}")
        if self.notes.strip():
            parts.append(f"Notes: {self.notes.strip()}")
        return "\n".join(parts)

    def summary_metadata(self) -> dict[str, Any]:
        """Return event-log metadata for greenfield initialization."""
        return {
            "mode": self.mode,
            "is_empty": True,
            "project_name": self.project_name,
            "brownfield_artifacts": 0,
            "phase2_hooks": list(self.brownfield_extension_points()),
        }


__all__ = [
    "BrownfieldArtifactRef",
    "GreenfieldEmptyContext",
    "SystemContext",
    "SystemContextBase",
]
