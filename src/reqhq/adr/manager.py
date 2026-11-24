"""Michael Nygard-style Architecture Decision Record (ADR) lifecycle manager."""

from __future__ import annotations

import re
import shutil
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from reqhq.adr.audit import AuditEntry, AuditTrail

ADRStatus = Literal["Proposed", "Accepted", "Deprecated", "Superseded"]

_FILENAME_RE = re.compile(r"^(\d{4})-([a-z0-9-]+)\.md$")
_HEADING_RE = re.compile(r"^#\s+(?:ADR-)?(\d+)[.:]?\s+(.+?)\s*$", re.MULTILINE)
_META_LINE_RE = re.compile(r"^-\s+\*\*([^*:]+):\*\*\s*(.+?)\s*$")
_ID_RE = re.compile(r"^(?:adr[-_]?)?(\d+)(?:-[a-z0-9-]+(?:\.md)?)?$", re.IGNORECASE)

_VALID_STATUSES: set[str] = {"Proposed", "Accepted", "Deprecated", "Superseded"}


def slugify(title: str) -> str:
    """Convert an ADR title into a lowercase hyphen-separated filename slug."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    if not cleaned:
        raise ValueError(f"ADR title must contain alphanumeric characters: {title!r}")
    return cleaned


def normalize_adr_id(adr_id: str) -> str:
    """Normalize an ADR identifier (e.g. '1', '0001', 'ADR-0001') to 4-digit string."""
    match = _ID_RE.match(adr_id.strip())
    if not match:
        raise ValueError(f"Invalid ADR identifier: {adr_id!r}")
    num = int(match.group(1))
    if num <= 0 or num > 9999:
        raise ValueError(f"ADR number out of 4-digit range (1..9999): {adr_id!r}")
    return f"{num:04d}"


def _parse_optional_adr_ref(raw: str | None) -> str | None:
    if raw is None:
        return None
    cleaned = raw.strip().strip("`").strip()
    if not cleaned or cleaned.lower() in {"none", "n/a", "null", "-", "—"}:
        return None
    link_match = re.search(r"(?:ADR-)?(\d{1,4})", cleaned, re.IGNORECASE)
    if link_match:
        return f"{int(link_match.group(1)):04d}"
    return cleaned


class ADRRecord(BaseModel):
    """Structured metadata for a single Nygard ADR file."""

    model_config = ConfigDict(extra="forbid")

    number: str
    title: str
    slug: str
    status: ADRStatus
    date: str
    path: Path
    pr_branch: str = ""
    supersedes: str | None = None
    superseded_by: str | None = None
    archived: bool = False


class ADRManager:
    """Lifecycle manager for Michael Nygard ADRs, archival, index sync, and audit logging."""

    def __init__(self, adr_dir: Path = Path("docs/adr")) -> None:
        self.adr_dir = Path(adr_dir)
        self.archive_dir = self.adr_dir / "archive"
        self.index_path = self.adr_dir / "INDEX.md"
        self.audit = AuditTrail(self.adr_dir)

    def _ensure_dirs(self) -> None:
        self.adr_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.audit.ensure_initialized()

    def _rel_path_str(self, file_path: Path) -> str:
        try:
            return file_path.resolve().relative_to(self.adr_dir.resolve()).as_posix()
        except ValueError:
            return file_path.as_posix()

    def _next_number(self) -> str:
        existing = self.list_adrs(include_archived=True)
        if not existing:
            return "0001"
        highest = max(int(rec.number) for rec in existing)
        return f"{highest + 1:04d}"

    def _parse_adr_file(self, file_path: Path, *, archived: bool) -> ADRRecord:
        filename_match = _FILENAME_RE.match(file_path.name)
        if not filename_match:
            raise ValueError(f"File does not follow NNNN-slug.md naming convention: {file_path}")
        number, slug = filename_match.group(1), filename_match.group(2)

        content = file_path.read_text(encoding="utf-8")
        heading_match = _HEADING_RE.search(content)
        title = heading_match.group(2).strip() if heading_match else slug.replace("-", " ").title()

        meta: dict[str, str] = {}
        for line in content.splitlines():
            meta_match = _META_LINE_RE.match(line.strip())
            if meta_match:
                key = meta_match.group(1).strip().lower()
                val = meta_match.group(2).strip()
                meta[key] = val
            elif line.startswith("## "):
                break

        raw_status = meta.get("status", "Proposed").strip().strip("`")
        if raw_status not in _VALID_STATUSES:
            raise ValueError(f"Unsupported ADR status {raw_status!r} in {file_path}")
        status: ADRStatus = raw_status  # type: ignore[assignment]

        adr_date = meta.get("date", date.today().isoformat()).strip().strip("`")
        pr_branch = meta.get("pr / branch", meta.get("pr/branch", "")).strip().strip("`")
        if pr_branch.lower() in {"none", "n/a", "—", "-"}:
            pr_branch = ""

        supersedes = _parse_optional_adr_ref(meta.get("supersedes"))
        superseded_by = _parse_optional_adr_ref(meta.get("superseded by"))

        return ADRRecord(
            number=number,
            title=title,
            slug=slug,
            status=status,
            date=adr_date,
            path=file_path,
            pr_branch=pr_branch,
            supersedes=supersedes,
            superseded_by=superseded_by,
            archived=archived,
        )

    def _render_adr_document(
        self,
        *,
        number: str,
        title: str,
        adr_date: str,
        status: ADRStatus,
        pr_branch: str,
        supersedes: str | None,
        superseded_by: str | None,
        context: str,
        decision: str,
        consequences: str,
    ) -> str:
        supersedes_str = f"ADR-{supersedes}" if supersedes else "None"
        pr_str = f"`{pr_branch}`" if pr_branch else "None"
        meta_lines = [
            f"# {number}. {title}",
            "",
            f"- **Date:** {adr_date}",
            f"- **Status:** {status}",
            f"- **PR / Branch:** {pr_str}",
            f"- **Supersedes:** {supersedes_str}",
        ]
        if superseded_by:
            meta_lines.append(f"- **Superseded By:** ADR-{superseded_by}")

        context_body = (
            context.strip()
            if context.strip()
            else "Describe the architectural forces, constraints, and problem statement motivating this decision."
        )
        decision_body = (
            decision.strip()
            if decision.strip()
            else "State the architectural decision clearly in active voice."
        )
        consequences_body = (
            consequences.strip()
            if consequences.strip()
            else "List positive outcomes, trade-offs, risks, and follow-up obligations."
        )

        sections = [
            "\n".join(meta_lines),
            "",
            "## Context",
            "",
            context_body,
            "",
            "## Decision",
            "",
            decision_body,
            "",
            "## Consequences",
            "",
            consequences_body,
            "",
        ]
        return "\n".join(sections)

    def _update_metadata_in_file(
        self,
        file_path: Path,
        *,
        status: ADRStatus,
        superseded_by: str | None = None,
        append_section: tuple[str, str] | None = None,
    ) -> None:
        text = file_path.read_text(encoding="utf-8")
        lines = text.splitlines()
        out_lines: list[str] = []
        status_updated = False
        superseded_updated = False
        inserted_superseded = False

        for line in lines:
            stripped = line.strip()
            meta_match = _META_LINE_RE.match(stripped)
            if meta_match:
                key = meta_match.group(1).strip().lower()
                if key == "status":
                    out_lines.append(f"- **Status:** {status}")
                    status_updated = True
                    continue
                if key == "superseded by":
                    if superseded_by:
                        out_lines.append(f"- **Superseded By:** ADR-{superseded_by}")
                        superseded_updated = True
                        inserted_superseded = True
                    continue
            if superseded_by and not inserted_superseded and stripped.startswith("## "):
                if out_lines and out_lines[-1] == "":
                    out_lines.insert(len(out_lines) - 1, f"- **Superseded By:** ADR-{superseded_by}")
                else:
                    out_lines.append(f"- **Superseded By:** ADR-{superseded_by}")
                inserted_superseded = True
            out_lines.append(line)

        if not status_updated:
            insert_idx = 2 if len(out_lines) >= 2 else len(out_lines)
            out_lines.insert(insert_idx, f"- **Status:** {status}")

        if superseded_by and not superseded_updated and not inserted_superseded:
            out_lines.append(f"- **Superseded By:** ADR-{superseded_by}")

        if append_section:
            heading, body = append_section
            if out_lines and out_lines[-1] != "":
                out_lines.append("")
            out_lines.extend([f"## {heading}", "", body.strip()])

        file_path.write_text("\n".join(out_lines).rstrip() + "\n", encoding="utf-8", newline="\n")

    def _find_adr(self, adr_id: str, *, include_archived: bool = True) -> ADRRecord:
        normalized = normalize_adr_id(adr_id)
        for record in self.list_adrs(include_archived=include_archived):
            if record.number == normalized:
                return record
        raise FileNotFoundError(f"ADR-{normalized} not found in {self.adr_dir}")

    def list_adrs(self, include_archived: bool = False) -> list[ADRRecord]:
        """List ADRs in monotonic numeric order; optionally include archived ADRs."""
        records: list[ADRRecord] = []
        if self.adr_dir.exists():
            for path in sorted(self.adr_dir.iterdir()):
                if path.is_file() and _FILENAME_RE.match(path.name):
                    records.append(self._parse_adr_file(path, archived=False))

        if include_archived and self.archive_dir.exists():
            for path in sorted(self.archive_dir.iterdir()):
                if path.is_file() and _FILENAME_RE.match(path.name):
                    records.append(self._parse_adr_file(path, archived=True))

        records.sort(key=lambda rec: (int(rec.number), rec.archived))
        return records

    def create(
        self,
        title: str,
        *,
        context: str = "",
        decision: str = "",
        consequences: str = "",
        pr_branch: str = "",
        supersedes: str | None = None,
        author: str = "cli",
    ) -> ADRRecord:
        """Create a new Proposed Nygard ADR with the next monotonic 4-digit number."""
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("ADR title cannot be empty.")

        self._ensure_dirs()
        number = self._next_number()
        slug = slugify(clean_title)
        branch = pr_branch.strip() or f"adr/{number}-{slug}"
        norm_supersedes = normalize_adr_id(supersedes) if supersedes else None
        today = date.today().isoformat()

        file_path = self.adr_dir / f"{number}-{slug}.md"
        content = self._render_adr_document(
            number=number,
            title=clean_title,
            adr_date=today,
            status="Proposed",
            pr_branch=branch,
            supersedes=norm_supersedes,
            superseded_by=None,
            context=context,
            decision=decision,
            consequences=consequences,
        )
        file_path.write_text(content, encoding="utf-8", newline="\n")

        record = self._parse_adr_file(file_path, archived=False)
        self.audit.append(
            AuditEntry(
                action="created",
                adr_number=record.number,
                title=record.title,
                from_status=None,
                to_status="Proposed",
                author=author,
                note=f"Created ADR-{record.number}: {record.title}",
                path=self._rel_path_str(record.path),
                related_adr=norm_supersedes,
            )
        )
        self.sync_index()
        return record

    def accept(
        self,
        adr_id: str,
        *,
        author: str = "cli",
        note: str = "",
    ) -> ADRRecord:
        """Transition a Proposed ADR to Accepted and log the audit event."""
        record = self._find_adr(adr_id, include_archived=True)
        if record.archived or record.status in {"Deprecated", "Superseded"}:
            raise ValueError(
                f"Cannot accept ADR-{record.number}: current status is {record.status} "
                f"(archived={record.archived})."
            )
        if record.status == "Accepted":
            raise ValueError(f"ADR-{record.number} is already Accepted.")

        previous_status = record.status
        self._update_metadata_in_file(record.path, status="Accepted")
        updated = self._parse_adr_file(record.path, archived=False)

        self.audit.append(
            AuditEntry(
                action="accepted",
                adr_number=updated.number,
                title=updated.title,
                from_status=previous_status,
                to_status="Accepted",
                author=author,
                note=note or f"Accepted ADR-{updated.number}: {updated.title}",
                path=self._rel_path_str(updated.path),
            )
        )
        self.sync_index()
        return updated

    def deprecate(
        self,
        adr_id: str,
        *,
        reason: str = "",
        author: str = "cli",
    ) -> ADRRecord:
        """Mark an ADR as Deprecated, move it to `docs/adr/archive/`, and log audit events."""
        self._ensure_dirs()
        record = self._find_adr(adr_id, include_archived=True)
        if record.archived or record.status in {"Deprecated", "Superseded"}:
            raise ValueError(
                f"Cannot deprecate ADR-{record.number}: current status is {record.status} "
                f"(archived={record.archived})."
            )

        previous_status = record.status
        note_body = reason.strip() or f"Deprecated by {author} on {date.today().isoformat()}."
        self._update_metadata_in_file(
            record.path,
            status="Deprecated",
            append_section=("Deprecation Note", note_body),
        )

        archived_path = self.archive_dir / record.path.name
        shutil.move(str(record.path), str(archived_path))
        archived_record = self._parse_adr_file(archived_path, archived=True)
        rel_archived = self._rel_path_str(archived_path)

        self.audit.append(
            AuditEntry(
                action="deprecated",
                adr_number=archived_record.number,
                title=archived_record.title,
                from_status=previous_status,
                to_status="Deprecated",
                author=author,
                note=note_body,
                path=rel_archived,
            )
        )
        self.audit.append(
            AuditEntry(
                action="archived",
                adr_number=archived_record.number,
                title=archived_record.title,
                from_status="Deprecated",
                to_status="Deprecated",
                author=author,
                note=f"Archived ADR-{archived_record.number} to {rel_archived}",
                path=rel_archived,
            )
        )
        self.sync_index()
        return archived_record

    def supersede(
        self,
        old_adr_id: str,
        new_title: str,
        *,
        context: str = "",
        decision: str = "",
        consequences: str = "",
        pr_branch: str = "",
        author: str = "cli",
    ) -> tuple[ADRRecord, ADRRecord]:
        """Create a new ADR superseding an existing ADR and archive the superseded ADR.

        Returns:
            Tuple of ``(old_archived_record, new_record)``.
        """
        self._ensure_dirs()
        old_record = self._find_adr(old_adr_id, include_archived=True)
        if old_record.archived or old_record.status in {"Deprecated", "Superseded"}:
            raise ValueError(
                f"Cannot supersede ADR-{old_record.number}: current status is {old_record.status} "
                f"(archived={old_record.archived})."
            )

        new_record = self.create(
            new_title,
            context=context or f"Supersedes ADR-{old_record.number} ({old_record.title}).",
            decision=decision,
            consequences=consequences,
            pr_branch=pr_branch,
            supersedes=old_record.number,
            author=author,
        )

        previous_status = old_record.status
        self._update_metadata_in_file(
            old_record.path,
            status="Superseded",
            superseded_by=new_record.number,
            append_section=(
                "Supersession Note",
                f"Superseded by ADR-{new_record.number} ({new_record.title}) on {date.today().isoformat()}.",
            ),
        )

        archived_path = self.archive_dir / old_record.path.name
        shutil.move(str(old_record.path), str(archived_path))
        archived_old = self._parse_adr_file(archived_path, archived=True)
        rel_archived = self._rel_path_str(archived_path)

        self.audit.append(
            AuditEntry(
                action="superseded",
                adr_number=archived_old.number,
                title=archived_old.title,
                from_status=previous_status,
                to_status="Superseded",
                author=author,
                note=f"Superseded by ADR-{new_record.number}: {new_record.title}",
                path=rel_archived,
                related_adr=new_record.number,
            )
        )
        self.audit.append(
            AuditEntry(
                action="archived",
                adr_number=archived_old.number,
                title=archived_old.title,
                from_status="Superseded",
                to_status="Superseded",
                author=author,
                note=f"Archived superseded ADR-{archived_old.number} to {rel_archived}",
                path=rel_archived,
                related_adr=new_record.number,
            )
        )
        self.sync_index()
        return archived_old, new_record

    def _render_index_markdown(self, records: list[ADRRecord]) -> str:
        active = [rec for rec in records if not rec.archived]
        archived = [rec for rec in records if rec.archived]

        lines: list[str] = [
            "# Architecture Decision Records Index",
            "",
            "> Auto-generated by `ADRManager.sync_index()` (`reqhq adr sync-index`). "
            "See [`README.md`](./README.md) for governance rules and "
            "[`AUDIT_HISTORY.md`](./AUDIT_HISTORY.md) for the append-only lifecycle audit trail.",
            "",
            f"- **Total ADRs:** {len(records)} (**Active:** {len(active)}, **Archived:** {len(archived)})",
            "",
            "## Active ADRs",
            "",
        ]

        if active:
            lines.extend(
                [
                    "| ID | Title | Status | Date | PR / Branch | Supersedes |",
                    "| --- | --- | --- | --- | --- | --- |",
                ]
            )
            for rec in active:
                rel = self._rel_path_str(rec.path)
                title_cell = rec.title.replace("|", r"\|")
                branch_cell = f"`{rec.pr_branch}`" if rec.pr_branch else "—"
                supersedes_cell = f"ADR-{rec.supersedes}" if rec.supersedes else "—"
                lines.append(
                    f"| [ADR-{rec.number}](./{rel}) | {title_cell} | **{rec.status}** "
                    f"| {rec.date} | {branch_cell} | {supersedes_cell} |"
                )
        else:
            lines.append("_No active ADRs._")

        lines.extend(["", "## Archived ADRs (`docs/adr/archive/`)", ""])

        if archived:
            lines.extend(
                [
                    "| ID | Title | Status | Date | Superseded By | File |",
                    "| --- | --- | --- | --- | --- | --- |",
                ]
            )
            for rec in archived:
                rel = self._rel_path_str(rec.path)
                title_cell = rec.title.replace("|", r"\|")
                sup_by = f"ADR-{rec.superseded_by}" if rec.superseded_by else "—"
                lines.append(
                    f"| [ADR-{rec.number}](./{rel}) | {title_cell} | **{rec.status}** "
                    f"| {rec.date} | {sup_by} | `{rel}` |"
                )
        else:
            lines.append("_No archived ADRs._")

        lines.append("")
        return "\n".join(lines)

    def sync_index(self) -> dict[str, int]:
        """Synchronize `INDEX.md` and `AUDIT_HISTORY.md` from disk and return summary counts."""
        self._ensure_dirs()
        records = self.list_adrs(include_archived=True)
        index_md = self._render_index_markdown(records)
        self.index_path.write_text(index_md, encoding="utf-8", newline="\n")
        self.audit.write_history_markdown()

        counts: dict[str, int] = {
            "total": len(records),
            "active": sum(1 for r in records if not r.archived),
            "archived": sum(1 for r in records if r.archived),
            "proposed": sum(1 for r in records if r.status == "Proposed"),
            "accepted": sum(1 for r in records if r.status == "Accepted"),
            "deprecated": sum(1 for r in records if r.status == "Deprecated"),
            "superseded": sum(1 for r in records if r.status == "Superseded"),
        }
        return counts
