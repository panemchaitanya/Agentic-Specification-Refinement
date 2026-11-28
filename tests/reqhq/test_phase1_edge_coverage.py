"""Edge-case unit tests closing the final 14 uncovered lines across Phase 1 modules.

Kept in a dedicated test module so the 91 original Phase 1 tests remain untouched.
All tests run 100% offline with zero network calls and zero API keys.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest

from reqhq.ingestion.chunker import HierarchicalChunker, _trim_acronym_expansion
from reqhq.ingestion.connectors import SharePointConnector
from reqhq.ingestion.normalizer import NormalizedDocument
from reqhq.llm.providers.base import LLMRequest
from reqhq.llm.providers.gemini import GeminiFreeProvider
from reqhq.llm.providers.groq import GroqFreeProvider
from reqhq.llm.providers.ollama import OllamaLocalProvider
from reqhq.schemas.routing_adr import TraceabilityChain
from reqhq.schemas.system_context import BrownfieldArtifactRef


class _FakeMSALApp:
    def get_accounts(self) -> list[dict[str, str]]:
        return [{"username": "engineer@example.com"}]

    def acquire_token_silent(
        self,
        scopes: list[str],
        account: dict[str, str] | None = None,
    ) -> dict[str, str]:
        return {"access_token": "offline-msal-token"}


def _make_mock_httpx_client_factory(
    handler: Any,
    captured_kwargs: list[dict[str, Any]],
) -> Any:
    real_client_cls = httpx.Client

    def _factory(*args: Any, **kwargs: Any) -> httpx.Client:
        captured_kwargs.append(dict(kwargs))
        return real_client_cls(*args, transport=httpx.MockTransport(handler), **kwargs)

    return _factory


class TestChunkerEdgeCases:
    """Covers hyphenated leading-initial acronym matching and mid-page prose anchor inheritance."""

    def test_trim_acronym_expansion_matches_leading_sub_initial_of_hyphenated_word(self) -> None:
        # "Multi-Tenant" has sub-initials ['m', 't'], while acronym "GMA" uses only 'M'.
        assert (
            _trim_acronym_expansion("Global Multi-Tenant Architecture", "GMA")
            == "Global Multi-Tenant Architecture"
        )
        # Boundary case where letter_idx < len(sub_initials) - 1 on the first word.
        assert (
            _trim_acronym_expansion("High-Availability System", "HS")
            == "High-Availability System"
        )

        # End-to-end via extract_glossary
        glossary = HierarchicalChunker.extract_glossary(
            "The platform adopts a Global Multi-Tenant Architecture (GMA) for isolation.\n"
        )
        assert glossary.acronyms["GMA"] == "Global Multi-Tenant Architecture"

    def test_chunk_inherits_active_page_anchor_when_prose_precedes_next_page_anchor(
        self,
        tmp_path: Path,
    ) -> None:
        # Chunk 0 establishes [page:1]; chunk 1 begins with prose continuing page 1
        # and then transitions to [page:2] mid-chunk, exercising _has_prose_before_offset -> True.
        markdown = (
            "[page:1]\n"
            + ("Alpha requirement detail on page one. " * 4)
            + "\n\n"
            + "Continued page-one prose before the break.\n\n"
            + "[page:2]\n"
            + "Beta requirement detail on page two.\n"
        )
        doc = NormalizedDocument(
            source_path=tmp_path / "spec.pdf",
            source_sha256=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            parser_name="pypdf",
            media_type="application/pdf",
            markdown=markdown,
            char_count=len(markdown),
            page_count=2,
            anchors=[],
        )
        chunker = HierarchicalChunker(max_chunk_chars=180, overlap_chars=0)
        result = chunker.chunk(doc)

        assert result.truncation_ratio == 0.0
        assert result.chunk_count >= 2
        # Locate the chunk that transitions from page-1 prose into [page:2]
        transition_chunks = [c for c in result.chunks if "[page:2]" in c.text]
        assert len(transition_chunks) == 1
        assert transition_chunks[0].anchors == ["[page:1]", "[page:2]"]

    def test_chunk_does_not_inherit_prior_page_when_only_heading_and_blank_line_precede_anchor(
        self,
        tmp_path: Path,
    ) -> None:
        markdown = (
            "# Section One\n[page:1]\nPage one content.\n\n"
            "# Section Two\n\n[page:2]\nPage two content.\n"
        )
        doc = NormalizedDocument(
            source_path=tmp_path / "sections.md",
            source_sha256=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            parser_name="plain_text_markdown",
            media_type="text/markdown",
            markdown=markdown,
            char_count=len(markdown),
            page_count=2,
            anchors=[],
        )
        result = HierarchicalChunker(max_chunk_chars=200, overlap_chars=0).chunk(doc)
        assert result.chunk_count == 2
        assert result.chunks[1].anchors == ["[page:2]"]


class TestDefaultHttpxClientBranchesOffline:
    """Covers the default httpx.Client context-manager paths offline via MockTransport."""

    def test_sharepoint_connector_uses_default_httpx_client_when_uninjected(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[dict[str, Any]] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert request.headers["authorization"] == "Bearer offline-msal-token"
            return httpx.Response(
                200,
                json={"value": [{"id": "item-1", "name": "prd.md"}]},
            )

        monkeypatch.setattr(
            httpx,
            "Client",
            _make_mock_httpx_client_factory(_handler, captured),
        )
        connector = SharePointConnector(msal_app=_FakeMSALApp())
        items = connector.list_drive_children()

        assert items == [{"id": "item-1", "name": "prd.md"}]
        assert captured == [{"follow_redirects": True, "timeout": 30.0}]

    def test_gemini_provider_uses_default_httpx_client_when_uninjected(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[dict[str, Any]] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["x-goog-api-key"] == "fake-gemini-key"
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {"parts": [{"text": "gemini default client ok"}]},
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 5,
                        "candidatesTokenCount": 4,
                        "totalTokenCount": 9,
                    },
                },
            )

        monkeypatch.setattr(
            httpx,
            "Client",
            _make_mock_httpx_client_factory(_handler, captured),
        )
        provider = GeminiFreeProvider(api_key="fake-gemini-key", timeout=12.5)
        resp = provider.generate(LLMRequest(prompt="Ping Gemini"))

        assert resp.text == "gemini default client ok"
        assert resp.total_tokens == 9
        assert resp.cloud_verified is True
        assert captured == [{"timeout": 12.5}]

    def test_groq_provider_uses_default_httpx_client_when_uninjected(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[dict[str, Any]] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["authorization"] == "Bearer fake-groq-key"
            return httpx.Response(
                200,
                json={
                    "model": "llama-3.3-70b-versatile",
                    "choices": [
                        {
                            "message": {"content": "groq default client ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 6,
                        "completion_tokens": 4,
                        "total_tokens": 10,
                    },
                },
            )

        monkeypatch.setattr(
            httpx,
            "Client",
            _make_mock_httpx_client_factory(_handler, captured),
        )
        provider = GroqFreeProvider(api_key="fake-groq-key", timeout=15.0)
        resp = provider.generate(LLMRequest(prompt="Ping Groq"))

        assert resp.text == "groq default client ok"
        assert resp.total_tokens == 10
        assert resp.cloud_verified is True
        assert captured == [{"timeout": 15.0}]

    def test_ollama_provider_uses_default_httpx_client_when_uninjected(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[dict[str, Any]] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == "http://127.0.0.1:11434/api/generate"
            return httpx.Response(
                200,
                json={
                    "model": "qwen3:4b",
                    "response": "ollama default client ok",
                    "prompt_eval_count": 7,
                    "eval_count": 5,
                    "done_reason": "stop",
                },
            )

        monkeypatch.setattr(
            httpx,
            "Client",
            _make_mock_httpx_client_factory(_handler, captured),
        )
        provider = OllamaLocalProvider(default_model="qwen3:4b", timeout=20.0)
        resp = provider.generate(LLMRequest(prompt="Ping Ollama"))

        assert resp.text == "ollama default client ok"
        assert resp.total_tokens == 12
        assert resp.cloud_verified is False
        assert captured == [{"timeout": 20.0}]


class TestSystemContextExplicitNoneSha256:
    """Covers explicit None optional-field validation paths in Phase 1 schemas."""

    def test_brownfield_artifact_ref_accepts_explicit_none_sha256(self) -> None:
        ref = BrownfieldArtifactRef.model_validate(
            {
                "artifact_kind": "architecture_md",
                "path": "ARCHITECTURE.md",
                "sha256": None,
                "summary": "Unindexed architecture overview",
            }
        )
        assert ref.sha256 is None
        assert ref.path == "ARCHITECTURE.md"

    def test_traceability_chain_accepts_explicit_none_optional_refs(self) -> None:
        chain = TraceabilityChain.model_validate(
            {
                "source_sha256": "a" * 64,
                "req_id": "REQ-001",
                "adr_id": "ADR-R001",
                "alloy_model_ref": None,
                "pbt_spec_id": None,
            }
        )
        assert chain.alloy_model_ref is None
        assert chain.pbt_spec_id is None
        assert chain.is_complete() is False
