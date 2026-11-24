"""Base protocol, request/response schemas, and exceptions for LLM providers."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class ProviderError(RuntimeError):
    """Base error raised by an LLM provider adapter."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code


class RateLimitError(ProviderError):
    """Raised when a cloud provider returns HTTP 429 Too Many Requests."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        retry_after: float | None = None,
        status_code: int = 429,
    ) -> None:
        super().__init__(message, provider=provider, status_code=status_code)
        self.retry_after = retry_after


class LLMRequest(BaseModel):
    """Normalized request passed to local or cloud LLM providers."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    system_prompt: str = ""
    model: str | None = None
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, gt=0)
    response_schema: type[BaseModel] | None = None
    json_schema: dict[str, Any] | None = None
    gbnf_grammar: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def resolved_json_schema(self) -> dict[str, Any] | None:
        """Return explicit json_schema or derive it from response_schema."""
        if self.json_schema is not None:
            return self.json_schema
        if self.response_schema is not None:
            return self.response_schema.model_json_schema()
        return None


class LLMResponse(BaseModel):
    """Normalized response returned by providers and the ProviderRouter."""

    model_config = ConfigDict(extra="forbid")

    text: str
    provider: str
    model: str
    parsed: dict[str, Any] | None = None
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    latency_ms: float = Field(default=0.0, ge=0.0)
    cloud_verified: bool = False
    cached: bool = False
    escalated: bool = False
    escalation_reason: str | None = None
    pii_redactions: int = Field(default=0, ge=0)
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol implemented by all local and cloud LLM provider adapters."""

    name: str
    is_local: bool
    default_model: str

    def generate(self, request: LLMRequest) -> LLMResponse:
        """Execute an LLM generation request and return an LLMResponse."""
        ...
