"""Append-only audit logging and Markdown audit history rendering for ADRs."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AuditAction = Literal[
    "created",
    "accepted",
    "deprecated",
    "superseded",
    "archived",
    "index_synced",
]


def utc_now_iso() -> str:
    """Return current UTC timestamp in compact ISO-8601 format with Z suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _escape_md_cell(value: str | None) -> str:
    """Escape pipe characters and newlines for Markdown table cells."""
    if not value:
        return "—"
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", " ").replace("\n", " ").strip()


class AuditEntry(BaseModel):
    """Single immutable event in the ADR append-only audit log."""

    model_config = ConfigDict(extra="forbid")

    timestamp: str = Field(default_factory=utc_now_iso)
    action: AuditAction
    adr_number: str
    title: str = ""
    from_status: str | None = None
    to_status: str | None = None
    author: str = "cli"
    note: str = ""
    path: str = ""
    related_adr: str | None = None


class AuditTrail:
    """Manages append-only `audit_log.jsonl` and rendered `AUDIT_HISTORY.md`."""

    def __init__(self, adr_dir: Path) -> None:
        self.adr_dir = Path(adr_dir)
        self.log_path = self.adr_dir / "audit_log.jsonl"
        self.history_path = self.adr_dir / "AUDIT_HISTORY.md"

    def ensure_initialized(self) -> None:
        """Ensure the ADR directory and audit log file exist."""
        self.adr_dir.mkdir(parents=True, exist_ok=True)
        if not self.log_path.exists():
            self.log_path.write_text("", encoding="utf-8")

    def append(self, entry: AuditEntry) -> AuditEntry:
        """Append a single validated AuditEntry to `audit_log.jsonl` (never rewrites prior lines)."""
        self.ensure_initialized()
        line = entry.model_dump_json() + "\n"
        with self.log_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
        return entry

    def read_entries(self) -> list[AuditEntry]:
        """Read and validate all entries from `audit_log.jsonl` in chronological order."""
        if not self.log_path.exists():
            return []
        entries: list[AuditEntry] = []
        raw_text = self.log_path.read_text(encoding="utf-8")
        for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                entries.append(AuditEntry.model_validate_json(stripped))
            except Exception as exc:  # pragma: no cover - defensive parse guard
                raise ValueError(f"Invalid audit log JSON at {self.log_path}:{line_no}: {exc}") from exc
        return entries

    def render_history_markdown(self) -> str:
        """Deterministically render `AUDIT_HISTORY.md` from `audit_log.jsonl`."""
        entries = self.read_entries()
        counts: dict[str, int] = {}
        for entry in entries:
            counts[entry.action] = counts.get(entry.action, 0) + 1

        lines: list[str] = [
            "# ADR Audit History",
            "",
            "> Auto-generated from the append-only [`audit_log.jsonl`](./audit_log.jsonl) ledger. "
            "Do not delete historical events; append new lifecycle transitions via `ADRManager` or `reqhq adr`.",
            "",
            f"- **Total Audit Events:** {len(entries)}",
            f"- **Created:** {counts.get('created', 0)} | "
            f"**Accepted:** {counts.get('accepted', 0)} | "
            f"**Deprecated:** {counts.get('deprecated', 0)} | "
            f"**Superseded:** {counts.get('superseded', 0)} | "
            f"**Archived:** {counts.get('archived', 0)}",
            "",
        ]

        if not entries:
            lines.append("_No ADR lifecycle events recorded yet._")
            lines.append("")
            return "\n".join(lines)

        lines.extend(
            [
                "| Timestamp (UTC) | Action | ADR | Transition | Author | Related | Path | Notes |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )

        for entry in entries:
            if entry.from_status and entry.to_status:
                transition = f"{entry.from_status} -> {entry.to_status}"
            elif entry.to_status:
                transition = entry.to_status
            elif entry.from_status:
                transition = entry.from_status
            else:
                transition = "—"

            adr_label = (
                f"ADR-{entry.adr_number}"
                if entry.adr_number != "ALL" and not entry.adr_number.startswith("ADR-")
                else entry.adr_number
            )
            related_label = (
                f"ADR-{entry.related_adr}"
                if entry.related_adr and not entry.related_adr.startswith("ADR-")
                else (entry.related_adr or "—")
            )
            path_cell = f"`{entry.path}`" if entry.path else "—"
            lines.append(
                "| "
                + " | ".join(
                    [
                        _escape_md_cell(entry.timestamp),
                        f"`{entry.action}`",
                        _escape_md_cell(adr_label),
                        _escape_md_cell(transition),
                        _escape_md_cell(entry.author),
                        _escape_md_cell(related_label),
                        path_cell,
                        _escape_md_cell(entry.note or entry.title),
                    ]
                )
                + " |"
            )

        lines.append("")
        return "\n".join(lines)

    def write_history_markdown(self) -> Path:
        """Write rendered `AUDIT_HISTORY.md` to disk and return its path."""
        self.ensure_initialized()
        content = self.render_history_markdown()
        self.history_path.write_text(content, encoding="utf-8", newline="\n")
        return self.history_path

    def record_and_render(self, entry: AuditEntry) -> AuditEntry:
        """Append an audit entry and immediately refresh `AUDIT_HISTORY.md`."""
        self.append(entry)
        self.write_history_markdown()
        return entry
