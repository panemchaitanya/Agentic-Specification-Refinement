"""Google Gemini free-tier cloud provider adapter."""

from __future__ import annotations

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


class GeminiFreeProvider:
    """Cloud escalation adapter for Gemini's free-tier `generateContent` endpoint."""

    name: str = "gemini"
    is_local: bool = False

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str = "gemini-2.0-flash",
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self.default_model = default_model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = client

    def _resolve_api_key(self) -> str:
        key = self._api_key or os.environ.get("GEMINI_API_KEY", "")
        if not key:
            raise ProviderError(
                "GEMINI_API_KEY is not configured",
                provider=self.name,
            )
        return key

    def build_payload(self, request: LLMRequest) -> dict[str, Any]:
        """Construct the Gemini `generateContent` payload."""
        generation_config: dict[str, Any] = {
            "temperature": request.temperature,
            "maxOutputTokens": request.max_tokens,
        }
        schema = request.resolved_json_schema()
        if schema is not None:
            generation_config["responseMimeType"] = "application/json"
            generation_config["responseSchema"] = schema

        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": request.prompt}]}],
            "generationConfig": generation_config,
        }
        if request.system_prompt:
            payload["systemInstruction"] = {
                "parts": [{"text": request.system_prompt}]
            }
        return payload

    def generate(self, request: LLMRequest) -> LLMResponse:
        """Execute a Gemini cloud request, raising RateLimitError on HTTP 429."""
        api_key = self._resolve_api_key()
        model = request.model or self.default_model
        url = f"{self.base_url}/models/{model}:generateContent"
        payload = self.build_payload(request)
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
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
                f"Gemini request failed: {exc}",
                provider=self.name,
            ) from exc

        latency_ms = (time.perf_counter() - start) * 1000.0

        if resp.status_code == 429:
            raise RateLimitError(
                f"Gemini rate limited (HTTP 429): {resp.text}",
                provider=self.name,
                retry_after=_parse_retry_after(resp.headers),
            )
        if resp.status_code >= 400:
            raise ProviderError(
                f"Gemini returned HTTP {resp.status_code}: {resp.text}",
                provider=self.name,
                status_code=resp.status_code,
            )

        try:
            data = resp.json()
            candidates = data.get("candidates") or []
            parts = candidates[0]["content"]["parts"]
            text = "".join(str(p.get("text", "")) for p in parts)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"Gemini returned unexpected response payload: {exc}",
                provider=self.name,
                status_code=resp.status_code,
            ) from exc

        usage = data.get("usageMetadata") or {}
        prompt_tokens = int(usage.get("promptTokenCount", 0) or 0)
        completion_tokens = int(usage.get("candidatesTokenCount", 0) or 0)
        total_tokens = int(
            usage.get("totalTokenCount", prompt_tokens + completion_tokens) or 0
        )

        return LLMResponse(
            text=text,
            provider=self.name,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=round(latency_ms, 2),
            cloud_verified=True,
            cached=False,
            escalated=True,
            raw_metadata={"finish_reason": candidates[0].get("finishReason")},
        )
