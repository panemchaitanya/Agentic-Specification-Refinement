"""Local-first LLM routing, quota management, PII scrubbing, and provider adapters."""

from __future__ import annotations

from reqhq.llm.pii_scrubber import PIIScrubber
from reqhq.llm.providers import (
    GeminiFreeProvider,
    GroqFreeProvider,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    MockProvider,
    OllamaLocalProvider,
    ProviderError,
    RateLimitError,
)
from reqhq.llm.quota_ledger import (
    DEFAULT_PROVIDER_LIMITS,
    ProviderQuotaLimits,
    QuotaLedger,
)
from reqhq.llm.router import EscalationReason, ProviderRouter

__all__ = [
    "DEFAULT_PROVIDER_LIMITS",
    "EscalationReason",
    "GeminiFreeProvider",
    "GroqFreeProvider",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "MockProvider",
    "OllamaLocalProvider",
    "PIIScrubber",
    "ProviderError",
    "ProviderQuotaLimits",
    "ProviderRouter",
    "QuotaLedger",
    "RateLimitError",
]
