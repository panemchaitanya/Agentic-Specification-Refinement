"""Read-only governance tests and Hypothesis PBT for Phase 4 Nygard ADRs (0015-0017)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from reqhq.adr import ADRManager

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_ADR_DIR = REPO_ROOT / "docs" / "adr"

PHASE4_EXPECTED_BRANCHES: dict[str, str] = {
    "0015": "adr/0015-fail-closed-empirical-runner-and-mock-quarantine",
    "0016": "adr/0016-pure-python-stats-pareto-and-error-taxonomy",
    "0017": "adr/0017-reproducible-report-pipeline-and-release-manifest",
}

PHASE4_EXPECTED_FILES: dict[str, str] = {
    "0015": "0015-fail-closed-empirical-runner-and-mock-quarantine.md",
    "0016": "0016-pure-python-stats-pareto-and-error-taxonomy.md",
    "0017": "0017-reproducible-report-pipeline-and-release-manifest.md",
}

_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_ADR_WORD = st.text(alphabet=_ALNUM, min_size=1, max_size=12)
_ADR_SEP = st.sampled_from([" ", " - ", " | ", ": ", "/", " (", ") ", "."])


@st.composite
def _phase4_title_strategy(draw: st.DrawFn) -> str:
    """Generate valid ADR titles (1-6 words, 1-80 chars) including punctuation & pipe chars."""
    words = draw(st.lists(_ADR_WORD, min_size=1, max_size=6))
    parts = [words[0]]
    for word in words[1:]:
        parts.append(draw(_ADR_SEP))
        parts.append(word)
    suffix = draw(st.sampled_from(["", " (Phase 4)", " | strict", " [v2]"]))
    return ("".join(parts) + suffix).strip()[:80]


class TestPhase4NygardADRsReadOnly:
    """Validate Phase 4 ADRs (0015-0017), exact Phase 4 module parity, audit ledger, and LF."""

    def test_phase4_adrs_0015_to_0017_conform_to_nygard_standard(self) -> None:
        mgr = ADRManager(REAL_ADR_DIR)
        records = mgr.list_adrs(include_archived=False)
        by_num = {rec.number: rec for rec in records}

        assert len(records) >= 17
        for num, expected_branch in PHASE4_EXPECTED_BRANCHES.items():
            assert num in by_num, f"Missing Phase 4 ADR-{num}"
            rec = by_num[num]
            assert rec.path.name == PHASE4_EXPECTED_FILES[num]
            assert rec.status == "Accepted"
            assert rec.pr_branch == expected_branch
            assert rec.date == "2026-11-25"
            assert rec.supersedes is None
            assert rec.archived is False

            content = rec.path.read_text(encoding="utf-8")
            assert f"# {num}. " in content
            assert "- **Date:** 2026-11-25" in content
            assert "- **Status:** Accepted" in content
            assert f"- **PR / Branch:** `{expected_branch}`" in content
            assert "- **Supersedes:** None" in content
            assert "## Context" in content
            assert "## Decision" in content
            assert "## Consequences" in content

    def test_phase4_adrs_document_required_decisions_modules_and_quarantine(self) -> None:
        adr15 = (REAL_ADR_DIR / PHASE4_EXPECTED_FILES["0015"]).read_text(encoding="utf-8")
        for token in (
            "src/reqhq/evaluation/",
            "models.py",
            "telemetry.py",
            "tasks.py",
            "runner.py",
            "data/benchmarks/coding_tasks/",
            "reqhq eval run",
            "reqhq eval resume",
            "ctypes",
            "tracemalloc",
            "HardwareProfiler",
            "QuotaLedger",
            "RunMode.MOCK",
            "RunMode.LIVE",
            "QUARANTINED_MOCK_OR_INCOMPLETE",
            "--strict-live",
        ):
            assert token in adr15, f"ADR-0015 missing required decision detail: {token}"

        adr16 = (REAL_ADR_DIR / PHASE4_EXPECTED_FILES["0016"]).read_text(encoding="utf-8")
        for token in (
            "src/reqhq/analysis/",
            "models.py",
            "stats.py",
            "hypotheses.py",
            "pareto.py",
            "taxonomy.py",
            "pipeline.py",
            "reqhq eval analyze",
            "paired_bootstrap_ci",
            "mcnemar_exact_p",
            "wilcoxon_signed_rank",
            "DRY-RUN MOCK DATA",
            "pareto_quality_vs_cloud_cost.svg",
            "pareto_quality_vs_latency.svg",
            "cond_abc_comparison.svg",
            "error_taxonomy_bar.svg",
            "SCHEMA_DRIFT",
            "ALLOY_SYNTAX",
            "ALLOY_VACUOUS",
            "UNSAT_CONTRADICTION",
            "ROUTING_AMBIGUITY",
            "PBT_COUNTEREXAMPLE",
            "QUOTA_FALLBACK",
        ):
            assert token in adr16, f"ADR-0016 missing required decision detail: {token}"

        adr17 = (REAL_ADR_DIR / PHASE4_EXPECTED_FILES["0017"]).read_text(encoding="utf-8")
        for token in (
            "src/reqhq/reporting/",
            "models.py",
            "generator.py",
            "packager.py",
            "FINAL_REPORT.md",
            "tables_latex.tex",
            "booktabs",
            "escape_latex",
            "DRY-RUN / UNSCORED MOCK DATA",
            "artifacts_manifest.json",
            "verify_bundle",
            "BundleTamperError",
            "REPRODUCIBILITY.md",
            "docs/final_report/README.md",
            "reqhq eval report",
            "reqhq eval package",
            "reqhq eval verify-bundle",
        ):
            assert token in adr17, f"ADR-0017 missing required decision detail: {token}"

    def test_audit_log_covers_all_17_adrs_with_34_or_more_events(self) -> None:
        mgr = ADRManager(REAL_ADR_DIR)
        entries = mgr.audit.read_entries()
        assert len(entries) >= 34

        for num in [f"{i:04d}" for i in range(1, 18)]:
            actions = [entry.action for entry in entries if entry.adr_number == num]
            assert actions == ["created", "accepted"], (
                f"Expected ['created', 'accepted'] for ADR-{num}, got {actions}"
            )

    def test_index_audit_history_parity_and_lf_line_endings(self) -> None:
        mgr = ADRManager(REAL_ADR_DIR)
        records = mgr.list_adrs(include_archived=True)
        numbers = [rec.number for rec in records]
        assert numbers[:17] == [f"{i:04d}" for i in range(1, 18)]

        expected_index = mgr._render_index_markdown(records)
        actual_index = (REAL_ADR_DIR / "INDEX.md").read_text(encoding="utf-8")
        assert actual_index == expected_index

        expected_history = mgr.audit.render_history_markdown()
        actual_history = (REAL_ADR_DIR / "AUDIT_HISTORY.md").read_text(encoding="utf-8")
        assert actual_history == expected_history

        for path in sorted(REAL_ADR_DIR.rglob("*")):
            if path.is_file():
                raw = path.read_bytes()
                assert b"\r\n" not in raw, f"Expected LF-only line endings in {path.name}"

    def test_sync_index_is_idempotent_on_phase4_copy(self, tmp_path: Path) -> None:
        copied = tmp_path / "adr"
        shutil.copytree(REAL_ADR_DIR, copied)

        mgr = ADRManager(copied)
        before_index = (copied / "INDEX.md").read_bytes()
        before_history = (copied / "AUDIT_HISTORY.md").read_bytes()
        before_log = (copied / "audit_log.jsonl").read_bytes()
        expected_total = len(mgr.list_adrs(include_archived=True))

        counts_1 = mgr.sync_index()
        counts_2 = mgr.sync_index()
        assert counts_1 == counts_2
        assert counts_1["total"] == expected_total
        assert counts_1["total"] >= 17
        assert counts_1["accepted"] >= 17
        assert (copied / "INDEX.md").read_bytes() == before_index
        assert (copied / "AUDIT_HISTORY.md").read_bytes() == before_history
        assert (copied / "audit_log.jsonl").read_bytes() == before_log


class TestPhase4ADRManagerHypothesisPBT:
    """Deterministic Hypothesis PBT extending Phase 4 ADR lifecycle governance invariants."""

    @settings(derandomize=True, max_examples=50, deadline=None)
    @given(
        ops=st.lists(
            st.tuples(
                st.sampled_from(["create", "accept", "deprecate", "supersede"]),
                _phase4_title_strategy(),
            ),
            min_size=1,
            max_size=8,
        )
    )
    def test_pbt_phase4_monotonic_numbering_archive_integrity_and_lf_sync(
        self,
        ops: list[tuple[str, str]],
    ) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            copied = Path(tmp_dir) / "adr"
            shutil.copytree(REAL_ADR_DIR, copied)
            mgr = ADRManager(copied)

            base_count = len(mgr.list_adrs(include_archived=True))
            assert base_count >= 17
            next_expected = base_count + 1
            new_archived: set[str] = set()

            for action, title in ops:
                active = mgr.list_adrs(include_archived=False)
                # Operate only on newly created ADRs (>= base_count + 1) when mutating existing entries
                new_active = [r for r in active if int(r.number) > base_count]
                if action == "create" or not new_active:
                    rec = mgr.create(title, author="pbt-p4")
                    assert int(rec.number) == next_expected
                    next_expected += 1
                elif action == "accept":
                    proposed = [r for r in new_active if r.status == "Proposed"]
                    if proposed:
                        acc = mgr.accept(proposed[0].number, author="pbt-p4")
                        assert acc.status == "Accepted"
                        assert acc.archived is False
                elif action == "deprecate":
                    target = new_active[0]
                    arch = mgr.deprecate(target.number, reason="pbt-p4 deprecate", author="pbt-p4")
                    assert arch.status == "Deprecated"
                    assert arch.archived is True
                    assert arch.path.parent == mgr.archive_dir
                    assert not target.path.exists()
                    new_archived.add(arch.number)
                elif action == "supersede":
                    target = new_active[0]
                    old_arch, new_rec = mgr.supersede(target.number, title, author="pbt-p4")
                    assert old_arch.status == "Superseded"
                    assert old_arch.archived is True
                    assert old_arch.superseded_by == new_rec.number
                    assert new_rec.supersedes == old_arch.number
                    assert int(new_rec.number) == next_expected
                    next_expected += 1
                    new_archived.add(old_arch.number)

            # Invariant 1: 0001..0017 remain untouched & Accepted; all IDs strictly monotonic 1..N
            all_records = mgr.list_adrs(include_archived=True)
            assert [int(r.number) for r in all_records] == list(range(1, next_expected))
            for orig in all_records[:17]:
                assert orig.status == "Accepted"
                assert orig.archived is False
            assert new_archived.issubset({r.number for r in all_records if r.archived})

            # Invariant 2: sync_index() idempotent and LF-only across all files
            c1 = mgr.sync_index()
            idx_1 = mgr.index_path.read_bytes()
            hist_1 = mgr.audit.history_path.read_bytes()
            log_1 = mgr.audit.log_path.read_bytes()

            c2 = mgr.sync_index()
            assert c1 == c2
            assert mgr.index_path.read_bytes() == idx_1
            assert mgr.audit.history_path.read_bytes() == hist_1
            assert mgr.audit.log_path.read_bytes() == log_1
            assert b"\r\n" not in idx_1
            assert b"\r\n" not in hist_1
            assert b"\r\n" not in log_1
