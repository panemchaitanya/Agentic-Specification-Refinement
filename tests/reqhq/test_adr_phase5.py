"""Read-only governance tests for Phase 5 Nygard ADR-0021."""

from __future__ import annotations

import shutil
from pathlib import Path

from reqhq.adr import ADRManager

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_ADR_DIR = REPO_ROOT / "docs" / "adr"

ADR_0021_FILE = "0021-live-llm-coding-agent-and-verified-live-evaluation-pipeline.md"
ADR_0021_BRANCH = "adr/0021-live-llm-coding-agent-and-verified-live-eval-pipeline"


class TestPhase5NygardADRReadOnly:
    """Validate ADR-0021 conformance, module/CLI parity, audit ledger, and LF line endings."""

    def test_adr_0021_conforms_to_nygard_standard_and_documents_phase5_decisions(self) -> None:
        mgr = ADRManager(REAL_ADR_DIR)
        records = mgr.list_adrs(include_archived=False)
        by_num = {rec.number: rec for rec in records}

        assert len(records) >= 21
        assert "0021" in by_num
        rec = by_num["0021"]
        assert rec.path.name == ADR_0021_FILE
        assert rec.status == "Accepted"
        assert rec.pr_branch == ADR_0021_BRANCH
        assert rec.supersedes is None
        assert rec.archived is False

        content = rec.path.read_text(encoding="utf-8")
        for token in (
            "LiveLLMCodingAgent",
            "src/reqhq/harness/live_agent.py",
            "CodingAgent",
            "COND_A",
            "COND_B",
            "COND_C",
            "hidden_tests/test_acceptance.py",
            "pbt_lock.sha256",
            "_CodeGenPayload",
            "EvalRunConfig.split",
            "--split dev|all",
            "_build_live_eval_hooks",
            "LiveExecutionUnavailableError",
            "VERIFIED_LIVE",
        ):
            assert token in content, f"ADR-0021 missing required decision detail: {token}"

    def test_audit_log_index_parity_and_lf_endings_through_0021(self, tmp_path: Path) -> None:
        mgr = ADRManager(REAL_ADR_DIR)
        entries = mgr.audit.read_entries()
        assert len(entries) >= 42
        assert [e.action for e in entries if e.adr_number == "0021"] == ["created", "accepted"]

        records = mgr.list_adrs(include_archived=True)
        assert [r.number for r in records[:21]] == [f"{i:04d}" for i in range(1, 22)]

        assert (REAL_ADR_DIR / "INDEX.md").read_text(encoding="utf-8") == mgr._render_index_markdown(
            records
        )
        assert (
            REAL_ADR_DIR / "AUDIT_HISTORY.md"
        ).read_text(encoding="utf-8") == mgr.audit.render_history_markdown()

        for path in sorted(REAL_ADR_DIR.rglob("*")):
            if path.is_file():
                assert b"\r\n" not in path.read_bytes(), f"CRLF detected in {path.name}"

        copied = tmp_path / "adr"
        shutil.copytree(REAL_ADR_DIR, copied)
        copied_mgr = ADRManager(copied)
        c1 = copied_mgr.sync_index()
        c2 = copied_mgr.sync_index()
        assert c1 == c2 and c1["total"] >= 21
