"""Read-only governance validation tests for Phase 2 Nygard ADRs (0007-0010)."""

from __future__ import annotations

import shutil
from pathlib import Path

from reqhq.adr import ADRManager

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_ADR_DIR = REPO_ROOT / "docs" / "adr"

PHASE2_EXPECTED_BRANCHES: dict[str, str] = {
    "0007": "adr/0007-iterative-delta-critic-convergence-engine",
    "0008": "adr/0008-socio-technical-triage-and-routing-adr-synthesis",
    "0009": "adr/0009-two-layer-literate-alloy6-and-legacy-compat",
    "0010": "adr/0010-headless-alloy-verification-and-vacuity-gate",
}

PHASE2_EXPECTED_FILES: dict[str, str] = {
    "0007": "0007-iterative-delta-critic-convergence-engine.md",
    "0008": "0008-socio-technical-triage-and-routing-adr-synthesis.md",
    "0009": "0009-two-layer-literate-alloy6-and-legacy-compat.md",
    "0010": "0010-headless-alloy-verification-and-vacuity-gate.md",
}


class TestPhase2NygardADRsReadOnly:
    """Validate Phase 2 ADRs (0007-0010), namespace rules, audit ledger, and LF parity."""

    def test_phase2_adrs_0007_to_0010_conform_to_nygard_standard(self) -> None:
        mgr = ADRManager(REAL_ADR_DIR)
        records = mgr.list_adrs(include_archived=False)
        by_num = {rec.number: rec for rec in records}

        assert len(records) >= 10
        for num, expected_branch in PHASE2_EXPECTED_BRANCHES.items():
            assert num in by_num, f"Missing Phase 2 ADR-{num}"
            rec = by_num[num]
            assert rec.path.name == PHASE2_EXPECTED_FILES[num]
            assert rec.status == "Accepted"
            assert rec.pr_branch == expected_branch
            assert rec.date == "2026-11-24"
            assert rec.supersedes is None
            assert rec.archived is False

            content = rec.path.read_text(encoding="utf-8")
            assert f"# {num}. " in content
            assert "- **Date:** 2026-11-24" in content
            assert "- **Status:** Accepted" in content
            assert f"- **PR / Branch:** `{expected_branch}`" in content
            assert "- **Supersedes:** None" in content
            assert "## Context" in content
            assert "## Decision" in content
            assert "## Consequences" in content

    def test_phase2_adrs_document_key_architectural_decisions_and_namespace_separation(self) -> None:
        adr7 = (REAL_ADR_DIR / PHASE2_EXPECTED_FILES["0007"]).read_text(encoding="utf-8")
        for token in (
            "TIER_0",
            "Radeon 660M",
            "qwen3:4b",
            "phi4-mini",
            "BLOCKER",
            "MAJOR",
            "MINOR",
            "3–5",
            "finalize",
            "--accept-defaults",
            "user_confirmed: false",
            "ADR-AXXX.md",
            "docs/adr/",
            "EscalationReason.CRITIC_DISAGREEMENT",
        ):
            assert token in adr7, f"ADR-0007 missing required decision detail: {token}"

        adr8 = (REAL_ADR_DIR / PHASE2_EXPECTED_FILES["0008"]).read_text(encoding="utf-8")
        for token in (
            "Software",
            "Process",
            "Training",
            "Hybrid",
            "docs/adr/",
            "ADR-RXXX.md",
            "ADR-AXXX.md",
            "Functional Core",
            "Imperative Shell",
            "Design.md",
            "Mermaid",
            "Process_and_Training.md",
            "traceability_matrix.json",
        ):
            assert token in adr8, f"ADR-0008 missing required decision detail: {token}"

        adr9 = (REAL_ADR_DIR / PHASE2_EXPECTED_FILES["0009"]).read_text(encoding="utf-8")
        for token in (
            "Alloy 6",
            "core_domain",
            "open core_domain",
            "var",
            "always",
            "eventually",
            "after",
            "until",
            "once",
            "--alloy-compat=legacy",
            "util/ordering[Time]",
            "sig HumanActor",
            "sig TrainedRole",
            "lint_core_domain_version_agnostic",
            "// @trace REQ-XXX",
        ):
            assert token in adr9, f"ADR-0009 missing required decision detail: {token}"

        adr10 = (REAL_ADR_DIR / PHASE2_EXPECTED_FILES["0010"]).read_text(encoding="utf-8")
        for token in (
            "REQHQ_ALLOY_JAR_PATH",
            "java -jar",
            "VACUOUS_UNSAT",
            "__vacuity_witness_",
            "UNSAT",
            "// @trace REQ-XXX",
            "CounterexamplePrompt",
            "EscalationReason.ALLOY_REPAIR_EXHAUSTED",
            "MockAlloyAnalyzer",
            "@pytest.mark.alloy_live",
        ):
            assert token in adr10, f"ADR-0010 missing required decision detail: {token}"

    def test_audit_log_has_20_events_covering_0001_to_0010(self) -> None:
        mgr = ADRManager(REAL_ADR_DIR)
        entries = mgr.audit.read_entries()
        assert len(entries) >= 20

        expected_ids = [f"{i:04d}" for i in range(1, 11)]
        for num in expected_ids:
            actions = [entry.action for entry in entries if entry.adr_number == num]
            assert actions[:2] == ["created", "accepted"], (
                f"Expected prefix ['created', 'accepted'] for ADR-{num}, got {actions}"
            )

    def test_index_audit_history_parity_and_lf_line_endings(self) -> None:
        mgr = ADRManager(REAL_ADR_DIR)
        records = mgr.list_adrs(include_archived=True)
        assert [rec.number for rec in records][:10] == [f"{i:04d}" for i in range(1, 11)]

        expected_index = mgr._render_index_markdown(records)
        actual_index = (REAL_ADR_DIR / "INDEX.md").read_text(encoding="utf-8")
        assert actual_index == expected_index

        expected_history = mgr.audit.render_history_markdown()
        actual_history = (REAL_ADR_DIR / "AUDIT_HISTORY.md").read_text(encoding="utf-8")
        assert actual_history == expected_history

        files_to_check = [
            *(REAL_ADR_DIR / fname for fname in PHASE2_EXPECTED_FILES.values()),
            REAL_ADR_DIR / "INDEX.md",
            REAL_ADR_DIR / "AUDIT_HISTORY.md",
            REAL_ADR_DIR / "audit_log.jsonl",
        ]
        for path in files_to_check:
            raw = path.read_bytes()
            assert b"\r\n" not in raw, f"Expected LF-only line endings in {path.name}"

    def test_sync_index_is_idempotent_on_phase2_copy(self, tmp_path: Path) -> None:
        copied = tmp_path / "adr"
        shutil.copytree(REAL_ADR_DIR, copied)

        mgr = ADRManager(copied)
        before_index = (copied / "INDEX.md").read_bytes()
        before_history = (copied / "AUDIT_HISTORY.md").read_bytes()
        before_log = (copied / "audit_log.jsonl").read_bytes()
        expected_total = len(mgr.list_adrs(include_archived=True))

        counts = mgr.sync_index()
        assert counts["total"] == expected_total
        assert counts["total"] >= 10
        assert counts["active"] >= 10
        assert counts["accepted"] >= 10
        assert counts["active"] + counts["archived"] == counts["total"]
        assert (copied / "INDEX.md").read_bytes() == before_index
        assert (copied / "AUDIT_HISTORY.md").read_bytes() == before_history
        assert (copied / "audit_log.jsonl").read_bytes() == before_log
