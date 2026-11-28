"""Groq free-tier cloud provider adapter (OpenAI-compatible `/chat/completions`)."""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from reqhq.llm.providers.base import (
    LLMRequest,
    LLMResponse,
    ProviderError,
    RateLimitError,
)


def _parse_retry_after(headers: httpx.Headers) -> float | None:
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        value = float(raw.strip())
        return value if value > 0 else None
    except ValueError:
        return None


class GroqFreeProvider:
    """Cloud escalation adapter for Groq's OpenAI-compatible chat completions API."""

    name: str = "groq"
    is_local: bool = False

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str = "openai/gpt-oss-20b",
        base_url: str = "https://api.groq.com/openai/v1",
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self.default_model = default_model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = client

    def _resolve_api_key(self) -> str:
        key = self._api_key or os.environ.get("GROQ_API_KEY", "")
        if not key:
            raise ProviderError(
                "GROQ_API_KEY is not configured",
                provider=self.name,
            )
        return key

    def build_payload(self, request: LLMRequest) -> dict[str, Any]:
        """Construct the Groq OpenAI-compatible `/chat/completions` payload."""
        model = request.model or self.default_model
        messages: list[dict[str, str]] = []
        schema = request.resolved_json_schema()

        system_text = request.system_prompt
        if schema is not None:
            schema_hint = (
                "Respond ONLY with valid JSON conforming to this schema: "
                + json.dumps(schema, sort_keys=True)
            )
            system_text = f"{system_text}\n\n{schema_hint}".strip()

        if system_text:
            messages.append({"role": "system", "content": system_text})
        messages.append({"role": "user", "content": request.prompt})

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if schema is not None:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def generate(self, request: LLMRequest) -> LLMResponse:
        """Execute a Groq cloud request, raising RateLimitError on HTTP 429."""
        api_key = self._resolve_api_key()
        url = f"{self.base_url}/chat/completions"
        payload = self.build_payload(request)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        start = time.perf_counter()

        try:
            if self._client is not None:
                resp = self._client.post(url, json=payload, headers=headers)
            else:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Groq request failed: {exc}",
                provider=self.name,
            ) from exc

        latency_ms = (time.perf_counter() - start) * 1000.0

        if resp.status_code == 429:
            raise RateLimitError(
                f"Groq rate limited (HTTP 429): {resp.text}",
                provider=self.name,
                retry_after=_parse_retry_after(resp.headers),
            )
        if resp.status_code >= 400:
            raise ProviderError(
                f"Groq returned HTTP {resp.status_code}: {resp.text}",
                provider=self.name,
                status_code=resp.status_code,
            )

        try:
            data = resp.json()
            choice = data["choices"][0]
            raw_content = choice["message"]["content"]
            if not isinstance(raw_content, str):
                raise TypeError("Groq message content must be a string")
            text = raw_content
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"Groq returned unexpected response payload: {exc}",
                provider=self.name,
                status_code=resp.status_code,
            ) from exc

        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        total_tokens = int(
            usage.get("total_tokens", prompt_tokens + completion_tokens) or 0
        )

        return LLMResponse(
            text=text,
            provider=self.name,
            model=str(data.get("model", payload["model"])),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=round(latency_ms, 2),
            cloud_verified=True,
            cached=False,
            escalated=True,
            raw_metadata={"finish_reason": choice.get("finish_reason")},
        )
