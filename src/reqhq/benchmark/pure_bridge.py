"""Read-only, fail-closed bridge for Stage 0 frozen PURE & PROMISE NFR datasets."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from reqhq.benchmark.models import FrozenGoldDocument

_FROZEN_STATUS_PREFIX = "FROZEN - human-verified"
_HUMAN_ADDED_PREFIX = "human-added:"
_VERIFIED_LLM_SOURCE = "llm-suggestion+human-verify"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_verified_requirement_source(source: Any) -> bool:
    if not isinstance(source, str):
        return False
    cleaned = source.strip()
    if cleaned == _VERIFIED_LLM_SOURCE:
        return True
    if cleaned.startswith(_HUMAN_ADDED_PREFIX) and len(cleaned) > len(_HUMAN_ADDED_PREFIX):
        return True
    return False


def validate_gold_record_dict(
    raw: dict[str, Any],
    *,
    split: Literal["dev", "test"] = "dev",
) -> FrozenGoldDocument:
    """Strictly validate a PURE gold JSON dict; fail-closed on unverified/skipped/skeleton data."""
    if not isinstance(raw, dict):
        raise ValueError("Gold record must be a JSON object")

    item_id = raw.get("item_id")
    prd_path = raw.get("prd_path")
    provenance = raw.get("provenance")
    gold_requirements = raw.get("gold_requirements")
    implied_categories = raw.get("implied_categories")

    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("Gold record missing non-empty 'item_id'")
    if not isinstance(prd_path, str) or not prd_path.strip():
        raise ValueError(f"Gold record {item_id!r} missing non-empty 'prd_path'")
    if not isinstance(provenance, dict):
        raise ValueError(f"Gold record {item_id!r} missing 'provenance' object")

    status = provenance.get("status")
    if not isinstance(status, str) or not status.startswith(_FROZEN_STATUS_PREFIX):
        raise ValueError(
            f"Fail-closed refusal for {item_id!r}: provenance.status must start with "
            f"{_FROZEN_STATUS_PREFIX!r}, got {status!r}"
        )

    verified_by_raw = provenance.get("verified_by")
    if not isinstance(verified_by_raw, list) or not verified_by_raw:
        raise ValueError(
            f"Fail-closed refusal for {item_id!r}: provenance.verified_by must be a non-empty list"
        )
    verified_by = [str(v).strip() for v in verified_by_raw if isinstance(v, str) and v.strip()]
    if len(verified_by) != len(verified_by_raw):
        raise ValueError(
            f"Fail-closed refusal for {item_id!r}: provenance.verified_by contains blank/non-string entries"
        )

    if provenance.get("implied_categories_verified") is not True:
        raise ValueError(
            f"Fail-closed refusal for {item_id!r}: provenance.implied_categories_verified must be True"
        )

    excluded = provenance.get("excluded")
    if isinstance(excluded, dict) and "skipped" in excluded:
        raise ValueError(
            f"Fail-closed refusal for {item_id!r}: unresolved skipped items in provenance.excluded"
        )

    source_sha256 = provenance.get("source_document_sha256")
    if not isinstance(source_sha256, str) or not source_sha256.strip():
        raise ValueError(
            f"Fail-closed refusal for {item_id!r}: missing provenance.source_document_sha256"
        )

    if not isinstance(gold_requirements, list) or not gold_requirements:
        raise ValueError(
            f"Fail-closed refusal for {item_id!r}: gold_requirements must be a non-empty list"
        )

    validated_reqs: list[dict[str, str]] = []
    for idx, req in enumerate(gold_requirements):
        if not isinstance(req, dict):
            raise ValueError(
                f"Fail-closed refusal for {item_id!r}: requirement at index {idx} must be an object"
            )
        req_id = req.get("id")
        kind = req.get("kind")
        text = req.get("text")
        source = req.get("source")
        if not all(isinstance(v, str) and v.strip() for v in (req_id, kind, text)):
            raise ValueError(
                f"Fail-closed refusal for {item_id!r}: requirement at index {idx} has missing id/kind/text"
            )
        if not _is_verified_requirement_source(source):
            raise ValueError(
                f"Fail-closed refusal for {item_id!r}: requirement {req_id!r} has unverified "
                f"source {source!r} (expected {_VERIFIED_LLM_SOURCE!r} or 'human-added:<annotator>')"
            )
        validated_reqs.append(
            {
                "id": str(req_id).strip(),
                "kind": str(kind).strip(),
                "text": str(text).strip(),
                "source": str(source).strip(),
            }
        )

    clean_categories: list[str] = []
    if isinstance(implied_categories, list):
        clean_categories = [
            str(cat).strip() for cat in implied_categories if isinstance(cat, str) and cat.strip()
        ]

    return FrozenGoldDocument(
        item_id=item_id.strip(),
        split=split,
        prd_path=prd_path.strip(),
        source_sha256=source_sha256.strip(),
        status=status.strip(),
        verified_by=verified_by,
        implied_categories=clean_categories,
        requirements=validated_reqs,
    )


def load_frozen_pure_gold(
    frozen_root: Path = Path("data/frozen"),
    *,
    split: Literal["dev", "test"] = "dev",
    strict_fail_closed: bool = False,
) -> list[FrozenGoldDocument]:
    """Read-only loader returning strictly human-verified FROZEN PURE gold documents.

    Defaults to split="dev" so the frozen test split stays protected unless explicitly requested.
    When strict_fail_closed=True, raises ValueError on the first unverified/skeleton file;
    otherwise filters out unverified/incomplete files fail-closed.
    """
    if split not in ("dev", "test"):
        raise ValueError(f"Unsupported split {split!r}; expected 'dev' or 'test'")

    gold_dir = frozen_root / "gold" / split
    if not gold_dir.exists():
        raise FileNotFoundError(f"Frozen PURE gold directory not found: {gold_dir}")

    verified_docs: list[FrozenGoldDocument] = []
    for path in sorted(gold_dir.glob("*.gold.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        try:
            doc = validate_gold_record_dict(raw, split=split)
        except ValueError:
            if strict_fail_closed:
                raise
            continue
        verified_docs.append(doc)
    return verified_docs


def load_frozen_promise_manifest(
    frozen_root: Path = Path("data/frozen"),
    *,
    verify_hashes: bool = True,
) -> dict[str, Any]:
    """Read-only loader for data/frozen/promise_nfr/FREEZE.json with optional SHA-256 verification."""
    promise_dir = frozen_root / "promise_nfr"
    manifest_path = promise_dir / "FREEZE.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"PROMISE NFR FREEZE.json not found: {manifest_path}")

    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    for required_key in ("dataset", "files", "stats", "split_rule", "discipline"):
        if required_key not in manifest:
            raise ValueError(f"PROMISE FREEZE.json missing required key: {required_key!r}")

    if verify_hashes:
        files_meta = manifest["files"]
        for split_name in ("dev", "test"):
            entry = files_meta.get(split_name)
            if not isinstance(entry, dict) or "sha256" not in entry:
                raise ValueError(f"PROMISE FREEZE.json missing sha256 metadata for {split_name!r}")
            csv_path = promise_dir / f"{split_name}.csv"
            if not csv_path.exists():
                raise FileNotFoundError(f"PROMISE NFR split file missing: {csv_path}")
            actual_sha = _sha256_file(csv_path)
            expected_sha = str(entry["sha256"]).strip().lower()
            if actual_sha != expected_sha:
                raise ValueError(
                    f"SHA-256 mismatch for {csv_path}: expected {expected_sha}, got {actual_sha}"
                )

    return manifest


def load_frozen_promise_nfr_gold(
    frozen_root: Path = Path("data/frozen"),
    *,
    split: Literal["dev", "test"] = "dev",
    verify_hashes: bool = True,
) -> list[FrozenGoldDocument]:
    """Read-only loader grouping human-labeled PROMISE NFR rows by project into FrozenGoldDocuments.

    Defaults to split="dev" (11 projects, 437 requirements) so the 4 held-out test projects
    (185 requirements) remain untouched until explicit blinded evaluation.
    """
    if split not in ("dev", "test"):
        raise ValueError(f"Unsupported split {split!r}; expected 'dev' or 'test'")

    load_frozen_promise_manifest(frozen_root, verify_hashes=verify_hashes)
    csv_path = frozen_root / "promise_nfr" / f"{split}.csv"
    csv_sha = _sha256_file(csv_path)

    grouped: dict[int, list[dict[str, str]]] = {}
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            pid = int(str(row["project_id"]).strip())
            text = str(row["text"]).strip()
            category = str(row["category"]).strip() or "FR"
            if not text:
                continue
            grouped.setdefault(pid, []).append({"text": text, "category": category})

    docs: list[FrozenGoldDocument] = []
    for pid in sorted(grouped.keys()):
        rows = grouped[pid]
        reqs = [
            {
                "id": f"P{pid:02d}-R{idx:03d}",
                "kind": item["category"],
                "text": item["text"],
                "source": "human-added:promise-nfr-2007",
            }
            for idx, item in enumerate(rows, start=1)
        ]
        implied = sorted({item["category"] for item in rows if item["category"].upper() != "FR"})
        docs.append(
            FrozenGoldDocument(
                item_id=f"promise-nfr-proj-{pid:02d}",
                split=split,
                prd_path=f"data/frozen/promise_nfr/{split}.csv#project_{pid}",
                source_sha256=csv_sha,
                status="FROZEN - human-verified (PROMISE NFR, Cleland-Huang et al. 2007)",
                verified_by=["promise-nfr-2007"],
                implied_categories=implied,
                requirements=reqs,
            )
        )
    return docs


def load_frozen_pure_manifest(
    frozen_root: Path = Path("data/frozen"),
) -> dict[str, Any]:
    """Read-only loader for data/frozen/pure/FREEZE.json."""
    manifest_path = frozen_root / "pure" / "FREEZE.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"PURE FREEZE.json not found: {manifest_path}")
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    for required_key in ("dataset", "stats", "documents", "discipline"):
        if required_key not in manifest:
            raise ValueError(f"PURE FREEZE.json missing required key: {required_key!r}")
    return manifest


__all__ = [
    "load_frozen_promise_manifest",
    "load_frozen_promise_nfr_gold",
    "load_frozen_pure_gold",
    "load_frozen_pure_manifest",
    "validate_gold_record_dict",
]
