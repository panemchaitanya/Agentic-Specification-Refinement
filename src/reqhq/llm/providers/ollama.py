"""Ollama local LLM provider adapter with JSON schema and GBNF grammar support."""

from __future__ import annotations

import time
from typing import Any

import httpx

from reqhq.llm.providers.base import (
    LLMRequest,
    LLMResponse,
    ProviderError,
    RateLimitError,
)


class OllamaLocalProvider:
    """Local Ollama adapter calling `/api/generate` with constrained decoding."""

    name: str = "ollama"
    is_local: bool = True

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        default_model: str = "qwen3:8b",
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.timeout = timeout
        self._client = client

    def build_payload(self, request: LLMRequest) -> dict[str, Any]:
        """Construct the Ollama `/api/generate` JSON payload."""
        model = request.model or self.default_model
        payload: dict[str, Any] = {
            "model": model,
            "prompt": request.prompt,
            "stream": False,
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_tokens,
            },
        }
        if request.system_prompt:
            payload["system"] = request.system_prompt
        if request.gbnf_grammar:
            payload["grammar"] = request.gbnf_grammar
        else:
            schema = request.resolved_json_schema()
            if schema is not None:
                payload["format"] = schema
        think_override = request.metadata.get("think")
        if isinstance(think_override, bool):
            payload["think"] = think_override
        elif "grammar" in payload or "format" in payload:
            # Reasoning models (e.g., qwen3:4b/8b/14b/32b) otherwise exhaust
            # num_predict on internal thinking tokens and return empty `response`
            # under constrained JSON/GBNF decoding (DEF-P4B-003).
            payload["think"] = False
        return payload

    def generate(self, request: LLMRequest) -> LLMResponse:
        """Send a generation request to the local Ollama server."""
        payload = self.build_payload(request)
        url = f"{self.base_url}/api/generate"
        start = time.perf_counter()

        try:
            if self._client is not None:
                resp = self._client.post(url, json=payload)
            else:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Ollama request failed: {exc}",
                provider=self.name,
            ) from exc

        latency_ms = (time.perf_counter() - start) * 1000.0

        if resp.status_code == 429:
            raise RateLimitError(
                f"Ollama returned HTTP 429: {resp.text}",
                provider=self.name,
            )
        if resp.status_code >= 400:
            raise ProviderError(
                f"Ollama returned HTTP {resp.status_code}: {resp.text}",
                provider=self.name,
                status_code=resp.status_code,
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(
                f"Ollama returned invalid JSON: {exc}",
                provider=self.name,
                status_code=resp.status_code,
            ) from exc

        text = str(data.get("response", ""))
        prompt_tokens = int(data.get("prompt_eval_count", 0) or 0)
        completion_tokens = int(data.get("eval_count", 0) or 0)
        total_tokens = prompt_tokens + completion_tokens
        model_used = str(data.get("model", payload["model"]))

        return LLMResponse(
            text=text,
            provider=self.name,
            model=model_used,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=round(latency_ms, 2),
            cloud_verified=False,
            cached=False,
            escalated=False,
            raw_metadata={"done_reason": data.get("done_reason")},
        )
