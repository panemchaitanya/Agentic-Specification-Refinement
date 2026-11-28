"""Immutable local SHA-256 snapshot storage for ingested source documents."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from reqhq.ingestion.connectors import SourceDocument

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class SnapshotIntegrityError(RuntimeError):
    """Raised when a snapshot is missing, tampered with, or has a hash collision."""


def sanitize_filename(filename: str) -> str:
    """Convert an arbitrary filename into a safe single path component."""
    base = Path(filename.replace("\\", "/")).name.strip()
    if not base or base in {".", ".."}:
        return "document.bin"
    cleaned = _SAFE_NAME_RE.sub("_", base).strip("._")
    if not cleaned:
        return "document.bin"
    return cleaned


def _validate_session_id(session_id: str) -> str:
    if not _SAFE_SESSION_RE.match(session_id) or ".." in session_id:
        raise ValueError(
            f"Invalid session_id {session_id!r}; must match {_SAFE_SESSION_RE.pattern} "
            "and contain no path traversal."
        )
    return session_id


class SnapshotRecord(BaseModel):
    """Metadata manifest for an immutable local document snapshot."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)
    original_filename: str = Field(..., min_length=1)
    safe_filename: str = Field(..., min_length=1)
    source_uri: str = Field(..., min_length=1)
    source_type: str = Field(default="local", min_length=1)
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(..., ge=0)
    snapshot_path: Path
    manifest_path: Path
    created_at: str = Field(..., min_length=1)
    immutable: bool = True

    @property
    def filename(self) -> str:
        """Return the original source filename for convenient display/CLI compatibility."""
        return self.original_filename


class SnapshotStore:
    """Manages immutable SHA-256 content-addressed snapshots per session."""

    def __init__(self, sessions_dir: Path | str = Path(".reqhq_sessions")) -> None:
        self.sessions_dir = Path(sessions_dir)

    def snapshots_dir(self, session_id: str) -> Path:
        """Return the snapshot directory path for ``session_id``."""
        valid_id = _validate_session_id(session_id)
        return self.sessions_dir / valid_id / "snapshots"

    def save_snapshot(
        self,
        session_id: str,
        filename: str,
        content: bytes,
        source_uri: str,
        source_type: str = "local",
    ) -> SnapshotRecord:
        """Persist an immutable snapshot and JSON manifest for ``content``.

        Path layout: ``<sessions_dir>/<session_id>/snapshots/<sha256[:12]>_<safe_filename>``.
        """
        valid_session = _validate_session_id(session_id)
        digest = hashlib.sha256(content).hexdigest()
        safe_name = sanitize_filename(filename)
        snapshot_id = f"{digest[:12]}_{safe_name}"

        snap_dir = self.snapshots_dir(valid_session)
        snap_dir.mkdir(parents=True, exist_ok=True)

        snapshot_path = snap_dir / snapshot_id
        manifest_path = snap_dir / f"{snapshot_id}.manifest.json"

        if snapshot_path.exists():
            existing_bytes = snapshot_path.read_bytes()
            existing_digest = hashlib.sha256(existing_bytes).hexdigest()
            if existing_digest != digest:
                raise SnapshotIntegrityError(
                    f"Immutable snapshot integrity violation at {snapshot_path}: "
                    f"expected SHA-256 {digest}, found {existing_digest}."
                )
            if manifest_path.exists():
                try:
                    existing_record = SnapshotRecord.model_validate_json(
                        manifest_path.read_text(encoding="utf-8")
                    )
                except Exception as exc:
                    raise SnapshotIntegrityError(
                        f"Corrupted snapshot manifest at {manifest_path}: {exc}"
                    ) from exc
                if (
                    existing_record.sha256 != digest
                    or existing_record.size_bytes != len(content)
                ):
                    raise SnapshotIntegrityError(
                        f"Snapshot manifest digest mismatch at {manifest_path}: "
                        f"expected {digest}, found {existing_record.sha256}."
                    )
                return existing_record
        else:
            snapshot_path.write_bytes(content)
            # Mark read-only so accidental in-place mutation fails at the OS layer.
            os.chmod(snapshot_path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)

        record = SnapshotRecord(
            snapshot_id=snapshot_id,
            session_id=valid_session,
            original_filename=filename,
            safe_filename=safe_name,
            source_uri=source_uri,
            source_type=source_type,
            sha256=digest,
            size_bytes=len(content),
            snapshot_path=snapshot_path,
            manifest_path=manifest_path,
            created_at=datetime.now(timezone.utc).isoformat(),
            immutable=True,
        )
        manifest_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        return record

    def save_document(self, session_id: str, document: SourceDocument) -> SnapshotRecord:
        """Convenience wrapper saving a ``SourceDocument`` directly."""
        return self.save_snapshot(
            session_id=session_id,
            filename=document.filename,
            content=document.content,
            source_uri=document.source_uri,
            source_type=document.source_type,
        )

    def verify_snapshot(self, record: SnapshotRecord) -> bool:
        """Verify snapshot presence, size, SHA-256 digest, and manifest consistency."""
        if not record.snapshot_path.exists() or not record.snapshot_path.is_file():
            return False

        raw = record.snapshot_path.read_bytes()
        if len(raw) != record.size_bytes:
            return False
        if hashlib.sha256(raw).hexdigest() != record.sha256:
            return False

        if not record.manifest_path.exists() or not record.manifest_path.is_file():
            return False
        try:
            manifest = SnapshotRecord.model_validate_json(
                record.manifest_path.read_text(encoding="utf-8")
            )
        except Exception:
            return False

        return manifest.sha256 == record.sha256 and manifest.size_bytes == record.size_bytes

    def verify_snapshot_or_raise(self, record: SnapshotRecord) -> None:
        """Raise ``SnapshotIntegrityError`` if ``record`` fails verification."""
        if not self.verify_snapshot(record):
            raise SnapshotIntegrityError(
                f"Snapshot verification failed for {record.snapshot_id!r} "
                f"(path={record.snapshot_path})."
            )

    def list_snapshots(self, session_id: str) -> list[SnapshotRecord]:
        """List all persisted snapshot records for ``session_id`` sorted by snapshot ID."""
        snap_dir = self.snapshots_dir(session_id)
        if not snap_dir.exists():
            return []
        records: list[SnapshotRecord] = []
        for manifest_file in sorted(snap_dir.glob("*.manifest.json")):
            records.append(
                SnapshotRecord.model_validate_json(manifest_file.read_text(encoding="utf-8"))
            )
        return records
