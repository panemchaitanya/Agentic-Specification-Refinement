"""Tamper-evident release bundle packager and SHA-256 verifier for ReqHQ v2."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from reqhq.llm.model_registry import list_model_tiers
from reqhq.reporting.models import (
    ArtifactsManifest,
    BundleVerificationResult,
    ReleaseArtifactEntry,
    VerdictStatus,
)

MANIFEST_FILENAME = "artifacts_manifest.json"

_FROZEN_MANIFEST_PATHS: tuple[tuple[str, str], ...] = (
    ("pure/FREEZE.json", "pure/FREEZE.json"),
    ("promise_nfr/FREEZE.json", "promise_nfr/FREEZE.json"),
)


class BundleTamperError(RuntimeError):
    """Raised when a release bundle manifest or tracked artifact fails integrity verification."""


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def _classify_artifact(path: Path) -> str | None:
    name = path.name
    if name == MANIFEST_FILENAME:
        return None
    if name == "eval_manifest.json":
        return "eval_manifest"
    if name == "analysis_summary.json":
        return "analysis_summary"
    if name == "FINAL_REPORT.md":
        return "report_markdown"
    if name == "tables_latex.tex":
        return "report_latex"
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        return "layer_log"
    if suffix == ".svg":
        return "figure_svg"
    return None


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(loaded, dict):
        return loaded
    return None


def _collect_pinned_models() -> dict[str, str]:
    pinned: dict[str, str] = {}
    for spec in list_model_tiers():
        fallback = f", fallback={spec.fallback_model}" if spec.fallback_model else ""
        pinned[f"TIER_{int(spec.tier)}"] = f"{spec.model_name} ({spec.quantization}{fallback})"
    return pinned


def _collect_frozen_hashes(frozen_root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for key, rel in _FROZEN_MANIFEST_PATHS:
        target = frozen_root / rel
        if target.is_file():
            hashes[key] = _sha256_file(target)
    return hashes


def _derive_manifest_metadata(
    eval_meta: dict[str, Any] | None,
    analysis_meta: dict[str, Any] | None,
) -> tuple[str, str, int, VerdictStatus]:
    run_id = "unknown-run"
    run_mode = "mock"
    seed = 42

    if eval_meta is not None:
        if eval_meta.get("run_id"):
            run_id = str(eval_meta["run_id"])
        if eval_meta.get("run_mode"):
            run_mode = str(eval_meta["run_mode"]).strip().lower()
        if isinstance(eval_meta.get("seed"), int) and not isinstance(eval_meta.get("seed"), bool):
            seed = int(eval_meta["seed"])

    if analysis_meta is not None:
        if analysis_meta.get("run_id"):
            run_id = str(analysis_meta["run_id"])
        if analysis_meta.get("run_mode"):
            run_mode = str(analysis_meta["run_mode"]).strip().lower()
        if isinstance(analysis_meta.get("seed"), int) and not isinstance(
            analysis_meta.get("seed"), bool
        ):
            seed = int(analysis_meta["seed"])

    live_ok = run_mode == "live"
    if eval_meta is not None:
        eval_mode = str(eval_meta.get("run_mode", "")).strip().lower()
        eval_replicates = int(eval_meta.get("replicates", 0))
        eval_gold_verified = bool(eval_meta.get("gold_verified", False))
        live_ok = live_ok and (eval_mode == "live") and eval_gold_verified and (eval_replicates >= 2)
    if analysis_meta is not None:
        live_ok = (
            live_ok
            and not bool(analysis_meta.get("quarantined", True))
            and str(analysis_meta.get("verdict_status", "")).strip() == "VERIFIED_LIVE"
        )
    if eval_meta is None and analysis_meta is None:
        live_ok = False

    verdict_status: VerdictStatus = "VERIFIED_LIVE" if live_ok else "DRY_RUN_UNSCORED"
    return run_id, run_mode, seed, verdict_status


def _validate_safe_rel_path(rel_posix: str, bundle_root: Path) -> tuple[Path | None, str | None]:
    if "\\" in rel_posix or ":" in rel_posix:
        return None, f"Illegal path characters in artifact entry: {rel_posix!r}"

    rel_path = Path(rel_posix)
    if rel_path.is_absolute() or ".." in rel_path.parts or rel_posix.startswith("/"):
        return None, f"Illegal path traversal in artifact entry: {rel_posix!r}"

    candidate = bundle_root / rel_path
    has_symlink_component = candidate.is_symlink() or any(
        (bundle_root / parent).is_symlink()
        for parent in rel_path.parents
        if str(parent) != "."
    )
    resolved = candidate.resolve()
    if has_symlink_component or not resolved.is_relative_to(bundle_root):
        return None, f"Artifact entry escapes bundle root or traverses a symlink: {rel_posix!r}"
    return resolved, None


def package_release_bundle(
    input_dir: Path | str,
    out_dir: Path | str,
    *,
    frozen_root: Path = Path("data/frozen"),
) -> ArtifactsManifest:
    """Collect evaluation/report artifacts into out_dir and write artifacts_manifest.json."""
    in_path = Path(input_dir)
    if not in_path.is_dir():
        raise FileNotFoundError(f"Input directory does not exist: {in_path}")

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    out_resolved = out_path.resolve()

    collected: list[tuple[str, Path, str]] = []
    eval_meta: dict[str, Any] | None = None
    analysis_meta: dict[str, Any] | None = None

    for candidate in sorted(in_path.rglob("*")):
        if not candidate.is_file() or candidate.is_symlink():
            continue
        category = _classify_artifact(candidate)
        if category is None:
            continue
        rel_posix = candidate.relative_to(in_path).as_posix()
        resolved_candidate = candidate.resolve()
        collected.append((rel_posix, resolved_candidate, category))

        if category == "eval_manifest" and eval_meta is None:
            eval_meta = _read_json_dict(resolved_candidate)
        elif category == "analysis_summary" and analysis_meta is None:
            analysis_meta = _read_json_dict(resolved_candidate)

    entries: list[ReleaseArtifactEntry] = []
    for rel_posix, src_file, category in sorted(collected, key=lambda item: item[0]):
        dst_file = (out_resolved / Path(rel_posix)).resolve()
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        if src_file != dst_file:
            shutil.copyfile(src_file, dst_file)
        entries.append(
            ReleaseArtifactEntry(
                rel_path=rel_posix,
                sha256=_sha256_file(dst_file),
                bytes=dst_file.stat().st_size,
                category=category,
            )
        )

    run_id, run_mode, seed, verdict_status = _derive_manifest_metadata(eval_meta, analysis_meta)
    manifest = ArtifactsManifest(
        schema_version="1.0.0",
        run_id=run_id,
        run_mode=run_mode,
        verdict_status=verdict_status,
        created_at=datetime.now(timezone.utc).isoformat(),
        seed=seed,
        pinned_models=_collect_pinned_models(),
        frozen_dataset_hashes=_collect_frozen_hashes(Path(frozen_root)),
        artifacts=entries,
    )

    manifest_path = out_resolved / MANIFEST_FILENAME
    payload = json.dumps(manifest.model_dump(mode="json"), indent=2) + "\n"
    with manifest_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)

    return manifest


def verify_bundle(
    manifest_path: Path | str,
    *,
    raise_on_error: bool = False,
) -> BundleVerificationResult:
    """Verify every tracked file in artifacts_manifest.json for presence, path safety, size, and SHA-256."""
    path = Path(manifest_path)
    if path.is_dir():
        path = path / MANIFEST_FILENAME

    if not path.is_file():
        err = f"Release manifest not found: {path}"
        result = BundleVerificationResult(
            valid=False,
            checked_count=0,
            missing_files=[],
            mismatched_files=[],
            errors=[err],
        )
        if raise_on_error:
            raise BundleTamperError(err)
        return result

    try:
        raw_data = json.loads(path.read_text(encoding="utf-8"))
        manifest = ArtifactsManifest.model_validate(raw_data)
    except (json.JSONDecodeError, ValidationError, OSError) as exc:
        err = f"Invalid release manifest {path.name}: {exc}"
        result = BundleVerificationResult(
            valid=False,
            checked_count=0,
            missing_files=[],
            mismatched_files=[],
            errors=[err],
        )
        if raise_on_error:
            raise BundleTamperError(err) from exc
        return result

    bundle_root = path.parent.resolve()
    missing_files: list[str] = []
    mismatched_files: list[str] = []
    errors: list[str] = []
    seen_paths: set[str] = set()
    checked_count = 0

    for entry in manifest.artifacts:
        checked_count += 1
        if entry.rel_path in seen_paths:
            errors.append(f"Duplicate artifact entry in manifest: {entry.rel_path!r}")
            continue
        seen_paths.add(entry.rel_path)

        resolved, path_err = _validate_safe_rel_path(entry.rel_path, bundle_root)
        if path_err is not None or resolved is None:
            errors.append(path_err or f"Invalid path: {entry.rel_path!r}")
            continue

        if not resolved.is_file():
            missing_files.append(entry.rel_path)
            errors.append(f"Tracked artifact missing: {entry.rel_path!r}")
            continue

        actual_bytes = resolved.stat().st_size
        actual_sha256 = _sha256_file(resolved)
        if actual_bytes != entry.bytes or actual_sha256 != entry.sha256:
            mismatched_files.append(entry.rel_path)
            errors.append(
                f"Artifact tamper detected for {entry.rel_path!r}: "
                f"expected ({entry.bytes} bytes, {entry.sha256}), "
                f"got ({actual_bytes} bytes, {actual_sha256})"
            )

    valid = not missing_files and not mismatched_files and not errors
    result = BundleVerificationResult(
        valid=valid,
        checked_count=checked_count,
        missing_files=missing_files,
        mismatched_files=mismatched_files,
        errors=errors,
    )
    if not valid and raise_on_error:
        raise BundleTamperError("; ".join(errors))
    return result


__all__ = [
    "MANIFEST_FILENAME",
    "BundleTamperError",
    "package_release_bundle",
    "verify_bundle",
]
