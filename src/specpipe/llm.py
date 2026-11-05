"""LLM client abstraction.

GeminiLLM calls the Gemini REST API (key from GEMINI_API_KEY, model from
GEMINI_MODEL). MockLLM is a deterministic offline stand-in so the pipeline,
tests and UI run without a key; it never claims model quality.
"""
from __future__ import annotations

import json
import os

import requests

DEFAULT_MODEL = "gemini-3.1-flash-lite"


class LLMError(Exception):
    pass


class GeminiLLM:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        # Populated from each API response's usageMetadata (H2 token cost);
        # None until the first successful call. Read, don't mutate.
        self.last_usage: dict | None = None

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{self.model}:generateContent")
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
            },
        }
        resp = requests.post(url, headers={"x-goog-api-key": self.api_key},
                             json=body, timeout=90)
        if resp.status_code != 200:
            raise LLMError(f"Gemini HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        self.last_usage = data.get("usageMetadata")
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Unexpected Gemini response shape: {exc}") from exc


def _extract_json(text: str):
    """Parse JSON from an LLM reply, tolerating code fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return json.loads(text)


class MockLLM:
    """Deterministic offline LLM for tests and demos without a key.

    Replies are rule-based on the system-prompt role marker; output quality
    is not representative of a real model and is never used in evaluation.
    """

    last_usage = None  # no token usage in the deterministic mock

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        if "ROLE: orchestrator" in system:
            return json.dumps(self._mock_frs(user))
        if "ROLE: security_critic" in system:
            return json.dumps(self._mock_security(user))
        if "ROLE: architecture_critic" in system:
            return json.dumps(self._mock_architecture(user))
        if "ROLE: performance_critic" in system:
            return json.dumps(self._mock_performance(user))
        return "[]"

    def _sentences(self, prd: str) -> list:
        import re
        return [s.strip() for s in re.split(r"(?<=[.!?])\s+", prd) if s.strip()]

    def _mock_frs(self, user: str) -> list:
        frs = []
        for i, sent in enumerate(self._sentences(user), start=1):
            low = sent.lower()
            if any(k in low for k in ("user", "shall", "must", "allow",
                                      "support", "enable", "let")):
                frs.append({"text": sent, "prd_sentence": sent})
        if not frs and self._sentences(user):
            s = self._sentences(user)[0]
            frs.append({"text": s, "prd_sentence": s})
        return {"frs": frs}

    def _mock_security(self, user: str) -> list:
        low = user.lower()
        out = []
        if "encrypt" not in low:
            out.append({
                "category": "security", "kind": "missing_context",
                "issue": "No statement on encryption of stored user data",
                "suggestion": "All stored personal data shall be encrypted at rest.",
                "prd_sentence": self._sentences(user)[0] if self._sentences(user) else "",
                "reason": "PRD mentions user data without storage protection"})
        if "password" in low and ("lock" not in low and "attempt" not in low):
            out.append({
                "category": "security", "kind": "missing_context",
                "issue": "No brute-force protection stated for password login",
                "suggestion": "Accounts shall lock after 5 failed login attempts for 15 minutes.",
                "prd_sentence": next((s for s in self._sentences(user)
                                      if "password" in s.lower()), ""),
                "reason": "Credential stuffing risk"})
        return {"findings": out}

    def _mock_architecture(self, user: str) -> list:
        low = user.lower()
        out = []
        if "database" not in low and "store" in low:
            out.append({
                "category": "reliability", "kind": "vague",
                "issue": "Storage mentioned without durability requirement",
                "suggestion": "Stored records shall survive a service restart without loss.",
                "prd_sentence": next((s for s in self._sentences(user)
                                      if "store" in s.lower()), ""),
                "reason": "Data durability undefined"})
        return {"findings": out}

    def _mock_performance(self, user: str) -> list:
        low = user.lower()
        out = []
        if "fast" in low or "quick" in low:
            sent = next((s for s in self._sentences(user)
                         if "fast" in s.lower() or "quick" in s.lower()), "")
            out.append({
                "category": "performance", "kind": "vague",
                "issue": "'fast' used without a numeric target",
                "suggestion": "The action shall complete within 2 seconds at p95.",
                "prd_sentence": sent,
                "reason": "Unquantified performance wording"})
        if not any(k in low for k in ("users", "load", "concurrent")):
            out.append({
                "category": "performance", "kind": "missing_context",
                "issue": "Expected user load not stated",
                "suggestion": "The system shall support the agreed peak concurrent users.",
                "prd_sentence": self._sentences(user)[0] if self._sentences(user) else "",
                "reason": "Cannot size performance requirements without load"})
        return {"findings": out}
