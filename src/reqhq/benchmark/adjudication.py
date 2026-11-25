"""Exact Cohen's kappa and fail-closed inter-rater adjudication utilities."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
import json
from pathlib import Path
from typing import Any

from reqhq.benchmark.models import AnnotationItem, KappaReport


def _compute_agreement_components(
    labels_a: Sequence[str],
    labels_b: Sequence[str],
) -> tuple[float, float, float, int]:
    """Return (kappa, observed_agreement, expected_agreement, agreed_count)."""
    if len(labels_a) != len(labels_b):
        raise ValueError(
            f"Label sequences must have equal length (got {len(labels_a)} vs {len(labels_b)})"
        )
    n = len(labels_a)
    if n == 0:
        raise ValueError("Label sequences must be non-empty")

    agreed = sum(1 for a, b in zip(labels_a, labels_b, strict=True) if a == b)
    p_o = agreed / n

    counts_a = Counter(labels_a)
    counts_b = Counter(labels_b)
    categories = sorted(set(counts_a.keys()) | set(counts_b.keys()))
    p_e = sum((counts_a[cat] / n) * (counts_b[cat] / n) for cat in categories)

    if p_e >= 1.0:
        kappa = 1.0
    else:
        raw_kappa = (p_o - p_e) / (1.0 - p_e)
        kappa = max(-1.0, min(1.0, raw_kappa))

    p_o_clamped = max(0.0, min(1.0, p_o))
    p_e_clamped = max(0.0, min(1.0, p_e))
    return kappa, p_o_clamped, p_e_clamped, agreed


def cohens_kappa(labels_a: Sequence[str], labels_b: Sequence[str]) -> float:
    """Compute exact Cohen's kappa in [-1.0, 1.0], returning 1.0 on perfect agreement."""
    kappa, _, _, _ = _compute_agreement_components(labels_a, labels_b)
    return kappa


def _index_verified_annotations(
    records: Sequence[AnnotationItem],
    *,
    rater_name: str,
) -> dict[str, AnnotationItem]:
    if not records:
        raise ValueError(f"Annotation set {rater_name!r} must not be empty")

    indexed: dict[str, AnnotationItem] = {}
    for item in records:
        if item.skipped:
            raise ValueError(
                f"Fail-closed refusal: item {item.item_id!r} in {rater_name!r} is marked skipped=True"
            )
        if not item.verified:
            raise ValueError(
                f"Fail-closed refusal: item {item.item_id!r} in {rater_name!r} is marked verified=False"
            )
        if item.item_id in indexed:
            raise ValueError(
                f"Fail-closed refusal: duplicate item_id {item.item_id!r} in {rater_name!r}"
            )
        indexed[item.item_id] = item
    return indexed


def adjudicate_annotations(
    records_a: Sequence[AnnotationItem],
    records_b: Sequence[AnnotationItem],
) -> KappaReport:
    """Fail-closed inter-rater adjudication refusing skipped, unverified, or mismatched items."""
    map_a = _index_verified_annotations(records_a, rater_name="rater_a")
    map_b = _index_verified_annotations(records_b, rater_name="rater_b")

    ids_a = set(map_a.keys())
    ids_b = set(map_b.keys())
    if ids_a != ids_b:
        only_a = sorted(ids_a - ids_b)
        only_b = sorted(ids_b - ids_a)
        raise ValueError(
            "Fail-closed refusal: mismatched item_id sets between raters "
            f"(only_in_a={only_a}, only_in_b={only_b})"
        )

    shared_ids = sorted(ids_a)
    labels_a = [map_a[item_id].label for item_id in shared_ids]
    labels_b = [map_b[item_id].label for item_id in shared_ids]

    kappa, p_o, p_e, agreed = _compute_agreement_components(labels_a, labels_b)
    disagreed_ids = [
        item_id for item_id in shared_ids if map_a[item_id].label != map_b[item_id].label
    ]

    return KappaReport(
        kappa=kappa,
        observed_agreement=p_o,
        expected_agreement=p_e,
        shared_count=len(shared_ids),
        agreed_count=agreed,
        disagreed_ids=disagreed_ids,
    )


def _load_annotation_items_from_file(path: Path) -> list[AnnotationItem]:
    if not path.exists():
        raise FileNotFoundError(f"Annotation file not found: {path}")

    raw_text = path.read_text(encoding="utf-8").strip()
    if not raw_text:
        raise ValueError(f"Annotation file is empty: {path}")

    raw_entries: list[Any]
    if path.suffix.lower() == ".jsonl":
        raw_entries = [json.loads(line) for line in raw_text.splitlines() if line.strip()]
    else:
        payload = json.loads(raw_text)
        if isinstance(payload, list):
            raw_entries = payload
        elif isinstance(payload, dict):
            if isinstance(payload.get("items"), list):
                raw_entries = payload["items"]
            elif isinstance(payload.get("annotations"), list):
                raw_entries = payload["annotations"]
            else:
                raise ValueError(
                    f"Annotation JSON object in {path} must contain an 'items' or 'annotations' list"
                )
        else:
            raise ValueError(f"Unsupported JSON structure in annotation file: {path}")

    return [AnnotationItem.model_validate(entry) for entry in raw_entries]


def compute_kappa_from_files(file_a: Path, file_b: Path) -> KappaReport:
    """Load two annotation JSON/JSONL files and run fail-closed Cohen's kappa adjudication."""
    records_a = _load_annotation_items_from_file(file_a)
    records_b = _load_annotation_items_from_file(file_b)
    return adjudicate_annotations(records_a, records_b)


__all__ = [
    "adjudicate_annotations",
    "cohens_kappa",
    "compute_kappa_from_files",
]
