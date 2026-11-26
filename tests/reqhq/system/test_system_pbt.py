"""Independent black-box cross-phase stateful & invariant PBT suite for ReqHQ v2.

Designed strictly from public contracts (ADRs 0001-0017, public CLI surfaces,
public Pydantic schemas, and public package exports) with zero private-helper
imports from src/reqhq/.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings, strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)
from typer.testing import CliRunner

from reqhq.alloy import (
    AlloyCompatMode,
    AlloySpecEmitter,
    build_req_to_alloy_refs,
    extract_alloy_blocks,
    lint_core_domain_version_agnostic,
)
from reqhq.analysis import (
    HypothesisVerdictStatus,
    MockRunQuarantineError,
    analyze_evaluation,
)
from reqhq.cli import app as cli_app
from reqhq.elicitation import RequirementDecomposer
from reqhq.evaluation import EvalOrchestrator, EvalRunConfig, RunMode, sha256_file
from reqhq.ingestion import (
    HierarchicalChunker,
    LocalFileLoader,
    SnapshotStore,
    normalize_file,
)
from reqhq.llm import MockProvider, ProviderRouter
from reqhq.pbt import (
    ContractTamperError,
    PBTSpecEmitter,
    dump_pbt_spec_yaml,
    export_pbt_bundle,
    load_pbt_spec_yaml,
    verify_contract_lock,
)
from reqhq.reporting import (
    DRY_RUN_BANNER_MD,
    DRY_RUN_BANNER_TEX,
    QUARANTINED_STATUS,
    generate_final_report,
)
from reqhq.schemas import (
    RequirementGraph,
    RequirementKind,
    RequirementNode,
    TraceLink,
)
from reqhq.store import EventKind, SessionEventLog
from reqhq.synthesis import SocioTechnicalTriager, build_routing_adrs

REPO_ROOT = Path(__file__).resolve().parents[3]
REPO_DOCS_ADR = REPO_ROOT / "docs" / "adr"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _snapshot_dir_state(directory: Path) -> dict[str, tuple[int, str]]:
    """Return a read-only {rel_posix: (size, sha256)} snapshot for namespace-isolation checks."""
    if not directory.exists():
        return {}
    state: dict[str, tuple[int, str]] = {}
    for item in sorted(directory.rglob("*")):
        if item.is_file():
            data = item.read_bytes()
            state[item.relative_to(directory).as_posix()] = (
                len(data),
                hashlib.sha256(data).hexdigest(),
            )
    return state


def _make_mock_router() -> ProviderRouter:
    """Create a deterministic offline ProviderRouter via public LLM exports."""
    return ProviderRouter(
        local_provider=MockProvider(name="system-qa-local", is_local=True),
        strict_local=True,
        enable_cache=False,
    )


def _assert_contract_lock_and_tamper_detection(bundle_dir: Path) -> None:
    """Verify valid pbt_lock.sha256 and confirm a 1-byte tamper triggers ContractTamperError."""
    verified = verify_contract_lock(bundle_dir)
    assert verified, "verify_contract_lock must return a non-empty digest map"
    assert (bundle_dir / "pbt_lock.sha256").is_file()

    # Copy into an isolated sibling directory so the state machine's bundle stays pristine.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as copy_tmp:
        tampered_dir = Path(copy_tmp) / "tampered_bundle"
        shutil.copytree(bundle_dir, tampered_dir)

        first_rel = sorted(verified.keys())[0]
        target_file = tampered_dir / first_rel
        original_bytes = target_file.read_bytes()
        assert len(original_bytes) > 0
        mutated = bytearray(original_bytes)
        mutated[0] = (mutated[0] + 1) % 256
        target_file.write_bytes(bytes(mutated))

        with pytest.raises(ContractTamperError):
            verify_contract_lock(tampered_dir)


def _assert_traceability_completeness(
    bundle_dir: Path,
    *,
    expect_pbt_chain: bool,
) -> None:
    """Assert canonical traceability chain completeness and zero orphaned requirements."""
    matrix_path = bundle_dir / "traceability_matrix.json"
    assert matrix_path.is_file(), f"Missing traceability_matrix.json in {bundle_dir}"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))

    assert matrix.get("orphaned_req_ids") == [], (
        f"Orphaned requirements found in {matrix_path}: {matrix.get('orphaned_req_ids')}"
    )
    rows = matrix.get("rows", [])
    assert len(rows) >= 1, "traceability_matrix.json must contain at least one row"

    adrs_dir = bundle_dir / "ADRs"
    assert adrs_dir.is_dir(), f"Missing target-system ADRs/ directory in {bundle_dir}"
    emitted_adr_stems = {p.stem for p in adrs_dir.glob("*.md")}

    pbt_property_ids: set[str] = set()
    if expect_pbt_chain:
        pbt_yaml = bundle_dir / "PBT_Spec.yaml"
        assert pbt_yaml.is_file(), f"Missing PBT_Spec.yaml in {bundle_dir}"
        pbt_doc = load_pbt_spec_yaml(pbt_yaml.read_text(encoding="utf-8"))
        assert len(pbt_doc.properties) >= 1, "PBT_Spec.yaml must emit at least one property"
        for prop in pbt_doc.properties:
            assert _SHA256_RE.match(prop.source_sha256), (
                f"Invalid source_sha256 on property {prop.property_id}: {prop.source_sha256!r}"
            )
            assert prop.req_id.strip(), f"Blank req_id on property {prop.property_id}"
            assert prop.adr_id.strip(), f"Blank adr_id on property {prop.property_id}"
            assert prop.property_id.strip(), "Blank property_id in PBTContractDocument"
            pbt_property_ids.add(prop.property_id)

    for row in rows:
        req_id = row["req_id"]
        assert req_id.strip(), "Blank req_id in traceability row"
        assert _SHA256_RE.match(row["source_sha256"]), (
            f"Invalid source_sha256 for {req_id}: {row['source_sha256']!r}"
        )
        adr_ids = row.get("adr_ids", [])
        assert len(adr_ids) >= 1, f"Requirement {req_id} has no linked Routing/Assumption ADRs"
        for adr_id in adr_ids:
            assert any(
                stem == adr_id or stem.startswith(f"{adr_id}-") or stem.startswith(adr_id)
                for stem in emitted_adr_stems
            ), f"ADR {adr_id!r} for {req_id} missing from {adrs_dir} (found {sorted(emitted_adr_stems)})"

        if expect_pbt_chain:
            pbt_refs = row.get("pbt_refs", [])
            track = str(row.get("track", "")).lower()
            if track in ("software", "hybrid"):
                assert row.get("alloy_refs"), (
                    f"Software/Hybrid requirement {req_id} missing alloy_refs after handoff"
                )
                assert pbt_refs, (
                    f"Software/Hybrid requirement {req_id} missing pbt_refs after handoff"
                )
            for pref in pbt_refs:
                assert pref in pbt_property_ids, (
                    f"traceability_matrix pbt_ref {pref!r} not found in PBT_Spec.yaml properties"
                )


# ---------------------------------------------------------------------------
# Deliverable 1.1: Cross-Phase Stateful CLI / EventLog / Handoff State Machine
# ---------------------------------------------------------------------------

_PROMPT_SEEDS = (
    "Build a deterministic wire settlement ledger service with dual-control compliance approval SOP and analyst certification.",
    "Design a role-based emergency triage room scheduler with immutable audit logs, bedside handover SOP, and nurse onboarding training.",
    "Create an inventory replenishment API enforcing non-negative stock invariants, manual shrinkage sign-off, and warehouse operator workshops.",
)

_SAFE_NOTE_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-.,:"


class ReqHQCrossPhaseStateMachine(RuleBasedStateMachine):
    """Stateful PBT driving real CLI transitions across Phases 1 -> 2 -> 3."""

    def __init__(self) -> None:
        super().__init__()
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root_dir = Path(self._tmp.name)
        self.sessions_dir = self.root_dir / "sessions"
        self.runner = CliRunner()
        self.session_id = "sysqa-pbt-session"
        self.step_seq = 0
        self.initialized = False
        self.elicited = False
        self.finalized = False
        self.verified = False
        self.bundle_stale = False
        self.last_export_dir: Path | None = None
        self.last_handoff_dir: Path | None = None
        self._initial_docs_adr_snapshot = _snapshot_dir_state(REPO_DOCS_ADR)

    def teardown(self) -> None:
        self._tmp.cleanup()

    def _next_dir(self, prefix: str) -> Path:
        self.step_seq += 1
        return self.root_dir / f"{prefix}_{self.step_seq}"

    @initialize(
        use_source_doc=st.booleans(),
        seed_prompt=st.sampled_from(_PROMPT_SEEDS),
    )
    def init_session(self, use_source_doc: bool, seed_prompt: str) -> None:
        cmd = [
            "init",
            "--sessions-dir",
            str(self.sessions_dir),
            "--strict-local",
            "--session-id",
            self.session_id,
        ]
        if use_source_doc:
            source_file = self.root_dir / "input_prd.md"
            source_file.write_text(
                "# Core Platform Specification\n\n"
                "## Functional Scope\n\n"
                f"The system shall satisfy: {seed_prompt}\n\n"
                "## Operational Controls\n\n"
                "Operators shall follow the written compliance approval SOP and complete role certification before production access.\n",
                encoding="utf-8",
            )
            cmd.extend(["--source", str(source_file)])
        else:
            cmd.extend(["--prompt", seed_prompt])

        res = self.runner.invoke(cli_app, cmd)
        assert res.exit_code == 0, f"init failed: {res.output}"
        self.initialized = True

    @rule(max_turns=st.integers(min_value=1, max_value=2))
    def elicit(self, max_turns: int) -> None:
        answers_file = self.root_dir / f"answers_{self.step_seq + 1}.json"
        self.step_seq += 1
        answers_file.write_text(
            json.dumps(
                [
                    {
                        "answer": (
                            "Enforce <=200ms p99 latency, immutable audit logs, "
                            "dual-control SOP approval, and certified role access."
                        )
                    }
                ]
            ),
            encoding="utf-8",
        )
        res = self.runner.invoke(
            cli_app,
            [
                "elicit",
                self.session_id,
                "--mock",
                "--max-turns",
                str(max_turns),
                "--answers-file",
                str(answers_file),
                "--sessions-dir",
                str(self.sessions_dir),
            ],
        )
        assert res.exit_code == 0, f"elicit failed: {res.output}"
        self.elicited = True

    @precondition(lambda self: self.elicited)
    @rule(
        note=st.text(alphabet=_SAFE_NOTE_ALPHABET, min_size=4, max_size=36),
        pick_earliest=st.booleans(),
    )
    def revise(self, note: str, pick_earliest: bool) -> None:
        log = SessionEventLog(self.sessions_dir)
        resumed_before = log.resume(self.session_id)
        active_turns: list[str] = list(resumed_before.get("active_turns", []))
        if not active_turns:
            return

        target_idx = 0 if pick_earliest else len(active_turns) - 1
        target_turn_id = active_turns[target_idx]
        expected_downstream = active_turns[target_idx + 1 :]
        events_before = log.load_events(self.session_id)

        clean_note = f"Revision note: {note.strip() or 'clarified constraint'}"
        res = self.runner.invoke(
            cli_app,
            [
                "revise",
                self.session_id,
                target_turn_id,
                "--note",
                clean_note,
                "--mock",
                "--sessions-dir",
                str(self.sessions_dir),
            ],
        )
        assert res.exit_code == 0, f"revise {target_turn_id} failed: {res.output}"

        events_after = log.load_events(self.session_id)
        resumed_after = log.resume(self.session_id)

        # Append-only guarantee: prior history is strictly preserved in exact order.
        assert len(events_after) > len(events_before)
        assert [e.event_id for e in events_after[: len(events_before)]] == [
            e.event_id for e in events_before
        ]

        # Revision event recorded for target turn.
        new_events = events_after[len(events_before) :]
        assert any(
            e.kind == EventKind.TURN_REVISED and e.turn_id == target_turn_id
            for e in new_events
        ), f"Expected TURN_REVISED for {target_turn_id}, got {[e.kind for e in new_events]}"

        # Downstream staleness & invalidation guarantee.
        invalidated_after = set(resumed_after.get("invalidated_turns", []))
        for downstream_turn in expected_downstream:
            assert downstream_turn in invalidated_after, (
                f"Downstream turn {downstream_turn!r} was not invalidated after revising {target_turn_id!r}"
            )
            assert any(
                e.kind == EventKind.TURN_INVALIDATED and e.turn_id == downstream_turn
                for e in new_events
            ), f"Missing TURN_INVALIDATED event for downstream turn {downstream_turn!r}"

        self.finalized = False
        self.verified = False
        self.bundle_stale = True

    @precondition(lambda self: self.elicited)
    @rule()
    def finalize(self) -> None:
        res = self.runner.invoke(
            cli_app,
            [
                "finalize",
                self.session_id,
                "--accept-defaults",
                "--mock",
                "--sessions-dir",
                str(self.sessions_dir),
            ],
        )
        assert res.exit_code == 0, f"finalize failed: {res.output}"
        self.finalized = True

    @precondition(lambda self: self.elicited)
    @rule(compat=st.sampled_from(["alloy6", "legacy"]))
    def verify(self, compat: str) -> None:
        res = self.runner.invoke(
            cli_app,
            [
                "verify",
                self.session_id,
                "--mock-alloy",
                "--alloy-compat",
                compat,
                "--sessions-dir",
                str(self.sessions_dir),
            ],
        )
        assert res.exit_code == 0, f"verify ({compat}) failed: {res.output}"
        events = SessionEventLog(self.sessions_dir).load_events(self.session_id)
        assert any(e.kind == EventKind.VERIFICATION_RECORDED for e in events)
        self.verified = True

    @precondition(lambda self: self.elicited)
    @rule(compat=st.sampled_from(["alloy6", "legacy"]))
    def export_bundle(self, compat: str) -> None:
        out_dir = self._next_dir("export")
        res = self.runner.invoke(
            cli_app,
            [
                "export",
                self.session_id,
                "--out",
                str(out_dir),
                "--alloy-compat",
                compat,
                "--mock",
                "--mock-alloy",
                "--sessions-dir",
                str(self.sessions_dir),
            ],
        )
        assert res.exit_code == 0, f"export ({compat}) failed: {res.output}"
        assert (out_dir / "Spec.md").is_file()
        assert (out_dir / "Design.md").is_file()
        assert (out_dir / "Process_and_Training.md").is_file()
        _assert_traceability_completeness(out_dir, expect_pbt_chain=False)
        self.last_export_dir = out_dir
        self.bundle_stale = False

    @precondition(lambda self: self.elicited)
    @rule()
    def pbt_export(self) -> None:
        out_dir = self._next_dir("pbt")
        res = self.runner.invoke(
            cli_app,
            [
                "pbt-export",
                self.session_id,
                "--out",
                str(out_dir),
                "--mock",
                "--sessions-dir",
                str(self.sessions_dir),
            ],
        )
        assert res.exit_code == 0, f"pbt-export failed: {res.output}"
        assert (out_dir / "PBT_Spec.yaml").is_file()
        _assert_contract_lock_and_tamper_detection(out_dir)

    @precondition(lambda self: self.elicited)
    @rule(compat=st.sampled_from(["alloy6", "legacy"]))
    def handoff(self, compat: str) -> None:
        out_dir = self._next_dir("handoff")
        res = self.runner.invoke(
            cli_app,
            [
                "handoff",
                self.session_id,
                "--out",
                str(out_dir),
                "--alloy-compat",
                compat,
                "--mock",
                "--mock-alloy",
                "--sessions-dir",
                str(self.sessions_dir),
            ],
        )
        assert res.exit_code == 0, f"handoff ({compat}) failed: {res.output}"
        assert (out_dir / "HANDOFF_INSTRUCTIONS.md").is_file()
        _assert_traceability_completeness(out_dir, expect_pbt_chain=True)
        _assert_contract_lock_and_tamper_detection(out_dir)
        self.last_handoff_dir = out_dir
        self.bundle_stale = False

    @invariant()
    def event_log_parity_and_adr_isolation(self) -> None:
        if not self.initialized:
            return
        log = SessionEventLog(self.sessions_dir)
        assert log.verify_jsonl_parity(self.session_id) is True

        events = log.load_events(self.session_id)
        assert len(events) >= 1
        assert [e.seq for e in events] == list(range(1, len(events) + 1))

        jsonl_path = self.sessions_dir / self.session_id / "events.jsonl"
        assert jsonl_path.is_file()
        jsonl_lines = [
            line for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        assert len(jsonl_lines) == len(events)

        # Repo governance docs/adr/ must remain completely untouched.
        assert _snapshot_dir_state(REPO_DOCS_ADR) == self._initial_docs_adr_snapshot


def test_cross_phase_session_lifecycle_state_machine() -> None:
    """Deterministic stateful PBT over real CLI session transitions and invariants."""
    run_state_machine_as_test(
        ReqHQCrossPhaseStateMachine,
        settings=settings(
            derandomize=True,
            max_examples=25,
            stateful_step_count=12,
            deadline=None,
        ),
    )


# ---------------------------------------------------------------------------
# Deliverable 1.2a: Ingestion -> Normalizer -> Chunker -> Decomposer -> Alloy @trace
# ---------------------------------------------------------------------------

_UNICODE_TOKENS = (
    "München Ledger — €50,000",
    "Αλφα-Βήτα Telemetry",
    "東京決済ゲートウェイ",
    "naïve façade résumé",
    "SLA ≤ 200ms ± 5%",
)

_WINDOWS_PATHS = (
    r"C:\Projects\EnterpriseApp\specs\ledger_v2.md",
    r"D:\Compliance\SOPs\dual_control\review.docx",
    r"\\fileserver\shared\audit_logs\2026\report.pdf",
)

_ASCII_WORD_ST = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ",
    min_size=12,
    max_size=60,
).map(lambda s: " ".join(s.split()) or "system requirement specification")


@st.composite
def _markdown_prd_strategy(draw: st.DrawFn) -> str:
    """Generate rich multi-section Markdown PRDs with Unicode, fenced code, tables, and Windows paths."""
    num_sections = draw(st.integers(min_value=2, max_value=5))
    parts: list[str] = ["# Enterprise Specification Document\n"]
    for idx in range(1, num_sections + 1):
        heading_tag = draw(st.sampled_from(_UNICODE_TOKENS))
        body_phrase = draw(_ASCII_WORD_ST)
        win_path = draw(st.sampled_from(_WINDOWS_PATHS))
        include_code = draw(st.booleans())
        include_table = draw(st.booleans())

        parts.append(f"## Section {idx}: {heading_tag}\n")
        parts.append(
            f"The system shall enforce invariant S{idx} for {body_phrase} "
            f"referencing artifact `{win_path}` with deterministic auditability.\n"
        )
        if include_table:
            parts.append(
                "| Control ID | Owner Role | Latency Budget |\n"
                "| --- | --- | --- |\n"
                f"| CTRL-{idx:02d} | Senior Operator | <= {idx * 100}ms |\n"
            )
        if include_code:
            parts.append(
                "```python\n"
                f"def validate_section_{idx}(amount: int) -> bool:\n"
                f"    return amount >= {idx}\n"
                "```\n"
            )
    return "\n".join(parts)


@settings(derandomize=True, max_examples=40, deadline=None)
@given(
    markdown_text=_markdown_prd_strategy(),
    max_chunk_chars=st.integers(min_value=300, max_value=1600),
)
def test_ingestion_to_alloy_trace_roundtrip_pbt(
    markdown_text: str,
    max_chunk_chars: int,
) -> None:
    """Full Ingestion -> Normalizer -> Chunker -> Decomposer -> Alloy @trace round-trip."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_str:
        tmp = Path(tmp_str)
        src_path = tmp / "generated_prd.md"
        src_path.write_text(markdown_text, encoding="utf-8")

        loader = LocalFileLoader()
        source_doc = loader.load_file(src_path)

        snapshots = SnapshotStore(tmp / "sessions")
        snap = snapshots.save_document("pbt-ingest", source_doc)
        assert snapshots.verify_snapshot(snap) is True

        normalized = normalize_file(snap.snapshot_path, source_sha256=snap.sha256)
        assert normalized.source_sha256 == snap.sha256
        assert normalized.char_count == len(normalized.markdown)

        chunker = HierarchicalChunker(max_chunk_chars=max_chunk_chars, overlap_chars=0)
        chunk_res = chunker.chunk(normalized)

        assert chunk_res.truncation_ratio == 0.0
        assert chunk_res.covered_chars == chunk_res.total_chars
        assert chunk_res.chunk_count == len(chunk_res.chunks) >= 1

        chunk_ids = {c.chunk_id for c in chunk_res.chunks}
        for chunk in chunk_res.chunks:
            assert chunk.source_sha256 == snap.sha256
            assert 0 <= chunk.start_char < chunk.end_char <= chunk_res.total_chars
            assert chunk.char_count == len(chunk.text) > 0

        router = _make_mock_router()
        decomposer = RequirementDecomposer(router)
        graph = decomposer.decompose_chunks(
            chunk_res,
            session_id="pbt-ingest",
            source_uri=source_doc.source_uri,
        )
        assert len(graph.nodes) >= 1
        assert graph.validate_traceability(require_all=True) == []
        for node in graph.nodes:
            assert len(node.trace_links) >= 1
            for link in node.trace_links:
                assert link.source_sha256 == snap.sha256
                if link.chunk_id is not None:
                    assert link.chunk_id in chunk_ids

        emitter = AlloySpecEmitter()
        spec_md = emitter.emit_spec_md(graph, compat=AlloyCompatMode.ALLOY6)
        blocks = extract_alloy_blocks(spec_md)
        assert "core_domain" in blocks and "behavior" in blocks
        assert lint_core_domain_version_agnostic(blocks["core_domain"].code) == []

        req_to_alloy = build_req_to_alloy_refs(blocks)
        graph_req_ids = {n.req_id for n in graph.nodes}
        assert req_to_alloy, "Expected at least one @trace REQ-ID mapped from Alloy blocks"
        for traced_req_id, refs in req_to_alloy.items():
            assert traced_req_id in graph_req_ids, (
                f"Alloy @trace referenced unknown req_id {traced_req_id!r}"
            )
            assert len(refs) >= 1


def test_ingestion_decomposer_alloy_over_150k_chars_zero_truncation() -> None:
    """Dedicated >150,000-character PRD test verifying 0% truncation and exact provenance."""
    sections: list[str] = ["# Mega-Scale Enterprise PRD (>150K Chars)\n"]
    # Build >160,000 characters with Unicode, tables, code fences, and Windows paths.
    for idx in range(1, 340):
        sections.append(
            f"## Module {idx:03d}: {_UNICODE_TOKENS[idx % len(_UNICODE_TOKENS)]}\n\n"
            f"The subsystem M{idx:03d} shall validate transaction payloads stored under "
            f"`{_WINDOWS_PATHS[idx % len(_WINDOWS_PATHS)]}` and guarantee deterministic "
            f"reconciliation, role-based access enforcement, and immutable audit logging "
            f"across all settlement cutoffs with bounded latency <= {100 + (idx % 50)}ms.\n\n"
            "| Metric | Target | Enforcement |\n"
            "| --- | --- | --- |\n"
            f"| Throughput-{idx:03d} | >= 500 TPS | Runtime Guardrail |\n\n"
            "```python\n"
            f"def check_module_{idx:03d}(x: int) -> bool:\n"
            "    return x >= 0\n"
            "```\n"
        )
    huge_markdown = "\n".join(sections)
    assert len(huge_markdown) > 150_000, f"Expected >150k chars, got {len(huge_markdown)}"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_str:
        tmp = Path(tmp_str)
        prd_path = tmp / "mega_prd_150k.md"
        prd_path.write_text(huge_markdown, encoding="utf-8")

        source_doc = LocalFileLoader().load_file(prd_path)
        snap = SnapshotStore(tmp / "sessions").save_document("mega-150k", source_doc)
        normalized = normalize_file(snap.snapshot_path, source_sha256=snap.sha256)
        assert normalized.char_count > 150_000

        chunk_res = HierarchicalChunker(max_chunk_chars=8000, overlap_chars=0).chunk(normalized)
        assert chunk_res.total_chars > 150_000
        assert chunk_res.truncation_ratio == 0.0
        assert chunk_res.covered_chars == chunk_res.total_chars
        assert chunk_res.chunk_count >= 15

        # Verify monotonic span preservation across all chunks.
        assert chunk_res.chunks[0].start_char == 0
        assert chunk_res.chunks[-1].end_char == chunk_res.total_chars
        for chunk in chunk_res.chunks:
            assert chunk.source_sha256 == snap.sha256
            assert chunk.start_char < chunk.end_char

        graph = RequirementDecomposer(_make_mock_router()).decompose_chunks(
            chunk_res,
            session_id="mega-150k",
            source_uri=source_doc.source_uri,
        )
        assert len(graph.nodes) >= 1
        assert graph.validate_traceability(require_all=True) == []
        assert all(
            link.source_sha256 == snap.sha256
            for node in graph.nodes
            for link in node.trace_links
        )

        spec_md = AlloySpecEmitter().emit_spec_md(graph, compat=AlloyCompatMode.ALLOY6)
        blocks = extract_alloy_blocks(spec_md)
        assert lint_core_domain_version_agnostic(blocks["core_domain"].code) == []
        req_to_alloy = build_req_to_alloy_refs(blocks)
        assert req_to_alloy


# ---------------------------------------------------------------------------
# Deliverable 1.2b: Spec.md (alloy6 & legacy) -> PBT_Spec.yaml -> codegen -> ast.parse -> pytest
# ---------------------------------------------------------------------------

_SAMPLE_STATEMENTS = (
    "The payment engine shall persist immutable ledger entries with non-negative settlement balances.",
    "The authentication service shall block sessions after consecutive failed login attempts and log audit events.",
    "The inventory API shall guarantee monotonic stock reservations without overselling available capacity.",
    "Compliance officers shall execute dual-control manual sign-off for high-value exceptions exceeding $50,000.",
    "Operators must complete role-based certification workshops prior to receiving production console credentials.",
)

_SUBPROCESS_RUNS_BUDGET = {"alloy6": 0, "legacy": 0}
_MAX_SUBPROCESS_RUNS_PER_MODE = 2


@st.composite
def _requirement_graph_strategy(draw: st.DrawFn) -> RequirementGraph:
    count = draw(st.integers(min_value=1, max_value=4))
    seed_bytes = draw(st.binary(min_size=16, max_size=64))
    source_sha = hashlib.sha256(seed_bytes).hexdigest()
    nodes: list[RequirementNode] = []
    for idx in range(1, count + 1):
        stmt = draw(st.sampled_from(_SAMPLE_STATEMENTS))
        kind = draw(
            st.sampled_from(
                [
                    RequirementKind.FUNCTIONAL,
                    RequirementKind.NON_FUNCTIONAL,
                    RequirementKind.SECURITY,
                    RequirementKind.DATA,
                    RequirementKind.PROCESS,
                ]
            )
        )
        nodes.append(
            RequirementNode(
                req_id=f"REQ-{idx:03d}",
                title=f"Requirement {idx}",
                statement=stmt,
                kind=kind,
                trace_links=[
                    TraceLink(
                        source_sha256=source_sha,
                        source_uri="file://pbt_contract_test.md",
                        anchor=f"[section:req-{idx}]",
                        excerpt=stmt[:80],
                    )
                ],
            )
        )
    return RequirementGraph(graph_id="pbt-codegen-graph", nodes=nodes)


@settings(derandomize=True, max_examples=40, deadline=None)
@given(
    graph=_requirement_graph_strategy(),
    compat=st.sampled_from([AlloyCompatMode.ALLOY6, AlloyCompatMode.LEGACY]),
    seed=st.integers(min_value=1, max_value=9999),
)
def test_spec_to_pbt_yaml_codegen_ast_and_pytest_pbt(
    graph: RequirementGraph,
    compat: AlloyCompatMode,
    seed: int,
) -> None:
    """Spec.md (alloy6/legacy) -> PBT_Spec.yaml -> Python codegen -> ast.parse -> real pytest."""
    router = _make_mock_router()
    plan = SocioTechnicalTriager(router).triage(graph, session_id="pbt-codegen")
    adrs = build_routing_adrs(graph, plan)
    spec_md = AlloySpecEmitter().emit_spec_md(
        graph,
        triage_plan=plan,
        adrs=adrs,
        compat=compat,
    )

    blocks = extract_alloy_blocks(spec_md)
    assert lint_core_domain_version_agnostic(blocks["core_domain"].code) == []
    if compat == AlloyCompatMode.LEGACY:
        assert "util/ordering" in blocks["behavior"].code

    pbt_doc = PBTSpecEmitter().emit_contract(
        graph,
        session_id="pbt-codegen",
        spec_markdown=spec_md,
        adrs=adrs,
        seed=seed,
    )
    yaml_text = dump_pbt_spec_yaml(pbt_doc)
    reloaded = load_pbt_spec_yaml(yaml_text)
    assert reloaded.session_id == pbt_doc.session_id
    assert len(reloaded.properties) == len(pbt_doc.properties) >= 1

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_str:
        out_dir = Path(tmp_str)
        bundle = export_pbt_bundle(reloaded, out_dir)
        assert bundle.pbt_spec_yaml.is_file()
        assert bundle.lock_file.is_file()
        assert len(bundle.guardrail_files) >= 1
        assert len(bundle.test_files) >= 1

        # Verify every generated Python file parses cleanly via ast.parse.
        for py_path in [*bundle.guardrail_files, *bundle.test_files]:
            source = py_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_path))
            assert isinstance(tree, ast.Module)

        # Verify SHA-256 contract lock passes cleanly.
        lock_map = verify_contract_lock(out_dir)
        assert len(lock_map) >= 1 + len(bundle.guardrail_files) + len(bundle.test_files)

        # Execute real pytest + Hypothesis subprocess on representative examples for each mode
        # so all 40 examples validate AST/YAML/lock while real execution stays bounded & fast.
        mode_key = compat.value
        if _SUBPROCESS_RUNS_BUDGET[mode_key] < _MAX_SUBPROCESS_RUNS_PER_MODE:
            _SUBPROCESS_RUNS_BUDGET[mode_key] += 1
            env = os.environ.copy()
            existing_pp = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (
                f"{out_dir}{os.pathsep}{existing_pp}" if existing_pp else str(out_dir)
            )
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", str(out_dir / "tests" / "properties")],
                cwd=str(out_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            assert proc.returncode == 0, (
                f"Generated PBT suite failed under {mode_key}!\n"
                f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )


# ---------------------------------------------------------------------------
# Deliverable 1.2c: eval run interruption -> eval resume idempotency + Mock Quarantine
# ---------------------------------------------------------------------------

_EVAL_BASELINE_CACHE: dict[int, tuple[dict[str, Any], dict[str, list[str]]]] = {}
_LAYER_FILES = (
    ("layer1", "layer1.jsonl"),
    ("layer2", "layer2.jsonl"),
    ("layer3", "layer3.jsonl"),
    ("layer4", "layer4_telemetry.jsonl"),
)


def _get_baseline_eval_run(seed: int) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Run and cache a deterministic baseline mock eval (replicates=2, max_tasks=1) per seed."""
    if seed in _EVAL_BASELINE_CACHE:
        return _EVAL_BASELINE_CACHE[seed]

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_str:
        out_dir = Path(tmp_str) / "baseline"
        cfg = EvalRunConfig(
            out_dir=out_dir,
            layers=[1, 2, 3, 4],
            mode=RunMode.MOCK,
            replicates=2,
            seed=seed,
            max_tasks=1,
        )
        EvalOrchestrator(cfg, frozen_root=REPO_ROOT / "data" / "frozen").run_all()
        checkpoint = json.loads((out_dir / "checkpoint.json").read_text(encoding="utf-8"))
        layer_lines: dict[str, list[str]] = {}
        for _, fname in _LAYER_FILES:
            fpath = out_dir / fname
            layer_lines[fname] = [
                ln for ln in fpath.read_text(encoding="utf-8").splitlines() if ln.strip()
            ]
        _EVAL_BASELINE_CACHE[seed] = (checkpoint, layer_lines)
        return _EVAL_BASELINE_CACHE[seed]


def _extract_record_key(fname: str, raw_line: str) -> str:
    obj = json.loads(raw_line)
    if fname == "layer4_telemetry.jsonl":
        return str(obj.get("stage", raw_line))
    return str(obj.get("record_id", raw_line))


def _unit_key_for_row(fname: str, raw_line: str) -> str:
    obj = json.loads(raw_line)
    if fname == "layer4_telemetry.jsonl":
        return f"L4:{obj['stage']}:default:r1"
    return str(obj["record_id"])


@settings(derandomize=True, max_examples=40, deadline=None)
@given(
    seed=st.sampled_from([17, 42, 99]),
    cut_ratio=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
)
def test_eval_interruption_and_resume_idempotency_pbt(
    seed: int,
    cut_ratio: float,
) -> None:
    """Interrupting eval at arbitrary step k and resuming yields zero duplicates & exact parity."""
    baseline_cp, baseline_lines = _get_baseline_eval_run(seed)
    all_cp_units = set(baseline_cp["completed_units"])

    # Order units in true execution order (layer1 -> layer2 -> layer3 -> layer4)
    # so slicing at arbitrary step k simulates an authentic mid-run crash.
    exec_ordered_units: list[str] = []
    for _, fname in _LAYER_FILES:
        for ln in baseline_lines[fname]:
            unit_key = _unit_key_for_row(fname, ln)
            if unit_key in all_cp_units and unit_key not in exec_ordered_units:
                exec_ordered_units.append(unit_key)
    for unit_key in baseline_cp["completed_units"]:
        if unit_key not in exec_ordered_units:
            exec_ordered_units.append(unit_key)

    assert len(exec_ordered_units) >= 4
    cut_k = int(round(cut_ratio * len(exec_ordered_units)))
    kept_units = sorted(exec_ordered_units[:cut_k])
    kept_set = set(kept_units)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_str:
        work_dir = Path(tmp_str) / "interrupted"
        work_dir.mkdir(parents=True, exist_ok=True)

        # Write partial checkpoint (pointing config.out_dir to work_dir) & exact matching JSONL rows.
        partial_cp = json.loads(json.dumps(baseline_cp))
        partial_cp["config"]["out_dir"] = str(work_dir)
        partial_cp["completed_units"] = kept_units
        (work_dir / "checkpoint.json").write_text(
            json.dumps(partial_cp, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        for _, fname in _LAYER_FILES:
            partial_rows = [
                ln for ln in baseline_lines[fname] if _unit_key_for_row(fname, ln) in kept_set
            ]
            if partial_rows:
                (work_dir / fname).write_text(
                    "\n".join(partial_rows) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )

        runner = CliRunner()
        res1 = runner.invoke(cli_app, ["eval", "resume", "--out", str(work_dir)])
        assert res1.exit_code == 0, f"eval resume (cut_k={cut_k}) failed: {res1.output}"

        # Capture post-resume files and verify exact record counts + zero duplicates.
        first_resume_bytes: dict[str, bytes] = {}
        for _, fname in _LAYER_FILES:
            fpath = work_dir / fname
            assert fpath.is_file(), f"Missing {fname} after eval resume"
            raw_bytes = fpath.read_bytes()
            first_resume_bytes[fname] = raw_bytes
            resumed_lines = [
                ln for ln in raw_bytes.decode("utf-8").splitlines() if ln.strip()
            ]
            assert len(resumed_lines) == len(baseline_lines[fname]), (
                f"{fname} length mismatch after resume (cut_k={cut_k}): "
                f"{len(resumed_lines)} != {len(baseline_lines[fname])}"
            )
            keys = [_extract_record_key(fname, ln) for ln in resumed_lines]
            assert len(keys) == len(set(keys)), f"Duplicate keys in {fname}: {keys}"
            baseline_keys = [_extract_record_key(fname, ln) for ln in baseline_lines[fname]]
            assert sorted(keys) == sorted(baseline_keys)

        # Second resume on completed run MUST be strictly byte-identical.
        res2 = runner.invoke(cli_app, ["eval", "resume", "--out", str(work_dir)])
        assert res2.exit_code == 0, f"Second eval resume failed: {res2.output}"
        for _, fname in _LAYER_FILES:
            assert (work_dir / fname).read_bytes() == first_resume_bytes[fname], (
                f"{fname} mutated on idempotent second eval resume"
            )


@settings(derandomize=True, max_examples=40, deadline=None)
@given(
    poison_layer_idx=st.integers(min_value=0, max_value=3),
    poison_row_offset=st.integers(min_value=0, max_value=5),
    spoof_live_manifest=st.booleans(),
)
def test_eval_mock_and_mixed_record_quarantine_fail_closed_pbt(
    poison_layer_idx: int,
    poison_row_offset: int,
    spoof_live_manifest: bool,
) -> None:
    """Any mock or mixed live+mock record MUST fail closed on --strict-live and never leak verdicts."""
    _, baseline_lines = _get_baseline_eval_run(42)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_str:
        tmp = Path(tmp_str)
        eval_dir = tmp / "eval_mixed"
        analysis_dir = tmp / "analysis"
        report_dir = tmp / "report"
        eval_dir.mkdir(parents=True, exist_ok=True)

        log_sha256: dict[str, str] = {}
        record_counts: dict[str, int] = {}

        for idx, (layer_key, fname) in enumerate(_LAYER_FILES):
            rows = [json.loads(ln) for ln in baseline_lines[fname]]
            assert len(rows) >= 1
            if spoof_live_manifest:
                # Claim every record is live EXCEPT one poisoned mock record in poison_layer_idx.
                for r in rows:
                    r["run_mode"] = "live"
                    if "gold_verified" in r:
                        r["gold_verified"] = True
                if idx == poison_layer_idx:
                    rows[poison_row_offset % len(rows)]["run_mode"] = "mock"
            else:
                # Pure mock run.
                for r in rows:
                    r["run_mode"] = "mock"

            fpath = eval_dir / fname
            fpath.write_text(
                "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            log_sha256[layer_key] = sha256_file(fpath)
            record_counts[layer_key] = len(rows)

        manifest = {
            "run_id": "mixed-quarantine-pbt",
            "run_mode": "live" if spoof_live_manifest else "mock",
            "seed": 42,
            "replicates": 2,
            "layers_completed": [1, 2, 3, 4],
            "gold_verified": True,
            "hardware_profile": {
                "tier": 0,
                "gpu_name": "Test-GPU",
                "vram_gb": 2.0,
                "ram_gb": 16.0,
            },
            "log_files": {k: fn for k, fn in _LAYER_FILES},
            "log_sha256": log_sha256,
            "record_counts": record_counts,
            "created_at": "2026-11-25T12:00:00Z",
        }
        (eval_dir / "eval_manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        # 1. API fail-closed check: strict_live=True MUST raise MockRunQuarantineError.
        with pytest.raises(MockRunQuarantineError):
            analyze_evaluation(eval_dir, analysis_dir / "strict", strict_live=True, n_resamples=100)

        # 2. CLI fail-closed check: `reqhq eval analyze --strict-live` MUST exit non-zero.
        runner = CliRunner()
        strict_res = runner.invoke(
            cli_app,
            [
                "eval",
                "analyze",
                "--in",
                str(eval_dir),
                "--out",
                str(analysis_dir / "cli_strict"),
                "--strict-live",
                "--resamples",
                "100",
            ],
        )
        assert strict_res.exit_code != 0, (
            f"CLI eval analyze --strict-live MUST fail closed on mixed/mock records! "
            f"spoof_live_manifest={spoof_live_manifest}, poison_layer_idx={poison_layer_idx}"
        )

        # 3. Non-strict analysis MUST quarantine all H1-H4 verdicts as DRY_RUN_UNSCORED.
        summary = analyze_evaluation(
            eval_dir,
            analysis_dir / "quarantined",
            strict_live=False,
            n_resamples=100,
        )
        assert summary.quarantined is True
        assert summary.verdict_status == "DRY_RUN_UNSCORED"
        assert len(summary.hypotheses) == 4
        for hyp in summary.hypotheses:
            assert hyp.status == HypothesisVerdictStatus.QUARANTINED_MOCK_OR_INCOMPLETE

        # 4. Report generation MUST stamp quarantine banners and leak ZERO live verdicts.
        report_res = generate_final_report(analysis_dir / "quarantined", report_dir, fmt="both")
        assert report_res.quarantined is True
        assert report_res.verdict_status == "DRY_RUN_UNSCORED"
        assert report_res.report_md is not None and report_res.tables_tex is not None

        md_text = report_res.report_md.read_text(encoding="utf-8")
        tex_text = report_res.tables_tex.read_text(encoding="utf-8")
        assert DRY_RUN_BANNER_MD in md_text
        assert DRY_RUN_BANNER_TEX in tex_text
        assert QUARANTINED_STATUS in md_text
        for forbidden in ("VERIFIED_LIVE", "| SUPPORTED |", "| REFUTED |"):
            assert forbidden not in md_text, f"Forbidden live verdict token {forbidden!r} leaked into FINAL_REPORT.md"
        for forbidden_tex in ("VERIFIED\\_LIVE", "VERIFIED_LIVE", "& SUPPORTED", "& REFUTED"):
            assert forbidden_tex not in tex_text, (
                f"Forbidden live verdict token {forbidden_tex!r} leaked into tables_latex.tex"
            )
