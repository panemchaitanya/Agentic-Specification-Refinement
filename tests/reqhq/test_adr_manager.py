"""Offline unit tests for ReqHQ ADR lifecycle manager, audit log, and foundational ADRs."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from reqhq.adr import (
    ADRManager,
    ADRRecord,
    AuditEntry,
    AuditTrail,
    normalize_adr_id,
    slugify,
)
from reqhq.adr.audit import _escape_md_cell
from reqhq.adr.manager import _parse_optional_adr_ref

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_ADR_DIR = REPO_ROOT / "docs" / "adr"


class TestHelpersAndModels:
    """Tests for normalization helpers and strict Pydantic v2 models."""

    def test_slugify_normalizes_and_rejects_empty(self) -> None:
        assert slugify("Use Local-First SLM Router!") == "use-local-first-slm-router"
        assert slugify("  ADR: Python 3.15 + pypdf  ") == "adr-python-3-15-pypdf"
        with pytest.raises(ValueError, match="alphanumeric"):
            slugify("   !!!   ")

    def test_normalize_adr_id_formats_and_validates(self) -> None:
        assert normalize_adr_id("1") == "0001"
        assert normalize_adr_id("0006") == "0006"
        assert normalize_adr_id("ADR-12") == "0012"
        assert normalize_adr_id("adr_0042") == "0042"
        assert normalize_adr_id("0003-hardware-aware-router.md") == "0003"

        with pytest.raises(ValueError, match="Invalid ADR identifier"):
            normalize_adr_id("not-an-id")
        with pytest.raises(ValueError, match="out of 4-digit range"):
            normalize_adr_id("0")
        with pytest.raises(ValueError, match="out of 4-digit range"):
            normalize_adr_id("10000")

    def test_parse_optional_adr_ref(self) -> None:
        assert _parse_optional_adr_ref(None) is None
        assert _parse_optional_adr_ref("None") is None
        assert _parse_optional_adr_ref("N/A") is None
        assert _parse_optional_adr_ref("—") is None
        assert _parse_optional_adr_ref("ADR-7") == "0007"
        assert _parse_optional_adr_ref("[0002](./0002-foo.md)") == "0002"
        assert _parse_optional_adr_ref("custom-ref") == "custom-ref"

    def test_pydantic_models_forbid_extra_fields(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError):
            ADRRecord(
                number="0001",
                title="Test",
                slug="test",
                status="Proposed",
                date="2026-11-24",
                path=tmp_path / "0001-test.md",
                unexpected="boom",  # type: ignore[call-arg]
            )

        with pytest.raises(ValidationError):
            AuditEntry(
                action="created",
                adr_number="0001",
                unknown_field=123,  # type: ignore[call-arg]
            )


class TestAuditTrail:
    """Tests for append-only audit_log.jsonl and AUDIT_HISTORY.md rendering."""

    def test_empty_audit_trail(self, tmp_path: Path) -> None:
        trail = AuditTrail(tmp_path / "adr")
        assert trail.read_entries() == []
        rendered = trail.render_history_markdown()
        assert "No ADR lifecycle events recorded yet." in rendered

    def test_append_read_and_render_all_transitions(self, tmp_path: Path) -> None:
        trail = AuditTrail(tmp_path / "adr")
        trail.record_and_render(
            AuditEntry(
                action="created",
                adr_number="0001",
                title="First | Decision\nLine 2",
                from_status=None,
                to_status="Proposed",
                author="alice",
                note="Created | with pipe\nand newline",
                path="0001-first.md",
            )
        )
        trail.append(
            AuditEntry(
                action="accepted",
                adr_number="ADR-0001",
                title="First Decision",
                from_status="Proposed",
                to_status="Accepted",
                author="bob",
                note="",
                path="0001-first.md",
                related_adr="ADR-0002",
            )
        )
        trail.append(
            AuditEntry(
                action="index_synced",
                adr_number="ALL",
                from_status="Active",
                to_status=None,
                author="cli",
                note="Sync check",
                path="",
            )
        )
        trail.append(
            AuditEntry(
                action="archived",
                adr_number="0002",
                from_status=None,
                to_status=None,
                author="cli",
                note="Archived",
                path="archive/0002-old.md",
                related_adr="0003",
            )
        )

        # Inject a blank line to verify tolerant JSONL reader
        with trail.log_path.open("a", encoding="utf-8") as fh:
            fh.write("\n")

        entries = trail.read_entries()
        assert len(entries) == 4

        history_path = trail.write_history_markdown()
        history_md = history_path.read_text(encoding="utf-8")
        assert "**Total Audit Events:** 4" in history_md
        assert r"Created \| with pipe and newline" in history_md
        assert "Proposed -> Accepted" in history_md
        assert "| `index_synced` | ALL | Active |" in history_md
        assert "ADR-0003" in history_md
        assert _escape_md_cell("") == "—"


class TestADRManagerLifecycle:
    """End-to-end offline tests for ADRManager in isolated tmp_path directories."""

    def test_sync_index_on_empty_dir(self, tmp_path: Path) -> None:
        mgr = ADRManager(tmp_path / "adr")
        assert mgr.list_adrs(include_archived=True) == []
        counts = mgr.sync_index()
        assert counts == {
            "total": 0,
            "active": 0,
            "archived": 0,
            "proposed": 0,
            "accepted": 0,
            "deprecated": 0,
            "superseded": 0,
        }
        index_md = (tmp_path / "adr" / "INDEX.md").read_text(encoding="utf-8")
        assert "_No active ADRs._" in index_md
        assert "_No archived ADRs._" in index_md

    def test_create_accept_and_monotonic_numbering(self, tmp_path: Path) -> None:
        mgr = ADRManager(tmp_path / "adr")

        with pytest.raises(ValueError, match="cannot be empty"):
            mgr.create("   ")

        rec1 = mgr.create(
            "Adopt Strict Schemas",
            context="We need strict validation.",
            decision="Use Pydantic v2 extra=forbid.",
            consequences="Fails fast on unknown keys.",
            author="architect",
        )
        assert rec1.number == "0001"
        assert rec1.slug == "adopt-strict-schemas"
        assert rec1.status == "Proposed"
        assert rec1.pr_branch == "adr/0001-adopt-strict-schemas"
        assert rec1.archived is False
        assert rec1.path.exists()

        rec2 = mgr.create(
            "Use SQLite Event Log",
            pr_branch="adr/custom-event-log",
            supersedes="1",
            author="architect",
        )
        assert rec2.number == "0002"
        assert rec2.pr_branch == "adr/custom-event-log"
        assert rec2.supersedes == "0001"

        accepted = mgr.accept("1", author="reviewer", note="LGTM in PR #10")
        assert accepted.status == "Accepted"

        with pytest.raises(ValueError, match="already Accepted"):
            mgr.accept("0001")

        with pytest.raises(FileNotFoundError, match="ADR-0099"):
            mgr.accept("99")

        entries = mgr.audit.read_entries()
        assert [e.action for e in entries] == ["created", "created", "accepted"]
        assert entries[-1].note == "LGTM in PR #10"

    def test_deprecate_moves_to_archive_and_preserves_monotonic_counter(self, tmp_path: Path) -> None:
        mgr = ADRManager(tmp_path / "adr")
        rec1 = mgr.create("Legacy Parser Strategy", author="dev")
        mgr.accept(rec1.number, author="lead")

        archived = mgr.deprecate("0001", reason="Replaced by pluggable registry", author="lead")
        assert archived.status == "Deprecated"
        assert archived.archived is True
        assert archived.path.parent == mgr.archive_dir
        assert archived.path.exists()
        assert not rec1.path.exists()
        assert "## Deprecation Note" in archived.path.read_text(encoding="utf-8")
        assert "Replaced by pluggable registry" in archived.path.read_text(encoding="utf-8")

        # Cannot accept, deprecate, or supersede an archived/deprecated ADR
        with pytest.raises(ValueError, match="Cannot accept"):
            mgr.accept("0001")
        with pytest.raises(ValueError, match="Cannot deprecate"):
            mgr.deprecate("0001")
        with pytest.raises(ValueError, match="Cannot supersede"):
            mgr.supersede("0001", "Another Title")

        # Active list excludes archived; include_archived=True includes it
        assert mgr.list_adrs(include_archived=False) == []
        assert [r.number for r in mgr.list_adrs(include_archived=True)] == ["0001"]

        # Next created ADR MUST be 0002 (never reuse archived 0001!)
        rec2 = mgr.create("Pluggable Parser Registry", author="dev")
        assert rec2.number == "0002"

        counts = mgr.sync_index()
        assert counts == {
            "total": 2,
            "active": 1,
            "archived": 1,
            "proposed": 1,
            "accepted": 0,
            "deprecated": 1,
            "superseded": 0,
        }
        index_md = mgr.index_path.read_text(encoding="utf-8")
        assert "archive/0001-legacy-parser-strategy.md" in index_md
        assert "0002-pluggable-parser-registry.md" in index_md

        actions = [e.action for e in mgr.audit.read_entries()]
        assert actions == ["created", "accepted", "deprecated", "archived", "created"]

    def test_supersede_cross_links_archives_and_logs(self, tmp_path: Path) -> None:
        mgr = ADRManager(tmp_path / "adr")
        old_initial = mgr.create("Initial Single Provider Router", author="dev")
        mgr.accept(old_initial.number, author="lead")

        old_archived, new_rec = mgr.supersede(
            "ADR-0001",
            "Multi-Provider Free-Tier Failover Router",
            decision="Fail over between Gemini and Groq on HTTP 429.",
            consequences="Improves availability under free-tier quotas.",
            author="architect",
        )

        assert old_archived.number == "0001"
        assert old_archived.status == "Superseded"
        assert old_archived.superseded_by == "0002"
        assert old_archived.archived is True
        assert old_archived.path.parent == mgr.archive_dir
        assert not old_initial.path.exists()

        assert new_rec.number == "0002"
        assert new_rec.status == "Proposed"
        assert new_rec.supersedes == "0001"
        assert new_rec.archived is False
        assert new_rec.path.parent == mgr.adr_dir

        old_text = old_archived.path.read_text(encoding="utf-8")
        assert "- **Status:** Superseded" in old_text
        assert "- **Superseded By:** ADR-0002" in old_text
        assert "## Supersession Note" in old_text

        index_md = mgr.index_path.read_text(encoding="utf-8")
        assert "ADR-0001" in index_md
        assert "ADR-0002" in index_md
        assert "**Superseded**" in index_md

        history_md = mgr.audit.history_path.read_text(encoding="utf-8")
        assert "`superseded`" in history_md
        assert "`archived`" in history_md

    def test_parser_and_metadata_edge_cases(self, tmp_path: Path) -> None:
        mgr = ADRManager(tmp_path / "adr")
        mgr._ensure_dirs()

        # Non-ADR markdown files are ignored by list_adrs
        (mgr.adr_dir / "NOTES.md").write_text("# Notes\n", encoding="utf-8")
        assert mgr.list_adrs() == []

        # Invalid filename rejected by _parse_adr_file
        bad_name = mgr.adr_dir / "bad.md"
        bad_name.write_text("# Bad\n", encoding="utf-8")
        with pytest.raises(ValueError, match="naming convention"):
            mgr._parse_adr_file(bad_name, archived=False)

        # Invalid status rejected
        bad_status = mgr.adr_dir / "0001-bad-status.md"
        bad_status.write_text(
            "# 0001. Bad Status\n\n- **Date:** 2026-11-24\n- **Status:** InvalidState\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="Unsupported ADR status"):
            mgr._parse_adr_file(bad_status, archived=False)
        bad_status.unlink()

        # Minimal hand-authored ADR missing heading, PR/Branch=None, and missing Status line
        minimal = mgr.adr_dir / "0005-hand-authored.md"
        minimal.write_text(
            "- **Date:** 2026-11-24\n- **PR / Branch:** None\n- **Superseded By:** ADR-0004\n## Context\nHand written.\n",
            encoding="utf-8",
        )
        parsed = mgr._parse_adr_file(minimal, archived=False)
        assert parsed.title == "Hand Authored"
        assert parsed.pr_branch == ""
        assert parsed.status == "Proposed"
        assert parsed.superseded_by == "0004"

        # Metadata updater handles missing Status line and replaces existing Superseded By
        mgr._update_metadata_in_file(minimal, status="Superseded", superseded_by="0006")
        reparsed = mgr._parse_adr_file(minimal, archived=False)
        assert reparsed.status == "Superseded"
        assert reparsed.superseded_by == "0006"

        # Metadata updater handles file with no section headings when adding Superseded By
        no_section = mgr.adr_dir / "0006-no-section.md"
        no_section.write_text("# 0006. No Section\n\n- **Status:** Proposed\n", encoding="utf-8")
        mgr._update_metadata_in_file(no_section, status="Superseded", superseded_by="0007")
        assert "- **Superseded By:** ADR-0007" in no_section.read_text(encoding="utf-8")

        # Metadata updater handles missing blank line immediately before ## Context
        tight = mgr.adr_dir / "0007-tight-section.md"
        tight.write_text(
            "# 0007. Tight Section\n\n- **Status:** Proposed\n## Context\nNo blank line.\n",
            encoding="utf-8",
        )
        mgr._update_metadata_in_file(tight, status="Superseded", superseded_by="0008")
        tight_text = tight.read_text(encoding="utf-8")
        assert "- **Superseded By:** ADR-0008\n## Context" in tight_text

        # Direct renderer includes Superseded By when provided
        rendered = mgr._render_adr_document(
            number="0008",
            title="Rendered Supersession",
            adr_date="2026-11-24",
            status="Superseded",
            pr_branch="adr/0008-rendered-supersession",
            supersedes="0007",
            superseded_by="0009",
            context="Ctx",
            decision="Dec",
            consequences="Cons",
        )
        assert "- **Superseded By:** ADR-0009" in rendered

        # Relative path fallback when outside adr_dir
        outside = tmp_path / "external.md"
        assert mgr._rel_path_str(outside) == outside.as_posix()

    def test_sync_index_idempotency_and_pipe_title_escaping(self, tmp_path: Path) -> None:
        mgr = ADRManager(tmp_path / "adr")
        rec = mgr.create("Use Local | Cloud Router", author="architect")
        mgr.accept(rec.number, author="reviewer")

        counts_first = mgr.sync_index()
        index_first = mgr.index_path.read_text(encoding="utf-8")
        history_first = mgr.audit.history_path.read_text(encoding="utf-8")
        log_first = mgr.audit.log_path.read_text(encoding="utf-8")

        counts_second = mgr.sync_index()
        assert counts_second == counts_first
        assert mgr.index_path.read_text(encoding="utf-8") == index_first
        assert mgr.audit.history_path.read_text(encoding="utf-8") == history_first
        assert mgr.audit.log_path.read_text(encoding="utf-8") == log_first
        assert r"Use Local \| Cloud Router" in index_first

        # Deprecate and re-check archived table pipe escaping and sync idempotency
        mgr.deprecate(rec.number, reason="Superseded by test", author="reviewer")
        counts_arch_1 = mgr.sync_index()
        index_arch_1 = mgr.index_path.read_text(encoding="utf-8")
        counts_arch_2 = mgr.sync_index()
        assert counts_arch_2 == counts_arch_1
        assert mgr.index_path.read_text(encoding="utf-8") == index_arch_1
        assert r"Use Local \| Cloud Router" in index_arch_1


class TestFoundationalRepoADRsReadOnly:
    """Read-only validation against the real docs/adr/ directory (never mutates files)."""

    def test_governance_scaffolding_exists_and_matches_config_default(self) -> None:
        from reqhq.config import ReqHQConfig

        assert ReqHQConfig().adr_dir == Path("docs/adr")
        assert (REAL_ADR_DIR / "README.md").is_file()
        assert (REAL_ADR_DIR / "INDEX.md").is_file()
        assert (REAL_ADR_DIR / "AUDIT_HISTORY.md").is_file()
        assert (REAL_ADR_DIR / "audit_log.jsonl").is_file()
        assert (REAL_ADR_DIR / "archive" / ".gitkeep").is_file()

    def test_foundational_adrs_0001_to_0006_conform_to_nygard_standard(self) -> None:
        mgr = ADRManager(REAL_ADR_DIR)
        records = mgr.list_adrs(include_archived=False)
        expected_branches = {
            "0001": "adr/0001-record-architecture-decisions",
            "0002": "adr/0002-isolate-v2-engine-under-src-reqhq",
            "0003": "adr/0003-hardware-aware-local-slm-and-free-tier-router",
            "0004": "adr/0004-day1-cloud-connectors-and-hierarchical-chunking",
            "0005": "adr/0005-resumable-event-log-and-strict-pydantic-schemas",
            "0006": "adr/0006-abstract-system-context-for-brownfield-readiness",
        }

        by_num = {rec.number: rec for rec in records}
        for num, branch in expected_branches.items():
            assert num in by_num, f"Missing foundational ADR-{num}"
            rec = by_num[num]
            assert rec.status == "Accepted"
            assert rec.pr_branch == branch
            assert rec.date == "2026-11-24"
            assert rec.archived is False

            content = rec.path.read_text(encoding="utf-8")
            assert "## Context" in content
            assert "## Decision" in content
            assert "## Consequences" in content
            assert "- **Date:**" in content
            assert "- **Status:**" in content
            assert "- **PR / Branch:**" in content
            assert "- **Supersedes:**" in content

    def test_adr_0004_documents_python_315_wheel_audit(self) -> None:
        adr4 = (REAL_ADR_DIR / "0004-day1-cloud-connectors-and-hierarchical-chunking.md").read_text(
            encoding="utf-8"
        )
        for token in ("3.15", "docling", "pymupdf", "pypdf", "python-docx", "drive.readonly", "Files.Read.All"):
            assert token in adr4, f"ADR-0004 missing required audit/connector detail: {token}"

    def test_index_and_audit_history_match_disk_state(self) -> None:
        mgr = ADRManager(REAL_ADR_DIR)
        records = mgr.list_adrs(include_archived=True)
        assert len(records) >= 6

        expected_index = mgr._render_index_markdown(records)
        actual_index = (REAL_ADR_DIR / "INDEX.md").read_text(encoding="utf-8")
        assert actual_index == expected_index

        entries = mgr.audit.read_entries()
        assert len(entries) >= 12
        for num in ("0001", "0002", "0003", "0004", "0005", "0006"):
            actions_for_num = [e.action for e in entries if e.adr_number == num]
            assert "created" in actions_for_num, f"Missing created audit entry for ADR-{num}"
            assert "accepted" in actions_for_num, f"Missing accepted audit entry for ADR-{num}"
        expected_history = mgr.audit.render_history_markdown()
        actual_history = (REAL_ADR_DIR / "AUDIT_HISTORY.md").read_text(encoding="utf-8")
        assert actual_history == expected_history
