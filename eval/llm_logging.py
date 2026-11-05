"""Per-call cost and latency logging for any LLM client (proposal Stage 1).

H2's efficiency claim needs cost and latency numbers from the start, so
every evaluation run wraps its LLM in LoggingLLM. The current
src/specpipe/llm.py interface returns only the completion text, so token
counts are NOT available here; this wrapper records call latency and
character volumes (a crude proxy). True token usage requires the LLM
client to expose API usage metadata - tracked as a coordinator interface
request in docs/evaluation.md.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict


@dataclass
class CallRecord:
    n: int
    latency_s: float
    chars_in: int
    chars_out: int
    token_usage: dict | None = None  # client usageMetadata when exposed

    def to_dict(self) -> dict:
        return asdict(self)


class LoggingLLM:
    """Wraps any object with .complete(system, user) and records each call.
    When the inner client exposes .last_usage (GeminiLLM does, from the
    API's usageMetadata), per-call token counts are recorded too."""

    def __init__(self, inner):
        self._inner = inner
        self.calls: list = []
        self._n = 0

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        self._n += 1
        start = time.monotonic()
        out = self._inner.complete(system, user, temperature=temperature)
        self.calls.append(CallRecord(
            n=self._n,
            latency_s=round(time.monotonic() - start, 6),
            chars_in=len(system) + len(user),
            chars_out=len(out or ""),
            token_usage=getattr(self._inner, "last_usage", None),
        ))
        return out

    def summary(self) -> dict:
        return {
            "calls": len(self.calls),
            "total_latency_s": round(sum(c.latency_s for c in self.calls), 6),
            "total_chars_in": sum(c.chars_in for c in self.calls),
            "total_chars_out": sum(c.chars_out for c in self.calls),
            "token_usage": [c.token_usage for c in self.calls
                            if c.token_usage] or None,
        }
