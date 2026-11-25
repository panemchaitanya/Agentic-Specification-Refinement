"""Deterministic 7-category error taxonomy builder across Phase 4 evaluation logs."""

from __future__ import annotations

import json
from pathlib import Path

from reqhq.analysis.models import (
    ErrorCategory,
    ErrorTaxonomyReport,
    TaxonomyCategorySummary,
)

_KEYWORD_RULES: tuple[tuple[ErrorCategory, tuple[str, ...]], ...] = (
    (
        ErrorCategory.QUOTA_FALLBACK,
        ("QUOTA", "429", "RATE_LIMIT", "RATE LIMIT", "RPD", "RPM", "FALLBACK", "THROTTL", "EXHAUST"),
    ),
    (
        ErrorCategory.ALLOY_VACUOUS,
        ("VACUOUS", "VACUITY", "TRIVIAL_SAT", "EMPTY_SCOPE"),
    ),
    (
        ErrorCategory.UNSAT_CONTRADICTION,
        ("UNSAT", "CONTRADICT", "INCONSIST", "CONFLICT", "DEADLOCK"),
    ),
    (
        ErrorCategory.ALLOY_SYNTAX,
        ("ALLOY", "SYNTAX", "PARSE_ERROR", ".ALS"),
    ),
    (
        ErrorCategory.PBT_COUNTEREXAMPLE,
        (
            "PBT",
            "HYPOTHESIS",
            "COUNTEREXAMPLE",
            "FALSIFY",
            "MUTANT",
            "MUTATION",
            "LOCK_TAMPER",
            "TAMPER",
            "ASSERTION",
        ),
    ),
    (
        ErrorCategory.ROUTING_AMBIGUITY,
        ("ROUTING", "ROUTE", "AMBIGU", "TRIAGE", "TRACK", "SOCIO", "ADR_RUBRIC"),
    ),
)


def classify_error_event(raw_event: str) -> ErrorCategory:
    """Deterministically map any raw error string into one of the 7 ErrorCategory values."""
    normalized = raw_event.strip().upper()
    for category in ErrorCategory:
        if category.value in normalized:
            return category
    for category, keywords in _KEYWORD_RULES:
        if any(kw in normalized for kw in keywords):
            return category
    return ErrorCategory.SCHEMA_DRIFT


def _discover_log_paths(eval_path: Path) -> list[Path]:
    """Resolve JSONL log files in deterministic order from eval_manifest.json or directory scan."""
    manifest_path = eval_path / "eval_manifest.json"
    resolved: list[Path] = []
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = None
        if isinstance(manifest, dict) and isinstance(manifest.get("log_files"), dict):
            seen: set[Path] = set()
            for key in sorted(manifest["log_files"]):
                rel_or_abs = str(manifest["log_files"][key])
                candidate = Path(rel_or_abs)
                if not candidate.is_absolute():
                    candidate = eval_path / candidate
                if candidate.is_file() and candidate not in seen:
                    resolved.append(candidate)
                    seen.add(candidate)
    if resolved:
        return resolved
    return sorted(p for p in eval_path.glob("*.jsonl") if p.is_file())


def build_error_taxonomy(
    eval_dir: Path | str,
    *,
    max_samples_per_category: int = 5,
) -> ErrorTaxonomyReport:
    """Aggregate all JSONL `error_events` in `eval_dir` into the 7-category ErrorTaxonomyReport."""
    eval_path = Path(eval_dir)
    counts: dict[ErrorCategory, int] = {cat: 0 for cat in ErrorCategory}
    samples: dict[ErrorCategory, list[str]] = {cat: [] for cat in ErrorCategory}

    for log_path in _discover_log_paths(eval_path):
        for line_no, raw_line in enumerate(
            log_path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            events = record.get("error_events")
            if not isinstance(events, list):
                continue
            sample_id = str(
                record.get("record_id") or record.get("stage") or f"{log_path.stem}:{line_no}"
            )
            for event in events:
                category = classify_error_event(str(event))
                counts[category] += 1
                bucket = samples[category]
                if len(bucket) < max_samples_per_category and sample_id not in bucket:
                    bucket.append(sample_id)

    total_failures = sum(counts.values())
    categories = [
        TaxonomyCategorySummary(
            category=cat,
            count=counts[cat],
            share_pct=round((counts[cat] / total_failures) * 100.0, 2) if total_failures > 0 else 0.0,
            sample_ids=samples[cat],
        )
        for cat in ErrorCategory
    ]
    return ErrorTaxonomyReport(total_failures=total_failures, categories=categories)


__all__ = [
    "build_error_taxonomy",
    "classify_error_event",
]
