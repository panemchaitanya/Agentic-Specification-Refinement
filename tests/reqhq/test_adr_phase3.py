"""Read-only governance tests and Hypothesis PBT for Phase 3 Nygard ADRs (0011-0014)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
import pytest

from reqhq.adr import ADRManager, normalize_adr_id, slugify

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_ADR_DIR = REPO_ROOT / "docs" / "adr"

PHASE3_EXPECTED_BRANCHES: dict[str, str] = {
    "0011": "adr/0011-pbt-contract-emission-and-dual-layer-guardrails",
    "0012": "adr/0012-antigravity-handoff-and-windows-mutation-harness",
    "0013": "adr/0013-gap-injection-oracle-and-cross-domain-benchmark",
    "0014": "adr/0014-internal-pbt-and-post-swarm-verification-gate",
}

PHASE3_EXPECTED_FILES: dict[str, str] = {
    "0011": "0011-pbt-contract-emission-and-dual-layer-guardrails.md",
    "0012": "0012-antigravity-handoff-and-windows-mutation-harness.md",
    "0013": "0013-gap-injection-oracle-and-cross-domain-benchmark.md",
    "0014": "0014-internal-pbt-and-post-swarm-verification-gate.md",
}

_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_ADR_WORD = st.text(alphabet=_ALNUM, min_size=1, max_size=12)
_ADR_SEP = st.sampled_from([" ", " - ", " | ", ": ", "/", " (", ") ", ".", " — "])


@st.composite
def _adr_title_strategy(draw: st.DrawFn) -> str:
    """Generate valid ADR titles (1-6 words, 1-80 chars) including punctuation & pipe edge cases."""
    words = draw(st.lists(_ADR_WORD, min_size=1, max_size=6))
    parts = [words[0]]
    for word in words[1:]:
        parts.append(draw(_ADR_SEP))
        parts.append(word)
    # Optionally append trailing punctuation or pipe-delimited suffix without breaking alphanumeric requirement
    suffix = draw(st.sampled_from(["", "!", " (v2)", " | fallback", " [strict]"]))
    title = ("".join(parts) + suffix).strip()
    return title[:80]


def _unescaped_pipe_count(markdown_row: str) -> int:
    """Count unescaped Markdown table column delimiters `|` in a single table row."""
    count = 0
    escaped = False
    for ch in markdown_row:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "|":
            count += 1
    return count


class TestPhase3NygardADRsReadOnly:
    """Validate Phase 3 ADRs (0011-0014), namespace rules, audit ledger, and LF parity."""

    def test_phase3_adrs_0011_to_0014_conform_to_nygard_standard(self) -> None:
        mgr = ADRManager(REAL_ADR_DIR)
        records = mgr.list_adrs(include_archived=False)
        by_num = {rec.number: rec for rec in records}

        assert len(records) >= 14
        for num, expected_branch in PHASE3_EXPECTED_BRANCHES.items():
            assert num in by_num, f"Missing Phase 3 ADR-{num}"
            rec = by_num[num]
            assert rec.path.name == PHASE3_EXPECTED_FILES[num]
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

    def test_phase3_adrs_document_key_architectural_decisions_and_namespace_separation(self) -> None:
        adr11 = (REAL_ADR_DIR / PHASE3_EXPECTED_FILES["0011"]).read_text(encoding="utf-8")
        for token in (
            "PBT_Spec.yaml",
            "Pydantic v2",
            "Hypothesis",
            "RuleBasedStateMachine",
            "pbt_lock.sha256",
            "verify_contract_lock",
            "ContractTamperError",
            "CounterexampleRepairReport",
            "docs/adr/",
            "ADR-RXXX.md",
            "reqhq pbt-export",
        ):
            assert token in adr11, f"ADR-0011 missing required decision detail: {token}"

        adr12 = (REAL_ADR_DIR / PHASE3_EXPECTED_FILES["0012"]).read_text(encoding="utf-8")
        for token in (
            "COND_A",
            "COND_B",
            "COND_C",
            "HANDOFF_INSTRUCTIONS.md",
            "reqhq handoff",
            "zero-LLM-judge",
            "MutationRunner",
            "ASTMutantEngine",
            "MutmutRunner",
            "os.fork()",
            "mutation_score",
            "docs/adr/",
        ):
            assert token in adr12, f"ADR-0012 missing required decision detail: {token}"

        adr13 = (REAL_ADR_DIR / PHASE3_EXPECTED_FILES["0013"]).read_text(encoding="utf-8")
        for token in (
            "0.20 <= drop_ratio <= 0.40",
            "GapInjector",
            "FrozenOracleSimulator",
            "Not specified in ground truth",
            "data/frozen/",
            "cctns",
            "gamma_j",
            "dii",
            "16-Case",
            "fintech_settlements",
            "healthcare_operations",
            "inventory_logistics",
            "enterprise_it_hr",
            "cohens_kappa",
            "reqhq benchmark kappa",
        ):
            assert token in adr13, f"ADR-0013 missing required decision detail: {token}"

        adr14 = (REAL_ADR_DIR / PHASE3_EXPECTED_FILES["0014"]).read_text(encoding="utf-8")
        for token in (
            "Hypothesis",
            "derandomize=True",
            "PIIScrubber",
            "HierarchicalChunker",
            "QuotaLedger",
            "Independent Post-Swarm Deliverable Audit",
            "alloy_live",
            "src/specpipe/",
            "data/frozen/",
            "87 passed",
        ):
            assert token in adr14, f"ADR-0014 missing required decision detail: {token}"

    def test_audit_log_covers_all_14_adrs_with_28_or_more_events(self) -> None:
        mgr = ADRManager(REAL_ADR_DIR)
        entries = mgr.audit.read_entries()
        assert len(entries) >= 28

        for num in [f"{i:04d}" for i in range(1, 15)]:
            actions = [entry.action for entry in entries if entry.adr_number == num]
            assert actions == ["created", "accepted"], (
                f"Expected ['created', 'accepted'] for ADR-{num}, got {actions}"
            )

    def test_index_audit_history_parity_and_lf_line_endings(self) -> None:
        mgr = ADRManager(REAL_ADR_DIR)
        records = mgr.list_adrs(include_archived=True)
        numbers = [rec.number for rec in records]
        assert numbers[:14] == [f"{i:04d}" for i in range(1, 15)]

        expected_index = mgr._render_index_markdown(records)
        actual_index = (REAL_ADR_DIR / "INDEX.md").read_text(encoding="utf-8")
        assert actual_index == expected_index

        expected_history = mgr.audit.render_history_markdown()
        actual_history = (REAL_ADR_DIR / "AUDIT_HISTORY.md").read_text(encoding="utf-8")
        assert actual_history == expected_history

        files_to_check = [
            *(REAL_ADR_DIR / fname for fname in PHASE3_EXPECTED_FILES.values()),
            REAL_ADR_DIR / "INDEX.md",
            REAL_ADR_DIR / "AUDIT_HISTORY.md",
            REAL_ADR_DIR / "audit_log.jsonl",
        ]
        for path in files_to_check:
            raw = path.read_bytes()
            assert b"\r\n" not in raw, f"Expected LF-only line endings in {path.name}"

    def test_sync_index_is_idempotent_on_phase3_copy(self, tmp_path: Path) -> None:
        copied = tmp_path / "adr"
        shutil.copytree(REAL_ADR_DIR, copied)

        mgr = ADRManager(copied)
        before_index = (copied / "INDEX.md").read_bytes()
        before_history = (copied / "AUDIT_HISTORY.md").read_bytes()
        before_log = (copied / "audit_log.jsonl").read_bytes()

        counts_first = mgr.sync_index()
        counts_second = mgr.sync_index()
        assert counts_first == counts_second
        assert counts_first["total"] >= 14
        assert counts_first["accepted"] >= 14
        assert (copied / "INDEX.md").read_bytes() == before_index
        assert (copied / "AUDIT_HISTORY.md").read_bytes() == before_history
        assert (copied / "audit_log.jsonl").read_bytes() == before_log


class TestADRManagerHypothesisPBT:
    """Deterministic Hypothesis PBT verifying ADR lifecycle, bounds, and governance invariants."""

    @settings(derandomize=True, max_examples=50)
    @given(
        adr_num=st.integers(min_value=1, max_value=9999),
        prefix=st.sampled_from(["", "ADR-", "adr-", "ADR_", "adr_"]),
        pad_width=st.integers(min_value=1, max_value=4),
        title=_adr_title_strategy(),
    )
    def test_pbt_id_normalization_and_slugify_bounds(
        self,
        adr_num: int,
        prefix: str,
        pad_width: int,
        title: str,
    ) -> None:
        raw_id = f"{prefix}{adr_num:0{pad_width}d}"
        normalized = normalize_adr_id(raw_id)
        assert len(normalized) == 4
        assert normalized == f"{adr_num:04d}"

        slug = slugify(title)
        assert slug == slug.lower()
        assert not slug.startswith("-") and not slug.endswith("-")
        assert all(ch in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in slug)
        assert slugify(slug) == slug

    @settings(derandomize=True, max_examples=50)
    @given(
        out_of_range=st.one_of(
            st.integers(min_value=-1000, max_value=0),
            st.integers(min_value=10000, max_value=999999),
        )
    )
    def test_pbt_normalize_adr_id_rejects_out_of_range_numbers(self, out_of_range: int) -> None:
        with pytest.raises(ValueError):
            normalize_adr_id(str(out_of_range))

    @settings(derandomize=True, max_examples=50, deadline=None)
    @given(
        ops=st.lists(
            st.tuples(
                st.sampled_from(["create", "accept", "deprecate", "supersede"]),
                _adr_title_strategy(),
            ),
            min_size=1,
            max_size=10,
        )
    )
    def test_pbt_nygard_invariants_monotonic_ids_archive_and_sync_idempotency(
        self,
        ops: list[tuple[str, str]],
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            mgr = ADRManager(Path(tmp_dir) / "adr")
            created_numbers: list[int] = []
            archived_numbers: set[str] = set()

            for action, title in ops:
                active = mgr.list_adrs(include_archived=False)
                if action == "create" or not active:
                    rec = mgr.create(title, author="pbt")
                    num_int = int(rec.number)
                    if created_numbers:
                        assert num_int == created_numbers[-1] + 1
                    else:
                        assert num_int == 1
                    created_numbers.append(num_int)
                elif action == "accept":
                    proposed = [r for r in active if r.status == "Proposed"]
                    if proposed:
                        accepted = mgr.accept(proposed[0].number, author="pbt")
                        assert accepted.status == "Accepted"
                        assert accepted.archived is False
                elif action == "deprecate":
                    target = active[0]
                    archived = mgr.deprecate(target.number, reason="pbt | deprecate", author="pbt")
                    assert archived.status == "Deprecated"
                    assert archived.archived is True
                    assert archived.path.parent == mgr.archive_dir
                    assert archived.path.is_file()
                    assert not target.path.exists()
                    archived_numbers.add(archived.number)
                elif action == "supersede":
                    target = active[0]
                    old_arch, new_rec = mgr.supersede(target.number, title, author="pbt")
                    assert old_arch.status == "Superseded"
                    assert old_arch.archived is True
                    assert old_arch.superseded_by == new_rec.number
                    assert old_arch.path.parent == mgr.archive_dir
                    assert new_rec.supersedes == old_arch.number
                    assert int(new_rec.number) == created_numbers[-1] + 1
                    created_numbers.append(int(new_rec.number))
                    archived_numbers.add(old_arch.number)

            # Invariant 1: strictly monotonic, unique 4-digit numbering (archived IDs never reused).
            all_records = mgr.list_adrs(include_archived=True)
            all_nums = [int(r.number) for r in all_records]
            assert all_nums == list(range(1, len(created_numbers) + 1))
            assert archived_numbers.issubset({r.number for r in all_records if r.archived})

            # Invariant 2: every ADR file (active and archived) conforms to Nygard sections + LF.
            for rec in all_records:
                raw = rec.path.read_bytes()
                assert b"\r\n" not in raw
                text = raw.decode("utf-8")
                assert "## Context" in text
                assert "## Decision" in text
                assert "## Consequences" in text
                assert f"- **Status:** {rec.status}" in text

            # Invariant 3: sync_index() is strictly idempotent and preserves byte-for-byte parity.
            counts_1 = mgr.sync_index()
            idx_1 = mgr.index_path.read_bytes()
            hist_1 = mgr.audit.history_path.read_bytes()
            log_1 = mgr.audit.log_path.read_bytes()

            counts_2 = mgr.sync_index()
            assert counts_1 == counts_2
            assert mgr.index_path.read_bytes() == idx_1
            assert mgr.audit.history_path.read_bytes() == hist_1
            assert mgr.audit.log_path.read_bytes() == log_1
            assert b"\r\n" not in idx_1
            assert b"\r\n" not in hist_1
            assert b"\r\n" not in log_1

            # Invariant 4: Markdown tables in INDEX.md (6 cols -> 7 pipes) and AUDIT_HISTORY.md
            # (8 cols -> 9 pipes) never split columns even when titles contain pipe `|` characters.
            for line in idx_1.decode("utf-8").splitlines():
                if line.startswith("| "):
                    assert _unescaped_pipe_count(line) == 7
            for line in hist_1.decode("utf-8").splitlines():
                if line.startswith("| "):
                    assert _unescaped_pipe_count(line) == 9
