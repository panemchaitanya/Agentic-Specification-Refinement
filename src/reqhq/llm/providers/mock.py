"""Deterministic offline mock provider for testing retries, failover, and PII gating."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from reqhq.llm.providers.base import (
    LLMRequest,
    LLMResponse,
    RateLimitError,
)

ScriptedItem = str | Exception | Callable[[LLMRequest], str | LLMResponse]


class MockProvider:
    """Offline mock LLM provider supporting scripted outputs and HTTP 429 failures."""

    def __init__(
        self,
        name: str = "mock-local",
        *,
        is_local: bool = True,
        default_model: str = "mock-model-v1",
        responses: Sequence[ScriptedItem] | None = None,
        default_response: str = '{"status": "ok"}',
        fail_with_429_count: int = 0,
        retry_after: float | None = 60.0,
    ) -> None:
        self.name = name
        self.is_local = is_local
        self.default_model = default_model
        self._responses: list[ScriptedItem] = list(responses or [])
        self._default_response = default_response
        self._fail_with_429_remaining = fail_with_429_count
        self._retry_after = retry_after
        self.calls: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        """Return the next scripted response or raise a scripted failure."""
        self.calls.append(request)

        if self._fail_with_429_remaining > 0:
            self._fail_with_429_remaining -= 1
            raise RateLimitError(
                f"Mock HTTP 429 rate limit on provider '{self.name}'",
                provider=self.name,
                retry_after=self._retry_after,
            )

        if self._responses:
            item = self._responses.pop(0)
        else:
            item = self._default_response

        if isinstance(item, Exception):
            raise item

        if callable(item):
            out = item(request)
            if isinstance(out, LLMResponse):
                return out
            text = str(out)
        else:
            text = str(item)

        prompt_tokens = max(1, (len(request.prompt) + len(request.system_prompt)) // 4)
        completion_tokens = max(1, len(text) // 4)
        return LLMResponse(
            text=text,
            provider=self.name,
            model=request.model or self.default_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            latency_ms=1.0,
            cloud_verified=not self.is_local,
            cached=False,
            escalated=not self.is_local,
        )
