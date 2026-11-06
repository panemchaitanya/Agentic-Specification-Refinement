"""Bounded 429/503 retry behavior of GeminiLLM (no live calls)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from specpipe.llm import GeminiLLM, LLMError, MAX_ATTEMPTS


def _resp(status, text="x", retry_after=None, payload=None):
    r = mock.Mock()
    r.status_code = status
    r.text = text
    r.headers = {}
    if retry_after is not None:
        r.headers["Retry-After"] = retry_after
    r.json.return_value = payload or {
        "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
        "usageMetadata": {"totalTokenCount": 1},
    }
    return r


def _client():
    return GeminiLLM(api_key="test-key")


def test_success_first_try_no_sleep():
    with mock.patch("specpipe.llm.requests.post",
                    return_value=_resp(200)) as post, \
         mock.patch("specpipe.llm.time.sleep") as sleep:
        assert _client().complete("sys", "user") == "{}"
        assert post.call_count == 1
        sleep.assert_not_called()


def test_429_retries_then_succeeds_honoring_retry_after():
    seq = [_resp(429, retry_after="7"), _resp(200)]
    with mock.patch("specpipe.llm.requests.post", side_effect=seq) as post, \
         mock.patch("specpipe.llm.time.sleep") as sleep:
        assert _client().complete("sys", "user") == "{}"
        assert post.call_count == 2
        sleep.assert_called_once_with(7.0)


def test_429_exhausts_bounded_attempts_and_raises():
    with mock.patch("specpipe.llm.requests.post",
                    return_value=_resp(429)) as post, \
         mock.patch("specpipe.llm.time.sleep"):
        with pytest.raises(LLMError, match="429"):
            _client().complete("sys", "user")
        assert post.call_count == MAX_ATTEMPTS


def test_503_is_retryable_400_is_not():
    with mock.patch("specpipe.llm.requests.post",
                    return_value=_resp(503)) as post, \
         mock.patch("specpipe.llm.time.sleep"):
        with pytest.raises(LLMError, match="503"):
            _client().complete("sys", "user")
        assert post.call_count == MAX_ATTEMPTS
    with mock.patch("specpipe.llm.requests.post",
                    return_value=_resp(400)) as post, \
         mock.patch("specpipe.llm.time.sleep") as sleep:
        with pytest.raises(LLMError, match="400"):
            _client().complete("sys", "user")
        assert post.call_count == 1
        sleep.assert_not_called()
