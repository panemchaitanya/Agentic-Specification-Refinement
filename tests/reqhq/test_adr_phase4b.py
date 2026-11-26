"""Read-only governance tests and Hypothesis PBT for Phase 4b Nygard ADRs (0018-0019)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from reqhq.adr import ADRManager

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_ADR_DIR = REPO_ROOT / "docs" / "adr"

PHASE4B_EXPECTED_BRANCHES: dict[str, str] = {
    "0018": "adr/0018-local-ollama-auto-bootstrap-and-readiness-gate",
    "0019": "adr/0019-encrypted-local-credential-vault-and-first-run-provisioning",
}

PHASE4B_EXPECTED_FILES: dict[str, str] = {
    "0018": "0018-local-ollama-auto-bootstrap-and-readiness-gate.md",
    "0019": "0019-encrypted-local-credential-vault-and-first-run-provisioning.md",
}

_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_ADR_WORD = st.text(alphabet=_ALNUM, min_size=1, max_size=12)
_ADR_SEP = st.sampled_from([" ", " - ", " | ", ": ", "/", " (", ") ", "."])


@st.composite
def _phase4b_title_strategy(draw: st.DrawFn) -> str:
    """Generate valid ADR titles (1-6 words, 1-80 chars) including punctuation & pipe chars."""
    words = draw(st.lists(_ADR_WORD, min_size=1, max_size=6))
    parts = [words[0]]
    for word in words[1:]:
        parts.append(draw(_ADR_SEP))
        parts.append(word)
    suffix = draw(st.sampled_from(["", " (Phase 4b)", " | vault", " [local]"]))
    return ("".join(parts) + suffix).strip()[:80]


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


class TestPhase4bNygardADRsReadOnly:
    """Validate Phase 4b ADRs (0018-0019), exact module/CLI parity, audit ledger, and LF."""

    def test_phase4b_adrs_0018_to_0019_conform_to_nygard_standard(self) -> None:
        mgr = ADRManager(REAL_ADR_DIR)
        records = mgr.list_adrs(include_archived=False)
        by_num = {rec.number: rec for rec in records}

        assert len(records) >= 19
        for num, expected_branch in PHASE4B_EXPECTED_BRANCHES.items():
            assert num in by_num, f"Missing Phase 4b ADR-{num}"
            rec = by_num[num]
            assert rec.path.name == PHASE4B_EXPECTED_FILES[num]
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

    def test_phase4b_adrs_document_required_modules_cli_and_security_decisions(self) -> None:
        adr18 = (REAL_ADR_DIR / PHASE4B_EXPECTED_FILES["0018"]).read_text(encoding="utf-8")
        for token in (
            "src/reqhq/llm/ollama_runtime.py",
            "OllamaRuntimeManager",
            "ensure_ollama_ready",
            "OllamaReadinessReport",
            "OllamaRuntimeError",
            "normalize_model_tag",
            "reqhq doctor",
            "--auto-start",
            "--auto-pull",
            "--warmup",
            "GET /api/tags",
            "ollama serve",
            "CREATE_NO_WINDOW",
            "DETACHED_PROCESS",
            "%LOCALAPPDATA%\\Programs\\Ollama\\ollama.exe",
            "model_registry.py",
            "TIER_0",
            "qwen3:4b",
            "phi4-mini",
            'keep_alive="10m"',
            "REQHQ_AUTO_START_OLLAMA",
            "--mock",
        ):
            assert token in adr18, f"ADR-0018 missing required decision detail: {token}"

        adr19 = (REAL_ADR_DIR / PHASE4B_EXPECTED_FILES["0019"]).read_text(encoding="utf-8")
        for token in (
            "src/reqhq/security/credential_vault.py",
            "src/reqhq/security/models.py",
            "EncryptedCredentialVault",
            "SecretProtector",
            "WindowsDPAPIProtector",
            "PBKDF2FernetProtector",
            "ensure_credentials_ready",
            "VaultDecryptionError",
            "mask_secret",
            "is_valid_credential_value",
            "ReqHQConfig.vault_path",
            "REQHQ_VAULT_PATH",
            "reqhq auth setup",
            "reqhq auth status",
            "reqhq auth rotate",
            "reqhq auth clear",
            "cryptography.fernet.Fernet",
            "CryptProtectData",
            "CryptUnprotectData",
            "CRYPTPROTECT_UI_FORBIDDEN",
            "PBKDF2HMAC",
            "your-gemini-api-key-here",
            "hide_input=True",
            "--strict-local",
            "--mock",
        ):
            assert token in adr19, f"ADR-0019 missing required decision detail: {token}"

    def test_audit_log_covers_all_19_adrs_with_38_or_more_events(self) -> None:
        mgr = ADRManager(REAL_ADR_DIR)
        entries = mgr.audit.read_entries()
        assert len(entries) >= 38

        for num in [f"{i:04d}" for i in range(1, 20)]:
            actions = [entry.action for entry in entries if entry.adr_number == num]
            assert actions == ["created", "accepted"], (
                f"Expected ['created', 'accepted'] for ADR-{num}, got {actions}"
            )

    def test_index_audit_history_parity_and_lf_line_endings(self) -> None:
        mgr = ADRManager(REAL_ADR_DIR)
        records = mgr.list_adrs(include_archived=True)
        numbers = [rec.number for rec in records]
        assert numbers[:19] == [f"{i:04d}" for i in range(1, 20)]

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

    def test_sync_index_is_idempotent_on_phase4b_copy(self, tmp_path: Path) -> None:
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
        assert counts_1["total"] >= 19
        assert counts_1["accepted"] >= 19
        assert (copied / "INDEX.md").read_bytes() == before_index
        assert (copied / "AUDIT_HISTORY.md").read_bytes() == before_history
        assert (copied / "audit_log.jsonl").read_bytes() == before_log

    def test_adr_0020_documents_live_provider_short_circuit_and_probing_decisions(self) -> None:
        mgr = ADRManager(REAL_ADR_DIR)
        records = mgr.list_adrs(include_archived=False)
        by_num = {rec.number: rec for rec in records}
        assert len(records) >= 20
        assert "0020" in by_num
        rec20 = by_num["0020"]
        assert rec20.status == "Accepted"
        assert rec20.archived is False
        content = rec20.path.read_text(encoding="utf-8")
        for token in (
            "DEF-P4B-003",
            "DEF-P4B-004",
            "DEF-P4B-005",
            "DEF-P4B-006",
            '"think": False',
            "responseJsonSchema",
            "gemini-3.1-flash-lite",
            "REQHQ_GEMINI_MODEL",
            "max_transient_retries=1",
            "_build_router()",
            "_escalate_to_cloud()",
            "SocioTechnicalTriager.triage()",
            "MAX_FALLBACK_NODES_TOTAL=24",
            "verify_credential_live()",
            "--verify-live",
        ):
            assert token in content, f"ADR-0020 missing required decision detail: {token}"
        entries = mgr.audit.read_entries()
        assert len(entries) >= 40
        assert [e.action for e in entries if e.adr_number == "0020"] == ["created", "accepted"]


class TestPhase4bADRManagerHypothesisPBT:
    """Deterministic Hypothesis PBT extending Phase 4b ADR lifecycle governance invariants."""

    @settings(derandomize=True, max_examples=50, deadline=None)
    @given(
        ops=st.lists(
            st.tuples(
                st.sampled_from(["create", "accept", "deprecate", "supersede"]),
                _phase4b_title_strategy(),
            ),
            min_size=1,
            max_size=8,
        )
    )
    def test_pbt_phase4b_monotonic_numbering_archive_integrity_and_lf_sync(
        self,
        ops: list[tuple[str, str]],
    ) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            copied = Path(tmp_dir) / "adr"
            shutil.copytree(REAL_ADR_DIR, copied)
            mgr = ADRManager(copied)

            base_count = len(mgr.list_adrs(include_archived=True))
            assert base_count >= 19
            next_expected = base_count + 1
            new_archived: set[str] = set()

            for action, title in ops:
                active = mgr.list_adrs(include_archived=False)
                new_active = [r for r in active if int(r.number) > base_count]
                if action == "create" or not new_active:
                    rec = mgr.create(title, author="pbt-p4b")
                    assert int(rec.number) == next_expected
                    next_expected += 1
                elif action == "accept":
                    proposed = [r for r in new_active if r.status == "Proposed"]
                    if proposed:
                        acc = mgr.accept(proposed[0].number, author="pbt-p4b")
                        assert acc.status == "Accepted"
                        assert acc.archived is False
                elif action == "deprecate":
                    target = new_active[0]
                    arch = mgr.deprecate(target.number, reason="pbt-p4b | deprecate", author="pbt-p4b")
                    assert arch.status == "Deprecated"
                    assert arch.archived is True
                    assert arch.path.parent == mgr.archive_dir
                    assert not target.path.exists()
                    new_archived.add(arch.number)
                elif action == "supersede":
                    target = new_active[0]
                    old_arch, new_rec = mgr.supersede(target.number, title, author="pbt-p4b")
                    assert old_arch.status == "Superseded"
                    assert old_arch.archived is True
                    assert old_arch.superseded_by == new_rec.number
                    assert new_rec.supersedes == old_arch.number
                    assert int(new_rec.number) == next_expected
                    next_expected += 1
                    new_archived.add(old_arch.number)

            # Invariant 1: 0001..0019 remain untouched & Accepted; all IDs strictly monotonic 1..N
            all_records = mgr.list_adrs(include_archived=True)
            assert [int(r.number) for r in all_records] == list(range(1, next_expected))
            for orig in all_records[:19]:
                assert orig.status == "Accepted"
                assert orig.archived is False
            assert new_archived.issubset({r.number for r in all_records if r.archived})

            # Invariant 2: sync_index() is byte-for-byte idempotent and strictly LF-only
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

            # Invariant 3: Markdown table columns remain intact even with `|` in generated titles
            for line in idx_1.decode("utf-8").splitlines():
                if line.startswith("| "):
                    assert _unescaped_pipe_count(line) == 7
            for line in hist_1.decode("utf-8").splitlines():
                if line.startswith("| "):
                    assert _unescaped_pipe_count(line) == 9
