"""Offline unit tests for QuotaLedger, PIIScrubber, LLM providers, and ProviderRouter."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import httpx
from pydantic import BaseModel, ConfigDict
import pytest

SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

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
from reqhq.store.prompt_cache import PromptCache


class _SampleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    owner: str


class _MutableClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# QuotaLedger tests
# ---------------------------------------------------------------------------


def test_quota_ledger_enforces_90_percent_rpm_and_tpm_ceilings(tmp_path: Path) -> None:
    clock = _MutableClock()
    limits = {
        "gemini": ProviderQuotaLimits(
            rpm=10, rpd=100, tpm=1000, tpd=10_000, default_cooldown_seconds=30.0
        )
    }
    ledger = QuotaLedger(tmp_path / "quota.db", ceiling_ratio=0.90, limits=limits, clock=clock)

    # 90% of RPM=10 is 9 requests allowed per minute
    for _ in range(9):
        ok, reason = ledger.can_consume("gemini", est_tokens=50)
        assert ok is True
        assert reason == "ok"
        ledger.record_usage("gemini", prompt_tokens=30, completion_tokens=20)

    # 10th request hits the 90% RPM ceiling
    ok, reason = ledger.can_consume("gemini", est_tokens=10)
    assert ok is False
    assert "RPM ceiling" in reason

    # Advancing past 60s resets the minute window while keeping daily usage
    clock.advance(61.0)
    ok, _ = ledger.can_consume("gemini", est_tokens=900)
    assert ok is True
    ok_over_tpm, reason_tpm = ledger.can_consume("gemini", est_tokens=901)
    assert ok_over_tpm is False
    assert "TPM ceiling" in reason_tpm


def test_quota_ledger_rpd_tpd_cooldown_and_status(tmp_path: Path) -> None:
    clock = _MutableClock()
    limits = {
        "groq": ProviderQuotaLimits(
            rpm=100, rpd=10, tpm=10_000, tpd=1_000, default_cooldown_seconds=45.0
        )
    }
    ledger = QuotaLedger(tmp_path / "quota.db", ceiling_ratio=0.90, limits=limits, clock=clock)

    # Record 850 tokens (within 900 TPD cap)
    ledger.record_usage("groq", prompt_tokens=500, completion_tokens=350)
    ok, reason = ledger.can_consume("groq", est_tokens=100)
    assert ok is False
    assert "TPD ceiling" in reason

    # RPD ceiling check using a second provider
    ledger.set_limits(
        "tiny",
        ProviderQuotaLimits(rpm=100, rpd=2, tpm=10_000, tpd=100_000),
    )
    ledger.record_usage("tiny", prompt_tokens=10, completion_tokens=5)
    ok_rpd, reason_rpd = ledger.can_consume("tiny", est_tokens=5)
    assert ok_rpd is False
    assert "RPD ceiling" in reason_rpd

    # Cooldown tracking and expiry
    until = ledger.record_rate_limit("gemini", retry_after_seconds=25.0, reason="429 burst")
    assert until == pytest.approx(clock.now + 25.0)
    ok_cd, reason_cd = ledger.can_consume("gemini", est_tokens=10)
    assert ok_cd is False
    assert "cooldown" in reason_cd

    status = ledger.get_status("gemini")["gemini"]
    assert status["cooldown_active"] is True
    assert status["available"] is False
    assert status["cooldown_reason"] == "429 burst"

    clock.advance(26.0)
    assert ledger.can_consume("gemini", est_tokens=10)[0] is True

    # Manual clear_cooldown and unknown provider handling
    ledger.record_rate_limit("gemini", retry_after_seconds=None)
    assert ledger.can_consume("gemini", est_tokens=1)[0] is False
    ledger.clear_cooldown("gemini")
    assert ledger.can_consume("gemini", est_tokens=1)[0] is True

    ok_unk, reason_unk = ledger.can_consume("unknown_cloud", est_tokens=1)
    assert ok_unk is False
    assert "No quota limits" in reason_unk
    assert ledger.get_status("unknown_cloud")["unknown_cloud"]["configured"] is False

    # Validation edge cases
    with pytest.raises(ValueError):
        QuotaLedger(tmp_path / "bad.db", ceiling_ratio=0.0)
    with pytest.raises(ValueError):
        ledger.can_consume("gemini", est_tokens=-1)
    with pytest.raises(ValueError):
        ledger.record_usage("gemini", prompt_tokens=-1)
    with pytest.raises(ValueError):
        ledger.record_usage("gemini", prompt_tokens=1, total_tokens=-5)
    assert "gemini" in DEFAULT_PROVIDER_LIMITS and "groq" in DEFAULT_PROVIDER_LIMITS


# ---------------------------------------------------------------------------
# PIIScrubber tests
# ---------------------------------------------------------------------------


def test_pii_scrubber_detects_and_reversibly_restores_all_categories() -> None:
    scrubber = PIIScrubber()
    raw = (
        "Contact alice.smith@acme.org or bob@acme.org (repeat alice.smith@acme.org). "
        "Phone: (415) 555-0199, SSN: 123-45-6789, Card: 4532-1234-5678-9012, "
        "Host: 192.168.10.42, Auth: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig, "
        "Key: sk-proj-abcdef1234567890xyz, "
        "DB: postgres://admin:s3cr3tPass@db.internal.local:5432/prod"
    )
    scrubbed, mapping = scrubber.scrub(raw)

    assert "alice.smith@acme.org" not in scrubbed
    assert "bob@acme.org" not in scrubbed
    assert "(415) 555-0199" not in scrubbed
    assert "123-45-6789" not in scrubbed
    assert "4532-1234-5678-9012" not in scrubbed
    assert "192.168.10.42" not in scrubbed
    assert "eyJhbGciOiJIUzI1NiJ9" not in scrubbed
    assert "sk-proj-abcdef1234567890xyz" not in scrubbed
    assert "s3cr3tPass" not in scrubbed

    # Deterministic reuse for repeated email
    assert scrubbed.count("<EMAIL_1>") == 2
    assert "<EMAIL_2>" in scrubbed
    assert "<CREDENTIAL_URL_1>" in scrubbed
    assert "<API_KEY_1>" in scrubbed
    assert "<SSN_1>" in scrubbed
    assert "<CREDIT_CARD_1>" in scrubbed
    assert "<PHONE_1>" in scrubbed
    assert "<IPV4_1>" in scrubbed

    assert scrubber.restore(scrubbed, mapping) == raw


def test_pii_scrubber_recursive_restore_and_shared_mapping() -> None:
    scrubber = PIIScrubber()
    sys_scrubbed, mapping = scrubber.scrub("System owner: lead@corp.io")
    prompt_scrubbed, mapping = scrubber.scrub(
        "Notify lead@corp.io and dev@corp.io", mapping=mapping
    )

    assert "<EMAIL_1>" in sys_scrubbed
    assert "<EMAIL_1>" in prompt_scrubbed
    assert "<EMAIL_2>" in prompt_scrubbed

    nested = {
        "owner": "<EMAIL_1>",
        "reviewers": ["<EMAIL_2>", ("<EMAIL_1>", 42, True, None)],
        "meta": {"<EMAIL_1>": "primary"},
    }
    restored = scrubber.restore(nested, mapping)
    assert restored == {
        "owner": "lead@corp.io",
        "reviewers": ["dev@corp.io", ("lead@corp.io", 42, True, None)],
        "meta": {"lead@corp.io": "primary"},
    }
    assert scrubber.restore(nested, {}) == nested


# ---------------------------------------------------------------------------
# Provider adapter tests (100% offline via httpx.MockTransport)
# ---------------------------------------------------------------------------


def test_ollama_provider_json_schema_and_gbnf_grammar() -> None:
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content.decode("utf-8"))
        captured.append(body)
        if body.get("prompt") == "fail500":
            return httpx.Response(500, text="internal error")
        if body.get("prompt") == "fail429":
            return httpx.Response(429, text="busy")
        if body.get("prompt") == "badjson":
            return httpx.Response(200, content=b"not-json")
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "response": '{"title": "Auth", "owner": "alice"}',
                "prompt_eval_count": 25,
                "eval_count": 15,
                "done_reason": "stop",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OllamaLocalProvider(client=client)
    assert isinstance(provider, LLMProvider)
    assert provider.is_local is True

    # 1. JSON schema constrained decoding disables thinking by default (DEF-P4B-003)
    resp = provider.generate(
        LLMRequest(
            prompt="Generate spec",
            system_prompt="Be concise",
            response_schema=_SampleSpec,
        )
    )
    assert resp.provider == "ollama"
    assert resp.cloud_verified is False
    assert resp.total_tokens == 40
    assert "format" in captured[0]
    assert captured[0]["think"] is False
    assert captured[0]["system"] == "Be concise"

    # 2. GBNF grammar takes precedence over format and disables thinking by default
    provider.generate(
        LLMRequest(prompt="Use grammar", gbnf_grammar='root ::= "ok"')
    )
    assert captured[1]["grammar"] == 'root ::= "ok"'
    assert "format" not in captured[1]
    assert captured[1]["think"] is False

    # 3. Unconstrained prompt omits `think`; explicit metadata bool overrides it
    provider.generate(LLMRequest(prompt="Plain unconstrained prompt"))
    assert "think" not in captured[2]
    provider.generate(
        LLMRequest(
            prompt="Explicit think override",
            response_schema=_SampleSpec,
            metadata={"think": True},
        )
    )
    assert captured[3]["think"] is True

    # Error handling
    with pytest.raises(ProviderError):
        provider.generate(LLMRequest(prompt="fail500"))
    with pytest.raises(RateLimitError):
        provider.generate(LLMRequest(prompt="fail429"))
    with pytest.raises(ProviderError):
        provider.generate(LLMRequest(prompt="badjson"))


def test_gemini_and_groq_providers_offline_http_and_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    # Missing API key fails fast without network
    with pytest.raises(ProviderError, match="GEMINI_API_KEY"):
        GeminiFreeProvider().generate(LLMRequest(prompt="hello"))
    with pytest.raises(ProviderError, match="GROQ_API_KEY"):
        GroqFreeProvider().generate(LLMRequest(prompt="hello"))

    gemini_captured: list[dict] = []
    gemini_503_attempts = 0

    def gemini_handler(req: httpx.Request) -> httpx.Response:
        nonlocal gemini_503_attempts
        assert req.headers["x-goog-api-key"] == "test-gemini-key"
        body = json.loads(req.content.decode("utf-8"))
        gemini_captured.append(body)
        text_in = body["contents"][0]["parts"][0]["text"]
        if text_in == "429":
            return httpx.Response(429, headers={"Retry-After": "12"}, text="quota")
        if text_in == "503-recover":
            gemini_503_attempts += 1
            if gemini_503_attempts == 1:
                return httpx.Response(503, text="high demand")
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {"parts": [{"text": '{"title": "Recovered", "owner": "u"}'}]},
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 5, "totalTokenCount": 10},
                },
            )
        if text_in == "500":
            return httpx.Response(500, text="server error")
        if text_in == "malformed":
            return httpx.Response(200, json={"candidates": []})
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"thought": True, "text": "internal chain of thought"},
                                {"text": '{"title": "G", "owner": "u"}'},
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 20,
                    "totalTokenCount": 30,
                },
            },
        )

    assert GeminiFreeProvider(api_key="test-gemini-key").max_transient_retries == 2
    g_client = httpx.Client(transport=httpx.MockTransport(gemini_handler))
    gemini = GeminiFreeProvider(
        api_key="test-gemini-key",
        client=g_client,
        max_transient_retries=2,
        transient_retry_delay_seconds=0.001,
    )
    assert gemini.default_model == "gemini-3.1-flash-lite"
    g_resp = gemini.generate(
        LLMRequest(prompt="ok", system_prompt="sys", response_schema=_SampleSpec)
    )
    assert g_resp.cloud_verified is True
    assert g_resp.total_tokens == 30
    assert g_resp.text == '{"title": "G", "owner": "u"}'
    assert "responseJsonSchema" in gemini_captured[0]["generationConfig"]
    assert "responseSchema" not in gemini_captured[0]["generationConfig"]
    rec_resp = gemini.generate(LLMRequest(prompt="503-recover"))
    assert rec_resp.text == '{"title": "Recovered", "owner": "u"}'
    assert gemini_503_attempts == 2
    with pytest.raises(RateLimitError) as rl_info:
        gemini.generate(LLMRequest(prompt="429"))
    assert rl_info.value.retry_after == 12.0
    with pytest.raises(ProviderError):
        gemini.generate(LLMRequest(prompt="500"))
    with pytest.raises(ProviderError):
        gemini.generate(LLMRequest(prompt="malformed"))

    groq_captured: list[dict] = []

    def groq_handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer test-groq-key"
        body = json.loads(req.content.decode("utf-8"))
        groq_captured.append(body)
        user_msg = body["messages"][-1]["content"]
        if user_msg == "429":
            return httpx.Response(429, headers={"Retry-After": "bad"}, text="limit")
        if user_msg == "502":
            return httpx.Response(502, text="bad gateway")
        if user_msg == "json-validate-fallback" and "response_format" in body:
            return httpx.Response(
                400,
                text='{"error":{"code":"json_validate_failed","failed_generation":""}}',
            )
        if user_msg == "malformed":
            return httpx.Response(200, json={"choices": []})
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [
                    {
                        "message": {"content": '{"title": "Q", "owner": "v"}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 9, "total_tokens": 20},
            },
        )

    q_client = httpx.Client(transport=httpx.MockTransport(groq_handler))
    groq = GroqFreeProvider(api_key="test-groq-key", client=q_client)
    q_resp = groq.generate(
        LLMRequest(prompt="ok", system_prompt="sys", response_schema=_SampleSpec)
    )
    assert q_resp.cloud_verified is True
    assert q_resp.total_tokens == 20
    assert groq_captured[0]["reasoning_effort"] == "low"
    assert groq_captured[0]["response_format"] == {"type": "json_object"}
    fb_resp = groq.generate(
        LLMRequest(
            prompt="json-validate-fallback",
            system_prompt="sys",
            response_schema=_SampleSpec,
        )
    )
    assert fb_resp.text == '{"title": "Q", "owner": "v"}'
    assert "response_format" in groq_captured[1]
    assert "response_format" not in groq_captured[2]
    with pytest.raises(RateLimitError) as q_rl:
        groq.generate(LLMRequest(prompt="429"))
    assert q_rl.value.retry_after is None
    with pytest.raises(ProviderError):
        groq.generate(LLMRequest(prompt="502"))
    with pytest.raises(ProviderError):
        groq.generate(LLMRequest(prompt="malformed"))


# ---------------------------------------------------------------------------
# ProviderRouter integration tests
# ---------------------------------------------------------------------------


def test_router_local_first_schema_retry_and_prompt_cache(tmp_path: Path) -> None:
    cache = PromptCache(tmp_path / "cache.db")
    local = MockProvider(
        "ollama",
        is_local=True,
        responses=[
            "not valid json",
            '{"title": "Missing owner"}',
            '```json\n{"title": "Login", "owner": "alice"}\n```',
        ],
    )
    cloud = MockProvider("gemini", is_local=False)
    router = ProviderRouter(local, [cloud], prompt_cache=cache, max_local_retries=2)

    req = LLMRequest(prompt="Extract login requirement", response_schema=_SampleSpec)
    resp1 = router.route(req)
    assert resp1.provider == "ollama"
    assert resp1.parsed == {"title": "Login", "owner": "alice"}
    assert resp1.cloud_verified is False
    assert resp1.escalated is False
    assert resp1.cached is False
    assert len(local.calls) == 3
    assert len(cloud.calls) == 0

    # Second identical call hits PromptCache with zero new provider calls
    resp2 = router.route(req)
    assert resp2.cached is True
    assert resp2.parsed == {"title": "Login", "owner": "alice"}
    assert len(local.calls) == 3
    assert cache.stats()["hits"] == 1


def test_router_escalates_on_schema_retry_exhausted_with_pii_scrub_and_restore(
    tmp_path: Path,
) -> None:
    ledger = QuotaLedger(tmp_path / "quota.db")
    local = MockProvider(
        "ollama",
        is_local=True,
        responses=["bad1", "bad2", "bad3"],
    )

    def echo_cloud(req: LLMRequest) -> str:
        # Cloud MUST NOT see raw email or API key!
        assert "alice@secret.io" not in req.prompt
        assert "sk-secret-1234567890abcdef" not in req.prompt
        assert "<EMAIL_1>" in req.prompt
        return json.dumps({"title": "Audit <API_KEY_1>", "owner": "<EMAIL_1>"})

    gemini = MockProvider("gemini", is_local=False, responses=[echo_cloud])
    router = ProviderRouter(
        local,
        [gemini],
        quota_ledger=ledger,
        max_local_retries=2,
    )

    req = LLMRequest(
        prompt="Verify alice@secret.io with key sk-secret-1234567890abcdef",
        response_schema=_SampleSpec,
    )
    resp = router.route(req)

    assert resp.provider == "gemini"
    assert resp.cloud_verified is True
    assert resp.escalated is True
    assert resp.escalation_reason == EscalationReason.SCHEMA_RETRY_EXHAUSTED.value
    assert resp.pii_redactions == 2
    assert resp.parsed == {
        "title": "Audit sk-secret-1234567890abcdef",
        "owner": "alice@secret.io",
    }
    assert ledger.get_status("gemini")["gemini"]["rpm_used"] == 1


def test_router_strict_local_never_calls_cloud() -> None:
    local = MockProvider("ollama", is_local=True, responses=["invalid", "still invalid", "nope"])
    cloud = MockProvider("gemini", is_local=False, responses=['{"title": "X", "owner": "Y"}'])
    router = ProviderRouter(local, [cloud], strict_local=True, max_local_retries=2)

    resp = router.route(
        LLMRequest(prompt="Audit", response_schema=_SampleSpec),
        escalation_reason=EscalationReason.FINAL_AUDIT,
    )
    assert len(cloud.calls) == 0
    assert resp.provider == "ollama"
    assert resp.cloud_verified is False
    assert resp.escalated is False
    assert resp.raw_metadata["strict_local"] is True

    # Valid local response under strict_local + explicit escalation still stays local and caches
    valid_local = MockProvider(
        "ollama", is_local=True, default_response='{"title": "OK", "owner": "u"}'
    )
    valid_strict = ProviderRouter(valid_local, [cloud], strict_local=True)
    ok_resp = valid_strict.route(
        LLMRequest(prompt="Valid audit", response_schema=_SampleSpec),
        escalation_reason=EscalationReason.FINAL_AUDIT,
    )
    assert ok_resp.provider == "ollama" and ok_resp.parsed == {"title": "OK", "owner": "u"}

    # Implicit schema-retry exhaustion where cloud also fails degrades gracefully to local output
    bad_local = MockProvider("ollama", is_local=True, responses=["bad-json"])
    bad_cloud = MockProvider("gemini", is_local=False, responses=[ProviderError("503", provider="gemini")])
    implicit_fallback = ProviderRouter(bad_local, [bad_cloud], max_local_retries=0)
    deg = implicit_fallback.route(LLMRequest(prompt="Implicit retry", response_schema=_SampleSpec))
    assert deg.provider == "ollama"
    assert deg.cloud_verified is False
    assert deg.raw_metadata["cloud_fallback"] is True
    assert deg.escalation_reason == EscalationReason.SCHEMA_RETRY_EXHAUSTED.value


def test_router_429_failover_gemini_to_groq_and_graceful_degradation(tmp_path: Path) -> None:
    clock = _MutableClock()
    ledger = QuotaLedger(tmp_path / "quota.db", clock=clock)
    local = MockProvider(
        "ollama",
        is_local=True,
        default_response='{"title": "LocalDraft", "owner": "local"}',
    )
    gemini = MockProvider("gemini", is_local=False, fail_with_429_count=1, retry_after=30.0)
    groq = MockProvider(
        "groq",
        is_local=False,
        responses=['{"title": "GroqVerified", "owner": "cloud"}'],
    )
    router = ProviderRouter(local, [gemini, groq], quota_ledger=ledger)

    # 1. Explicit escalation: Gemini 429s -> failover to Groq (short-circuits past local: 0 local calls!)
    resp = router.route(
        LLMRequest(prompt="Final audit", response_schema=_SampleSpec),
        escalation_reason=EscalationReason.FINAL_AUDIT,
    )
    assert resp.provider == "groq"
    assert resp.cloud_verified is True
    assert resp.escalated is True
    assert resp.escalation_reason == EscalationReason.FINAL_AUDIT.value
    assert len(local.calls) == 0
    assert ledger.get_status("gemini")["gemini"]["cooldown_active"] is True

    # 2. Both cloud providers unavailable -> degrades gracefully to local with cloud_verified=False
    ledger.record_rate_limit("groq", retry_after_seconds=60.0)
    degraded = router.route(
        LLMRequest(prompt="Second audit", response_schema=_SampleSpec),
        escalation_reason=EscalationReason.CRITIC_DISAGREEMENT,
    )
    assert degraded.provider == "ollama"
    assert degraded.cloud_verified is False
    assert degraded.escalated is False
    assert degraded.parsed == {"title": "LocalDraft", "owner": "local"}
    assert len(local.calls) == 1
    assert degraded.raw_metadata["cloud_fallback"] is True
    assert "gemini" in degraded.raw_metadata["cloud_failures"]
    assert "groq" in degraded.raw_metadata["cloud_failures"]

    # 3. Explicit escalation where both cloud AND local fallback fail -> raises ProviderError
    both_fail_router = ProviderRouter(
        MockProvider("ollama", is_local=True, responses=[ProviderError("local down", provider="ollama")]),
        [MockProvider("gemini", is_local=False, responses=[ProviderError("gemini 503", provider="gemini")])],
    )
    with pytest.raises(ProviderError, match="Cloud escalation failed"):
        both_fail_router.route(
            LLMRequest(prompt="Must audit"),
            escalation_reason=EscalationReason.FINAL_AUDIT,
        )


def test_router_provider_error_paths() -> None:
    with pytest.raises(ValueError):
        ProviderRouter(MockProvider(), max_local_retries=-1)

    failing_local = MockProvider(
        "ollama",
        is_local=True,
        responses=[ProviderError("ollama down", provider="ollama")],
    )
    strict_router = ProviderRouter(failing_local, strict_local=True)
    with pytest.raises(ProviderError, match="strict-local"):
        strict_router.route(LLMRequest(prompt="hi"))

    failing_local2 = MockProvider(
        "ollama",
        is_local=True,
        responses=[ProviderError("ollama down", provider="ollama")],
    )
    failing_cloud = MockProvider(
        "gemini",
        is_local=False,
        responses=[ProviderError("gemini 500", provider="gemini")],
    )
    router2 = ProviderRouter(failing_local2, [failing_cloud])
    with pytest.raises(ProviderError, match="all cloud providers failed"):
        router2.route(LLMRequest(prompt="hi"))


def test_provider_http_transport_errors_and_invalid_cloud_schema() -> None:
    def boom_handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline transport error", request=req)

    boom_client = httpx.Client(transport=httpx.MockTransport(boom_handler))
    with pytest.raises(ProviderError, match="Ollama request failed"):
        OllamaLocalProvider(client=boom_client).generate(LLMRequest(prompt="hi"))
    with pytest.raises(ProviderError, match="Gemini request failed"):
        GeminiFreeProvider(api_key="k", client=boom_client).generate(LLMRequest(prompt="hi"))
    with pytest.raises(ProviderError, match="Groq request failed"):
        GroqFreeProvider(api_key="k", client=boom_client).generate(LLMRequest(prompt="hi"))

    # Cloud returning invalid JSON degrades gracefully to local output
    local = MockProvider("ollama", is_local=True, default_response='{"title": "L", "owner": "u"}')
    bad_cloud = MockProvider("gemini", is_local=False, responses=["not-json"])
    router = ProviderRouter(local, [bad_cloud])
    resp = router.route(
        LLMRequest(prompt="Check", response_schema=_SampleSpec),
        escalation_reason=EscalationReason.FINAL_AUDIT,
    )
    assert resp.provider == "ollama"
    assert resp.cloud_verified is False
    assert "invalid_cloud_schema" in resp.raw_metadata["cloud_failures"]["gemini"]


def test_quota_ledger_bidirectional_failover_selection(tmp_path: Path) -> None:
    clock = _MutableClock()
    limits = {
        "gemini": ProviderQuotaLimits(rpm=2, rpd=10, tpm=100, tpd=1000),
        "groq": ProviderQuotaLimits(rpm=2, rpd=10, tpm=100, tpd=1000),
    }
    ledger = QuotaLedger(tmp_path / "quota_ledger.db", ceiling_ratio=0.90, limits=limits, clock=clock)

    # Initially gemini is chosen first
    chosen, skipped = ledger.select_available_provider(("gemini", "groq"), est_tokens=10)
    assert chosen == "gemini"
    assert skipped == {}

    # Hitting 90% RPM ceiling on gemini (1 of 1.8) -> selects groq
    ledger.record_usage("gemini", prompt_tokens=10)
    chosen, skipped = ledger.select_available_provider(("gemini", "groq"), est_tokens=10)
    assert chosen == "groq"
    assert "RPM ceiling" in skipped["gemini"]

    # Advancing window restores gemini; putting groq in cooldown and asking groq->gemini fails over to gemini
    clock.advance(61.0)
    ledger.record_rate_limit("groq", retry_after_seconds=30.0, reason="429 groq")
    chosen, skipped = ledger.select_available_provider(("groq", "gemini"), est_tokens=10)
    assert chosen == "gemini"
    assert "cooldown" in skipped["groq"]

    # Both unavailable -> returns None with both reasons + unconfigured provider handled
    ledger.record_rate_limit("gemini", retry_after_seconds=30.0)
    chosen, skipped = ledger.select_available_provider(("gemini", "groq", "unknown"), est_tokens=10)
    assert chosen is None
    assert "gemini" in skipped and "groq" in skipped and "unknown" in skipped

    with pytest.raises(ValueError):
        ledger.select_available_provider(("gemini",), est_tokens=-1)


def test_router_quota_ceiling_failover_alloy_reason_and_cache_isolation(tmp_path: Path) -> None:
    clock = _MutableClock()
    limits = {
        "gemini": ProviderQuotaLimits(rpm=2, rpd=10, tpm=500, tpd=5000),
        "groq": ProviderQuotaLimits(rpm=10, rpd=100, tpm=5000, tpd=50000),
    }
    ledger = QuotaLedger(tmp_path / "quota_ledger.db", ceiling_ratio=0.90, limits=limits, clock=clock)
    cache = PromptCache(tmp_path / "prompt_cache.db")

    # Exhaust gemini's 90% RPM cap (1 request out of 1.8) before routing
    ledger.record_usage("gemini", prompt_tokens=10)

    local = MockProvider("ollama", is_local=True, default_response="plain local output")
    gemini = MockProvider("gemini", is_local=False, default_response="gemini output")
    groq = MockProvider("groq", is_local=False, default_response="groq alloy repair")
    router = ProviderRouter(local, [gemini, groq], quota_ledger=ledger, prompt_cache=cache)

    # Local plain-text (no response_schema) stays completely local
    plain = router.route(LLMRequest(prompt="Summarize locally"))
    assert plain.provider == "ollama"
    assert plain.cloud_verified is False
    assert len(gemini.calls) == 0 and len(groq.calls) == 0

    # Explicit ALLOY_REPAIR_EXHAUSTED skips capped gemini (0 calls) and escalates to groq
    repaired = router.route(
        LLMRequest(prompt="Repair alloy model"),
        escalation_reason=EscalationReason.ALLOY_REPAIR_EXHAUSTED,
    )
    assert repaired.provider == "groq"
    assert repaired.cloud_verified is True
    assert repaired.escalation_reason == EscalationReason.ALLOY_REPAIR_EXHAUSTED.value
    assert len(gemini.calls) == 0
    assert len(groq.calls) == 1

    # Invalid escalation_reason rejected at runtime
    with pytest.raises(ValueError, match="EscalationReason"):
        router.route(
            LLMRequest(prompt="Unauthorized escalation"),
            escalation_reason="just_because",  # type: ignore[arg-type]
        )

    # Distinct json_schema and gbnf_grammar produce distinct cache keys
    schema_a = {"type": "object", "properties": {"a": {"type": "string"}}}
    schema_b = {"type": "object", "properties": {"b": {"type": "integer"}}}
    req_a = LLMRequest(prompt="same", json_schema=schema_a)
    req_b = LLMRequest(prompt="same", json_schema=schema_b)
    req_g = LLMRequest(prompt="same", gbnf_grammar='root ::= "yes"')
    assert req_a.resolved_json_schema() == schema_a
    r_a = router.route(req_a)
    r_b = router.route(req_b)
    r_g = router.route(req_g)
    assert r_a.cached is False and r_b.cached is False and r_g.cached is False
    assert router.route(req_g).cached is True
    assert router.route(req_g, bypass_cache=True).cached is False

    # No cloud providers configured with failing local gives accurate message
    no_cloud_router = ProviderRouter(
        MockProvider("ollama", is_local=True, responses=[ProviderError("down", provider="ollama")]),
        cloud_providers=[],
        strict_local=False,
    )
    with pytest.raises(ProviderError, match="no cloud providers are configured"):
        no_cloud_router.route(LLMRequest(prompt="hi"))


def test_pii_scrubber_extra_patterns_and_provider_edge_cases() -> None:
    import re

    scrubber = PIIScrubber(extra_patterns=[("EMPLOYEE_ID", re.compile(r"\bEMP-\d{5}\b"))])
    raw = (
        "Employee EMP-12345 used AIzaSyD1234567890abcdef1234567890, "
        "gsk_1234567890abcdef1234, ghp_1234567890abcdef1234, AKIAIOSFODNN7EXAMPLE, "
        "xoxb-1234567890-abcdef, Amex 3782-822463-10005, Intl +44 20 7946 0958"
    )
    scrubbed, mapping = scrubber.scrub(raw)
    assert "EMP-12345" not in scrubbed
    assert "<EMPLOYEE_ID_1>" in scrubbed
    assert "AIzaSyD" not in scrubbed
    assert "gsk_1234567890" not in scrubbed
    assert "AKIAIOSFODNN7EXAMPLE" not in scrubbed
    assert "3782-822463-10005" not in scrubbed
    assert scrubber.restore(scrubbed, mapping) == raw

    # MockProvider callable returning LLMResponse directly
    direct = LLMResponse(text="custom", provider="mock", model="m1", total_tokens=7)
    mock = MockProvider("mock", responses=[lambda _req: direct])
    assert mock.generate(LLMRequest(prompt="hi")) is direct

    # OllamaLocalProvider aligns default_model with Tier 1 registry (qwen3:8b)
    assert OllamaLocalProvider().default_model == "qwen3:8b"

    # Gemini missing/bad Retry-After and Groq null content + numeric Retry-After
    def g_edge(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content.decode("utf-8"))
        text_in = body["contents"][0]["parts"][0]["text"]
        if text_in == "no-header":
            return httpx.Response(429, text="limit")
        return httpx.Response(429, headers={"Retry-After": "invalid"}, text="limit")

    gemini = GeminiFreeProvider(
        api_key="k",
        client=httpx.Client(transport=httpx.MockTransport(g_edge)),
    )
    with pytest.raises(RateLimitError) as e1:
        gemini.generate(LLMRequest(prompt="no-header"))
    assert e1.value.retry_after is None
    with pytest.raises(RateLimitError) as e2:
        gemini.generate(LLMRequest(prompt="bad-header"))
    assert e2.value.retry_after is None

    def q_edge(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content.decode("utf-8"))
        msg = body["messages"][-1]["content"]
        if msg == "no-header":
            return httpx.Response(429, text="limit")
        if msg == "num-header":
            return httpx.Response(429, headers={"Retry-After": " 15 "}, text="limit")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": None}, "finish_reason": "stop"}]},
        )

    groq = GroqFreeProvider(
        api_key="k",
        client=httpx.Client(transport=httpx.MockTransport(q_edge)),
    )
    with pytest.raises(RateLimitError) as q1:
        groq.generate(LLMRequest(prompt="no-header"))
    assert q1.value.retry_after is None
    with pytest.raises(RateLimitError) as q2:
        groq.generate(LLMRequest(prompt="num-header"))
    assert q2.value.retry_after == 15.0
    with pytest.raises(ProviderError, match="unexpected response payload"):
        groq.generate(LLMRequest(prompt="null-content"))
