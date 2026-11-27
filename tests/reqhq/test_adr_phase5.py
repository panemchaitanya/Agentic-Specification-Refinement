"""Read-only governance tests for Phase 5 Nygard ADRs (0021 superseded by 0022 v2; 0003/0020 consolidated into 0023 v2)."""

from __future__ import annotations

import shutil
from pathlib import Path

from reqhq.adr import ADRManager

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_ADR_DIR = REPO_ROOT / "docs" / "adr"

ADR_0021_FILE = "0021-live-llm-coding-agent-and-verified-live-evaluation-pipeline.md"
ADR_0021_BRANCH = "adr/0021-live-llm-coding-agent-and-verified-live-eval-pipeline"
ADR_0022_FILE = (
    "0022-live-llm-coding-agent-verified-live-evaluation-pipeline-path-resolution-telemetry-v2.md"
)
ADR_0023_FILE = (
    "0023-hardware-aware-local-slm-free-tier-router-live-decoding-escalation-short-circuit-v2.md"
)
PHASE5_V2_BRANCH = "fix/p5-live-eval-paths-telemetry-progress"


class TestPhase5NygardADRReadOnly:
    """Validate consolidated v2 ADRs (0022 & 0023), clean supersession/archival, audit ledger, and LF."""

    def test_adr_0021_archived_and_superseded_by_consolidated_v2_adr_0022(self) -> None:
        mgr = ADRManager(REAL_ADR_DIR)
        all_records = mgr.list_adrs(include_archived=True)
        by_num = {rec.number: rec for rec in all_records}

        assert len(all_records) >= 23
        assert "0021" in by_num and "0022" in by_num

        rec21 = by_num["0021"]
        assert rec21.path.name == ADR_0021_FILE
        assert rec21.status == "Superseded"
        assert rec21.superseded_by == "0022"
        assert rec21.archived is True
        assert rec21.path.parent == REAL_ADR_DIR / "archive"
        assert rec21.pr_branch == ADR_0021_BRANCH

        rec22 = by_num["0022"]
        assert rec22.path.name == ADR_0022_FILE
        assert rec22.status == "Accepted"
        assert rec22.supersedes == "0021"
        assert rec22.pr_branch == PHASE5_V2_BRANCH
        assert rec22.archived is False

        content22 = rec22.path.read_text(encoding="utf-8")
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
            "DEF-P5-002",
            "DEF-P5-003",
            "DEF-P5-004",
            "Path.resolve()",
            "resp.cloud_verified",
            "include_ledger_delta=False",
            "progress_callback",
            "_echo_eval_progress",
        ):
            assert token in content22, f"ADR-0022 (v2) missing required decision detail: {token}"

    def test_adr_0003_and_0020_consolidated_into_v2_adr_0023_with_zero_routing_clash(self) -> None:
        mgr = ADRManager(REAL_ADR_DIR)
        all_records = mgr.list_adrs(include_archived=True)
        by_num = {rec.number: rec for rec in all_records}

        rec03 = by_num["0003"]
        assert rec03.status == "Superseded"
        assert rec03.superseded_by == "0023"
        assert rec03.archived is True
        assert rec03.path.parent == REAL_ADR_DIR / "archive"

        rec20 = by_num["0020"]
        assert rec20.status == "Deprecated"
        assert rec20.archived is True
        assert rec20.path.parent == REAL_ADR_DIR / "archive"

        rec23 = by_num["0023"]
        assert rec23.path.name == ADR_0023_FILE
        assert rec23.status == "Accepted"
        assert rec23.supersedes == "0003"
        assert rec23.pr_branch == PHASE5_V2_BRANCH
        assert rec23.archived is False

        content23 = rec23.path.read_text(encoding="utf-8")
        for token in (
            "HardwareProfiler",
            "qwen3:4b",
            "phi4-mini",
            '"think": False',
            "responseJsonSchema",
            "gemini-3.1-flash-lite",
            "REQHQ_GEMINI_MODEL",
            "max_transient_retries=1",
            "_escalate_to_cloud()",
            "QuotaLedger",
            "PIIScrubber",
            "--strict-local",
            "PromptCache",
            "_build_router()",
            "SocioTechnicalTriager.triage()",
            "MAX_FALLBACK_NODES_TOTAL=24",
            "verify_credential_live()",
            "--verify-live",
        ):
            assert token in content23, f"ADR-0023 (v2) missing required decision detail: {token}"

        # Active set must NOT simultaneously contain superseded v1 ADRs alongside their v2 replacements
        active_nums = {rec.number for rec in mgr.list_adrs(include_archived=False)}
        assert {"0022", "0023"}.issubset(active_nums)
        assert active_nums.isdisjoint({"0003", "0020", "0021"})

    def test_audit_log_index_parity_and_lf_endings_through_0023(self, tmp_path: Path) -> None:
        mgr = ADRManager(REAL_ADR_DIR)
        entries = mgr.audit.read_entries()
        assert len(entries) >= 52
        assert [e.action for e in entries if e.adr_number == "0003"] == [
            "created",
            "accepted",
            "superseded",
            "archived",
        ]
        assert [e.action for e in entries if e.adr_number == "0020"] == [
            "created",
            "accepted",
            "deprecated",
            "archived",
        ]
        assert [e.action for e in entries if e.adr_number == "0021"] == [
            "created",
            "accepted",
            "superseded",
            "archived",
        ]
        assert [e.action for e in entries if e.adr_number == "0022"] == ["created", "accepted"]
        assert [e.action for e in entries if e.adr_number == "0023"] == ["created", "accepted"]

        records = mgr.list_adrs(include_archived=True)
        assert [r.number for r in records[:23]] == [f"{i:04d}" for i in range(1, 24)]

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
        assert c1 == c2 and c1["total"] >= 23 and c1["archived"] >= 3
