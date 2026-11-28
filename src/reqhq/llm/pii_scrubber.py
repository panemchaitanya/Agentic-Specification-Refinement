"""Reversible PII and secret scrubber for outbound cloud LLM requests."""

from __future__ import annotations

import re
from typing import Any, TypeVar

T = TypeVar("T")


_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 1. Credentialed URLs (must precede email & IPv4 patterns to avoid partial redaction)
    (
        "CREDENTIAL_URL",
        re.compile(
            r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:[^\s/@]+@[^\s\"'<>]+"
        ),
    ),
    # 2. Bearer tokens and common provider API keys
    (
        "API_KEY",
        re.compile(
            r"(?:"
            r"\bBearer\s+[A-Za-z0-9\-._~+/]+=*"
            r"|\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{16,}"
            r"|\bAIza[0-9A-Za-z\-_]{20,}"
            r"|\bgsk_[A-Za-z0-9]{16,}"
            r"|\bgh[pousr]_[A-Za-z0-9]{16,}"
            r"|\bAKIA[0-9A-Z]{16}\b"
            r"|\bxox[baprs]-[A-Za-z0-9\-]{10,}"
            r")"
        ),
    ),
    # 3. Email addresses
    (
        "EMAIL",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ),
    # 4. US Social Security Numbers
    (
        "SSN",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    ),
    # 5. Credit card numbers (16-digit grouped or 15-digit Amex)
    (
        "CREDIT_CARD",
        re.compile(
            r"(?<!\d)(?:"
            r"(?:\d{4}[- ]?){3}\d{4}"
            r"|3[47]\d{2}[- ]?\d{6}[- ]?\d{5}"
            r")(?!\d)"
        ),
    ),
    # 6. Phone numbers (US and international formats)
    (
        "PHONE",
        re.compile(
            r"(?<!\w)(?:"
            r"(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]\d{3}[-.\s]\d{4}"
            r"|\+\d{1,3}[-.\s]\(?\d{2,4}\)?[-.\s]\d{3,4}[-.\s]\d{3,4}"
            r")\b"
        ),
    ),
    # 7. IPv4 addresses
    (
        "IPV4",
        re.compile(
            r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
            r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"
        ),
    ),
]


class PIIScrubber:
    """Deterministic, reversible PII and secret scrubber for cloud LLM calls."""

    def __init__(
        self,
        extra_patterns: list[tuple[str, re.Pattern[str]]] | None = None,
    ) -> None:
        self._patterns: list[tuple[str, re.Pattern[str]]] = list(_PATTERNS)
        if extra_patterns:
            self._patterns.extend(extra_patterns)

    def scrub(
        self,
        text: str,
        mapping: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, str]]:
        """Replace PII and credentials with deterministic placeholders.

        Returns `(scrubbed_text, mapping)` where `mapping` maps placeholder
        tokens (e.g. `<EMAIL_1>`) back to their original values. Passing an
        existing `mapping` preserves placeholder numbering across multiple fields
        (such as system + user prompts) in the same request.
        """
        placeholder_to_value: dict[str, str] = dict(mapping) if mapping else {}
        value_to_placeholder: dict[str, str] = {
            v: k for k, v in placeholder_to_value.items()
        }
        counters: dict[str, int] = {}
        for placeholder in placeholder_to_value:
            match = re.fullmatch(r"<([A-Z0-9_]+)_(\d+)>", placeholder)
            if match:
                label, idx = match.group(1), int(match.group(2))
                counters[label] = max(counters.get(label, 0), idx)

        scrubbed = text
        for label, pattern in self._patterns:
            def _replace(m: re.Match[str], _label: str = label) -> str:
                original = m.group(0)
                existing = value_to_placeholder.get(original)
                if existing is not None:
                    return existing
                next_idx = counters.get(_label, 0) + 1
                counters[_label] = next_idx
                placeholder = f"<{_label}_{next_idx}>"
                placeholder_to_value[placeholder] = original
                value_to_placeholder[original] = placeholder
                return placeholder

            scrubbed = pattern.sub(_replace, scrubbed)

        return scrubbed, placeholder_to_value

    def restore(self, data: T, mapping: dict[str, str]) -> T:
        """Restore placeholders in strings or recursively inside dicts/lists/tuples."""
        if not mapping:
            return data
        return self._restore_recursive(data, mapping)  # type: ignore[return-value]

    def _restore_string(self, text: str, mapping: dict[str, str]) -> str:
        # Sort longest placeholders first so <EMAIL_10> is restored before <EMAIL_1>.
        restored = text
        for placeholder in sorted(mapping.keys(), key=len, reverse=True):
            if placeholder in restored:
                restored = restored.replace(placeholder, mapping[placeholder])
        return restored

    def _restore_recursive(self, obj: Any, mapping: dict[str, str]) -> Any:
        if isinstance(obj, str):
            return self._restore_string(obj, mapping)
        if isinstance(obj, dict):
            return {
                self._restore_recursive(k, mapping): self._restore_recursive(v, mapping)
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [self._restore_recursive(item, mapping) for item in obj]
        if isinstance(obj, tuple):
            return tuple(self._restore_recursive(item, mapping) for item in obj)
        return obj
