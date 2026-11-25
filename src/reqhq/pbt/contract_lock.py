"""Fail-closed SHA-256 contract locking for immutable PBT specifications and test suites."""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
from pathlib import Path
import re

_LOCK_LINE_RE = re.compile(r"^([0-9a-f]{64})  (.+)$")


class ContractTamperError(RuntimeError):
    """Raised when a locked PBT contract file is missing, malformed, or tampered with."""


def _sha256_bytes(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def _normalize_relative_posix(root_resolved: Path, target: Path) -> str:
    target_resolved = target.resolve()
    try:
        rel = target_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ContractTamperError(
            f"Locked file {target!s} escapes lock root {root_resolved!s}"
        ) from exc
    if not target_resolved.is_file():
        raise ContractTamperError(f"Locked file does not exist or is not a file: {target!s}")
    return rel.as_posix()


def write_contract_lock(
    root_dir: Path,
    files: Sequence[Path],
    lock_filename: str = "pbt_lock.sha256",
) -> Path:
    """Write a deterministic LF-terminated SHA-256 manifest locking the given files."""
    if not files:
        raise ContractTamperError("Cannot write contract lock with an empty file list")

    root_path = Path(root_dir)
    root_path.mkdir(parents=True, exist_ok=True)
    root_resolved = root_path.resolve()

    entries: dict[str, str] = {}
    for file_path in files:
        candidate = Path(file_path)
        if not candidate.is_absolute():
            candidate = root_path / candidate
        rel_posix = _normalize_relative_posix(root_resolved, candidate)
        entries[rel_posix] = _sha256_bytes(candidate.resolve())

    lock_path = root_path / lock_filename
    lines = [f"{entries[rel_path]}  {rel_path}" for rel_path in sorted(entries)]
    content = "\n".join(lines) + "\n"
    with lock_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    return lock_path


def verify_contract_lock(
    root_dir: Path,
    lock_filename: str = "pbt_lock.sha256",
) -> dict[str, str]:
    """Verify every SHA-256 entry in the contract lock file, failing closed on any tamper."""
    root_path = Path(root_dir)
    root_resolved = root_path.resolve()
    lock_path = root_path / lock_filename

    if not lock_path.is_file():
        raise ContractTamperError(f"Contract lock file missing: {lock_path!s}")

    raw_text = lock_path.read_text(encoding="utf-8")
    verified: dict[str, str] = {}

    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        match = _LOCK_LINE_RE.match(raw_line)
        if not match:
            raise ContractTamperError(
                f"Malformed contract lock entry at {lock_path.name}:{line_no}: {raw_line!r}"
            )
        expected_digest, rel_posix = match.group(1), match.group(2)

        if rel_posix in verified:
            raise ContractTamperError(
                f"Duplicate lock entry for {rel_posix!r} at {lock_path.name}:{line_no}"
            )

        rel_path = Path(rel_posix)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            raise ContractTamperError(
                f"Illegal path traversal in lock entry {rel_posix!r} at {lock_path.name}:{line_no}"
            )

        target_path = (root_path / rel_path).resolve()
        try:
            target_path.relative_to(root_resolved)
        except ValueError as exc:
            raise ContractTamperError(
                f"Lock entry {rel_posix!r} resolves outside lock root {root_resolved!s}"
            ) from exc

        if not target_path.is_file():
            raise ContractTamperError(f"Locked contract file is missing: {rel_posix!r}")

        actual_digest = _sha256_bytes(target_path)
        if actual_digest != expected_digest:
            raise ContractTamperError(
                f"Contract tamper detected in {rel_posix!r}: "
                f"expected {expected_digest}, got {actual_digest}"
            )
        verified[rel_posix] = actual_digest

    if not verified:
        raise ContractTamperError(f"Contract lock file contains zero entries: {lock_path!s}")

    return verified


__all__ = [
    "ContractTamperError",
    "verify_contract_lock",
    "write_contract_lock",
]
