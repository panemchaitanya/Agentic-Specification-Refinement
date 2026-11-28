"""Strict Pydantic models and canonical specs for reqhq credential management."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class CredentialSpec(BaseModel):
    """Metadata describing a supported reqhq credential."""

    model_config = ConfigDict(extra="forbid")

    key_name: str
    description: str
    required_for_cloud: bool = False
    optional_under_strict_local: bool = True
    min_length: int = Field(default=1, ge=1)


CREDENTIAL_SPECS: dict[str, CredentialSpec] = {
    "GEMINI_API_KEY": CredentialSpec(
        key_name="GEMINI_API_KEY",
        description="Google Gemini API key for cloud LLM escalation (optional under --strict-local)",
        required_for_cloud=True,
        optional_under_strict_local=True,
        min_length=8,
    ),
    "GROQ_API_KEY": CredentialSpec(
        key_name="GROQ_API_KEY",
        description="Groq API key for cloud LLM failover (optional under --strict-local)",
        required_for_cloud=True,
        optional_under_strict_local=True,
        min_length=8,
    ),
    "GOOGLE_OAUTH_CLIENT_SECRETS": CredentialSpec(
        key_name="GOOGLE_OAUTH_CLIENT_SECRETS",
        description="Optional path to Google OAuth2 desktop client secrets JSON for Drive connector",
        required_for_cloud=False,
        optional_under_strict_local=True,
        min_length=1,
    ),
    "MSAL_CLIENT_ID": CredentialSpec(
        key_name="MSAL_CLIENT_ID",
        description="Optional Microsoft Entra ID application client ID for SharePoint connector",
        required_for_cloud=False,
        optional_under_strict_local=True,
        min_length=1,
    ),
    "MSAL_TENANT_ID": CredentialSpec(
        key_name="MSAL_TENANT_ID",
        description="Optional Microsoft Entra ID tenant ID for SharePoint connector",
        required_for_cloud=False,
        optional_under_strict_local=True,
        min_length=1,
    ),
}


class CredentialSource(str, Enum):
    """Origin or verification state of a resolved credential."""

    ENV = "env"
    VAULT = "vault"
    PROMPTED = "prompted"
    MISSING = "missing"
    INVALID = "invalid"


class CredentialStatusItem(BaseModel):
    """Per-key credential readiness status (never contains raw secret values)."""

    model_config = ConfigDict(extra="forbid")

    key_name: str
    configured: bool
    valid: bool
    source: CredentialSource
    masked_value: str = ""
    message: str = ""


class CredentialCheckReport(BaseModel):
    """Aggregate report returned by credential readiness checks and provisioning."""

    model_config = ConfigDict(extra="forbid")

    vault_path: Path
    protector_backend: str
    strict_local: bool
    items: list[CredentialStatusItem]
    all_required_ready: bool
    prompted_keys: list[str] = Field(default_factory=list)


__all__ = [
    "CREDENTIAL_SPECS",
    "CredentialCheckReport",
    "CredentialSource",
    "CredentialSpec",
    "CredentialStatusItem",
]
