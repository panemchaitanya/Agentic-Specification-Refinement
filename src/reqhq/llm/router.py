"""Local-first LLM router with schema retries, PII scrubbing, quota gating, and 429 failover."""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum
import hashlib
import json
import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from reqhq.llm.pii_scrubber import PIIScrubber
from reqhq.llm.providers.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderError,
    RateLimitError,
)

if TYPE_CHECKING:
    from reqhq.llm.quota_ledger import QuotaLedger
    from reqhq.store.prompt_cache import PromptCache


_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$",
    re.DOTALL | re.IGNORECASE,
)


class EscalationReason(str, Enum):
    """Explicit, audited reasons permitted to escalate from local SLM to cloud free-tier."""

    CRITIC_DISAGREEMENT = "critic_disagreement"
    SCHEMA_RETRY_EXHAUSTED = "schema_retry_exhausted"
    ALLOY_REPAIR_EXHAUSTED = "alloy_repair_exhausted"
    FINAL_AUDIT = "final_audit"


def _extract_json_candidate(text: str) -> str:
    stripped = text.strip()
    match = _CODE_FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    return stripped


def _validate_against_schema(
    text: str,
    schema: type[BaseModel],
) -> tuple[bool, dict[str, Any] | None, str]:
    candidate = _extract_json_candidate(text)
    try:
        raw_obj = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return False, None, f"Invalid JSON: {exc}"

    try:
        model_obj = schema.model_validate(raw_obj)
    except ValidationError as exc:
        return False, None, f"Schema validation failed: {exc}"

    return True, model_obj.model_dump(mode="json"), ""


class ProviderRouter:
    """Local-first router with prompt caching, schema retries, PII gate, and quota failover."""

    def __init__(
        self,
        local_provider: LLMProvider,
        cloud_providers: Sequence[LLMProvider] | None = None,
        *,
        quota_ledger: QuotaLedger | None = None,
        pii_scrubber: PIIScrubber | None = None,
        prompt_cache: PromptCache | None = None,
        strict_local: bool = False,
        max_local_retries: int = 2,
        enable_cache: bool = True,
    ) -> None:
        if max_local_retries < 0:
            raise ValueError("max_local_retries must be non-negative")
        self.local_provider = local_provider
        self.cloud_providers: list[LLMProvider] = list(cloud_providers or [])
        self.quota_ledger = quota_ledger
        self.pii_scrubber = pii_scrubber or PIIScrubber()
        self.prompt_cache = prompt_cache
        self.strict_local = strict_local
        self.max_local_retries = max_local_retries
        self.enable_cache = enable_cache

    def _cache_key(
        self,
        request: LLMRequest,
        escalation_reason: EscalationReason | None,
    ) -> str | None:
        if not self.enable_cache or self.prompt_cache is None:
            return None
        schema_name: str | None
        if request.response_schema is not None:
            schema_name = request.response_schema.__name__
        elif request.json_schema is not None:
            schema_digest = hashlib.sha256(
                json.dumps(request.json_schema, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest()[:16]
            schema_name = f"json_schema:{schema_digest}"
        else:
            schema_name = None

        if request.gbnf_grammar:
            grammar_digest = hashlib.sha256(
                request.gbnf_grammar.encode("utf-8")
            ).hexdigest()[:16]
            schema_name = (
                f"{schema_name}|gbnf:{grammar_digest}"
                if schema_name is not None
                else f"gbnf:{grammar_digest}"
            )

        mode_tag = (
            "strict_local"
            if self.strict_local
            else (escalation_reason.value if escalation_reason is not None else "local")
        )
        model_key = f"{request.model or self.local_provider.default_model}|{mode_tag}"
        return self.prompt_cache.compute_key(
            prompt=request.prompt,
            model=model_key,
            schema_name=schema_name,
            system_prompt=request.system_prompt,
        )

    def _store_cache(self, cache_key: str | None, response: LLMResponse) -> None:
        if cache_key is None or self.prompt_cache is None:
            return
        self.prompt_cache.put(
            cache_key,
            response.model_dump(mode="json"),
            model=response.model,
            metadata={
                "provider": response.provider,
                "cloud_verified": response.cloud_verified,
                "escalated": response.escalated,
            },
        )

    def _run_local_with_retries(
        self,
        request: LLMRequest,
    ) -> tuple[LLMResponse | None, bool, str | None]:
        """Run local provider with up to `max_local_retries` schema-repair attempts."""
        last_response: LLMResponse | None = None
        last_error: str | None = None
        current_prompt = request.prompt
        attempts = (1 + self.max_local_retries) if request.response_schema is not None else 1

        for attempt in range(attempts):
            attempt_req = (
                request
                if attempt == 0
                else request.model_copy(update={"prompt": current_prompt})
            )
            try:
                resp = self.local_provider.generate(attempt_req)
            except ProviderError as exc:
                last_error = str(exc)
                break

            resp = resp.model_copy(
                update={
                    "cloud_verified": False,
                    "escalated": False,
                    "cached": False,
                }
            )
            last_response = resp

            if request.response_schema is None:
                return resp, True, None

            ok, parsed, err = _validate_against_schema(resp.text, request.response_schema)
            if ok:
                return resp.model_copy(update={"parsed": parsed}), True, None

            last_error = err
            if attempt < attempts - 1:
                current_prompt = (
                    f"{request.prompt}\n\n"
                    f"[SCHEMA REPAIR ATTEMPT {attempt + 1}/{self.max_local_retries}] "
                    f"Your previous output failed validation: {err}. "
                    "Respond ONLY with valid JSON matching the required schema."
                )

        return last_response, False, last_error

    def _escalate_to_cloud(
        self,
        request: LLMRequest,
        reason: EscalationReason,
    ) -> tuple[LLMResponse | None, dict[str, str]]:
        """Scrub PII, check quota ceilings, and execute cloud providers with 429 failover."""
        scrubbed_system, mapping = self.pii_scrubber.scrub(request.system_prompt)
        scrubbed_prompt, mapping = self.pii_scrubber.scrub(request.prompt, mapping=mapping)
        est_tokens = max(1, (len(scrubbed_prompt) + len(scrubbed_system)) // 4)

        cloud_request = request.model_copy(
            update={
                "prompt": scrubbed_prompt,
                "system_prompt": scrubbed_system,
                "model": None,
            }
        )

        failures: dict[str, str] = {}
        for provider in self.cloud_providers:
            if self.quota_ledger is not None:
                allowed, quota_reason = self.quota_ledger.can_consume(
                    provider.name,
                    est_tokens=est_tokens,
                )
                if not allowed:
                    failures[provider.name] = quota_reason
                    continue

            try:
                cloud_resp = provider.generate(cloud_request)
            except RateLimitError as exc:
                if self.quota_ledger is not None:
                    self.quota_ledger.record_rate_limit(
                        provider.name,
                        retry_after_seconds=exc.retry_after,
                        reason=str(exc),
                    )
                failures[provider.name] = f"rate_limited: {exc}"
                continue
            except ProviderError as exc:
                failures[provider.name] = f"provider_error: {exc}"
                continue

            if self.quota_ledger is not None:
                total = cloud_resp.total_tokens or (
                    cloud_resp.prompt_tokens + cloud_resp.completion_tokens
                )
                self.quota_ledger.record_usage(
                    provider.name,
                    prompt_tokens=cloud_resp.prompt_tokens,
                    completion_tokens=cloud_resp.completion_tokens,
                    total_tokens=total,
                )

            restored_text = self.pii_scrubber.restore(cloud_resp.text, mapping)
            restored_parsed = (
                self.pii_scrubber.restore(cloud_resp.parsed, mapping)
                if cloud_resp.parsed is not None
                else None
            )

            if request.response_schema is not None:
                ok, parsed, err = _validate_against_schema(
                    restored_text,
                    request.response_schema,
                )
                if not ok:
                    failures[provider.name] = f"invalid_cloud_schema: {err}"
                    continue
                restored_parsed = parsed

            final_resp = cloud_resp.model_copy(
                update={
                    "text": restored_text,
                    "parsed": restored_parsed,
                    "cloud_verified": True,
                    "cached": False,
                    "escalated": True,
                    "escalation_reason": reason.value,
                    "pii_redactions": len(mapping),
                }
            )
            return final_resp, failures

        return None, failures

    def route(
        self,
        request: LLMRequest,
        *,
        escalation_reason: EscalationReason | None = None,
        bypass_cache: bool = False,
    ) -> LLMResponse:
        """Route a request local-first with optional audited cloud escalation."""
        if escalation_reason is not None and not isinstance(
            escalation_reason, EscalationReason
        ):
            raise ValueError(
                "escalation_reason must be an EscalationReason enum member, "
                f"got {escalation_reason!r}"
            )

        cache_key = None if bypass_cache else self._cache_key(request, escalation_reason)
        if cache_key is not None and self.prompt_cache is not None:
            cached_payload = self.prompt_cache.get(cache_key)
            if cached_payload is not None:
                return LLMResponse.model_validate({**cached_payload, "cached": True})

        local_resp, local_valid, local_err = self._run_local_with_retries(request)

        effective_reason: EscalationReason | None = escalation_reason
        if not local_valid and effective_reason is None:
            effective_reason = EscalationReason.SCHEMA_RETRY_EXHAUSTED

        # Stay completely local when local succeeded and no explicit escalation was requested.
        if effective_reason is None and local_resp is not None:
            self._store_cache(cache_key, local_resp)
            return local_resp

        # Enforce --strict-local: NEVER invoke any cloud provider.
        if self.strict_local or not self.cloud_providers:
            if local_resp is None:
                err_msg = (
                    f"Local provider failed under strict-local mode: {local_err}"
                    if self.strict_local
                    else f"Local provider failed and no cloud providers are configured: {local_err}"
                )
                raise ProviderError(
                    err_msg,
                    provider=self.local_provider.name,
                )
            degraded = local_resp.model_copy(
                update={
                    "cloud_verified": False,
                    "escalated": False,
                    "escalation_reason": (
                        effective_reason.value if effective_reason is not None else None
                    ),
                    "raw_metadata": {
                        **local_resp.raw_metadata,
                        "strict_local": self.strict_local,
                        "cloud_escalation_skipped": True,
                        "local_error": local_err,
                    },
                }
            )
            if local_valid:
                self._store_cache(cache_key, degraded)
            return degraded

        assert effective_reason is not None
        cloud_resp, cloud_failures = self._escalate_to_cloud(request, effective_reason)
        if cloud_resp is not None:
            self._store_cache(cache_key, cloud_resp)
            return cloud_resp

        # Graceful degradation to local output when all cloud free tiers are exhausted/blocked.
        if local_resp is not None:
            degraded = local_resp.model_copy(
                update={
                    "cloud_verified": False,
                    "escalated": False,
                    "escalation_reason": effective_reason.value,
                    "raw_metadata": {
                        **local_resp.raw_metadata,
                        "cloud_fallback": True,
                        "cloud_failures": cloud_failures,
                        "local_error": local_err,
                    },
                }
            )
            return degraded

        raise ProviderError(
            f"Local provider failed ({local_err}) and all cloud providers failed: {cloud_failures}",
            provider="router",
        )
