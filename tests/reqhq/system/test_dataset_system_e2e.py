"""Independent black-box real-dataset system E2E suite for ReqHQ v2 (Phase 4b).

Strictly enforces the DEV-SPLIT-ONLY dataset rule: ONLY verified frozen dev
documents (`0000 - cctns.pdf.txt`, `0000 - gamma j.pdf.txt` + their dev gold
JSONs) and `data/benchmarks/cross_domain/cases.json` may be read. Any access to
`data/frozen/*/test/` fails immediately via runtime guard.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx
import pytest

from reqhq.alloy import (
    AlloyCompatMode,
    AlloyRunStatus,
    AlloySpecEmitter,
    AlloyVerificationController,
    HeadlessAlloyRunner,
    MockAlloyAnalyzer,
    extract_alloy_blocks,
    lint_core_domain_version_agnostic,
)
from reqhq.analysis import analyze_evaluation
from reqhq.benchmark import load_cross_domain_suite, validate_gold_record_dict
from reqhq.elicitation import ElicitationEngine, RequirementDecomposer
from reqhq.evaluation import (
    EvalOrchestrator,
    EvalRunConfig,
    RunMode,
    StageTelemetryCollector,
    get_process_peak_ram_mb,
)
from reqhq.harness import ASTMutantEngine, build_handoff_bundle
from reqhq.ingestion import (
    HierarchicalChunker,
    LocalFileLoader,
    SnapshotStore,
    normalize_file,
)
from reqhq.llm import LLMRequest, MockProvider, OllamaLocalProvider, ProviderRouter
from reqhq.pbt import ContractTamperError, PBTSpecEmitter, verify_contract_lock
from reqhq.reporting import (
    EXPECTED_SVGS,
    BundleTamperError,
    generate_final_report,
    package_release_bundle,
    verify_bundle,
)
from reqhq.store import SessionEventLog
from reqhq.synthesis import (
    SocioTechnicalTriager,
    build_assumption_adrs,
    build_routing_adrs,
    write_synthesis_bundle,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
FROZEN_ROOT = REPO_ROOT / "data" / "frozen"
REPO_DOCS_ADR = REPO_ROOT / "docs" / "adr"

DEV_PURE_CCTNS_TEXT = FROZEN_ROOT / "pure" / "text" / "dev" / "0000 - cctns.pdf.txt"
DEV_PURE_CCTNS_GOLD = FROZEN_ROOT / "gold" / "dev" / "pure-0000_-_cctns.gold.json"
DEV_PURE_GAMMA_TEXT = FROZEN_ROOT / "pure" / "text" / "dev" / "0000 - gamma j.pdf.txt"
DEV_PURE_GAMMA_GOLD = FROZEN_ROOT / "gold" / "dev" / "pure-0000_-_gamma_j.gold.json"
CROSS_DOMAIN_CASES_JSON = REPO_ROOT / "data" / "benchmarks" / "cross_domain" / "cases.json"

DEFAULT_ALLOY_JAR = Path("C:/tools/alloy/org.alloytools.alloy.dist.jar")


def assert_dev_split_only(path: Path | str) -> Path:
    """Fail immediately if ANY resolved path touches a `test` split under `data/frozen/`."""
    resolved = Path(path).resolve()
    posix_lower = resolved.as_posix().lower()
    frozen_lower = FROZEN_ROOT.resolve().as_posix().lower()
    if posix_lower.startswith(frozen_lower):
        rel_parts = [p.lower() for p in resolved.relative_to(FROZEN_ROOT.resolve()).parts]
        if "test" in rel_parts:
            raise AssertionError(
                f"FORBIDDEN TEST-SPLIT ACCESS DETECTED under data/frozen/: {resolved}"
            )
    if "/data/frozen/" in posix_lower and "/test/" in posix_lower.split("/data/frozen/", 1)[1]:
        raise AssertionError(
            f"FORBIDDEN TEST-SPLIT PATH PATTERN DETECTED: {resolved}"
        )
    return resolved


def _snapshot_tree_hashes(directory: Path) -> dict[str, str]:
    """Read-only SHA-256 snapshot of all files in `directory`."""
    if not directory.exists():
        return {}
    return {
        p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_java_and_alloy_runner() -> HeadlessAlloyRunner | None:
    """Construct a HeadlessAlloyRunner using env/default JAR and Temurin Java 21 if available."""
    jar_env = os.environ.get("REQHQ_ALLOY_JAR_PATH")
    jar_path = Path(jar_env) if jar_env else DEFAULT_ALLOY_JAR
    if not jar_path.is_file():
        return None

    java_bin = shutil.which("java")
    if not java_bin:
        adoptium_root = Path("C:/Program Files/Eclipse Adoptium")
        if adoptium_root.is_dir():
            candidates = sorted(adoptium_root.glob("jre-*/bin/java.exe"))
            if candidates:
                java_bin = str(candidates[0])
    if not java_bin:
        return None

    runner = HeadlessAlloyRunner(jar_path=jar_path, java_bin=java_bin, timeout_seconds=45.0)
    return runner if runner.is_available() else None


def _make_mock_router() -> ProviderRouter:
    return ProviderRouter(
        local_provider=MockProvider(name="dataset-e2e-local", is_local=True),
        strict_local=True,
        enable_cache=False,
    )


def test_dev_split_runtime_guard_blocks_frozen_test_split() -> None:
    """Verify the dev-split runtime guard immediately rejects any data/frozen/*/test/ path."""
    # Allowed dev paths pass cleanly without error.
    for allowed in (
        DEV_PURE_CCTNS_TEXT,
        DEV_PURE_CCTNS_GOLD,
        DEV_PURE_GAMMA_TEXT,
        DEV_PURE_GAMMA_GOLD,
        CROSS_DOMAIN_CASES_JSON,
    ):
        resolved = assert_dev_split_only(allowed)
        assert resolved.is_file(), f"Required dev dataset file missing: {resolved}"

    # Synthetic test-split paths MUST fail immediately (never opened or listed).
    forbidden_paths = (
        FROZEN_ROOT / "pure" / "text" / "test" / "forbidden.txt",
        FROZEN_ROOT / "gold" / "test" / "forbidden.gold.json",
        FROZEN_ROOT / "promise_nfr" / "test" / "forbidden.csv",
    )
    for forbidden in forbidden_paths:
        with pytest.raises(AssertionError, match="FORBIDDEN TEST-SPLIT"):
            assert_dev_split_only(forbidden)


@pytest.mark.parametrize(
    ("case_label", "source_kind"),
    [
        ("pure_dev_cctns", "cctns"),
        ("pure_dev_gamma_j", "gamma_j"),
        ("cross_domain_fin03", "cross_domain"),
    ],
)
def test_real_dataset_phase1_to_phase4_full_e2e(
    case_label: str,
    source_kind: str,
) -> None:
    """End-to-end Phase 1 -> 2 -> 3 -> 4 verification on real frozen dev PRDs & cross-domain case."""
    docs_adr_before = _snapshot_tree_hashes(REPO_DOCS_ADR)
    dev_files_to_guard = [
        assert_dev_split_only(DEV_PURE_CCTNS_TEXT),
        assert_dev_split_only(DEV_PURE_CCTNS_GOLD),
        assert_dev_split_only(DEV_PURE_GAMMA_TEXT),
        assert_dev_split_only(DEV_PURE_GAMMA_GOLD),
        assert_dev_split_only(CROSS_DOMAIN_CASES_JSON),
    ]
    dev_hashes_before = {p: _file_sha256(p) for p in dev_files_to_guard}

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_str:
        tmp = Path(tmp_str)
        sessions_dir = tmp / "sessions"
        session_id = f"e2e-{case_label}"
        router = _make_mock_router()
        event_log = SessionEventLog(sessions_dir)
        event_log.create_session(session_id=session_id, metadata={"case": case_label})

        # ------------------------------------------------------------------
        # Phase 1: SnapshotStore (SHA-256), normalize_file, HierarchicalChunker
        # ------------------------------------------------------------------
        if source_kind == "cctns":
            src_path = assert_dev_split_only(DEV_PURE_CCTNS_TEXT)
            gold_path = assert_dev_split_only(DEV_PURE_CCTNS_GOLD)
            gold_doc = validate_gold_record_dict(
                json.loads(gold_path.read_text(encoding="utf-8")),
                split="dev",
            )
            assert gold_doc.item_id == "pure-0000_-_cctns"
            assert len(gold_doc.requirements) >= 10
        elif source_kind == "gamma_j":
            src_path = assert_dev_split_only(DEV_PURE_GAMMA_TEXT)
            gold_path = assert_dev_split_only(DEV_PURE_GAMMA_GOLD)
            gold_doc = validate_gold_record_dict(
                json.loads(gold_path.read_text(encoding="utf-8")),
                split="dev",
            )
            assert gold_doc.item_id == "pure-0000_-_gamma_j"
            assert len(gold_doc.requirements) >= 10
        else:
            suite = load_cross_domain_suite(assert_dev_split_only(CROSS_DOMAIN_CASES_JSON))
            target_case = next(c for c in suite.cases if c.case_id == "FIN-03")
            src_path = tmp / "fin03_cross_domain.md"
            src_path.write_text(
                f"# {target_case.title}\n\n"
                f"{target_case.prompt_text}\n\n"
                "## Gold Requirements\n\n"
                + "\n".join(f"- {r}" for r in target_case.gold_requirements)
                + "\n",
                encoding="utf-8",
            )

        source_doc = LocalFileLoader().load_file(src_path)
        snapshot_store = SnapshotStore(sessions_dir)
        snap = snapshot_store.save_document(session_id, source_doc)
        assert snapshot_store.verify_snapshot(snap) is True

        normalized = normalize_file(snap.snapshot_path, source_sha256=snap.sha256)
        assert normalized.char_count > 0
        chunk_res = HierarchicalChunker(max_chunk_chars=8000, overlap_chars=0).chunk(normalized)
        assert chunk_res.truncation_ratio == 0.0
        assert chunk_res.covered_chars == chunk_res.total_chars
        assert chunk_res.chunk_count >= 1

        # ------------------------------------------------------------------
        # Phase 2a: 5-Critic Elicitation + Delta Loop + Finalize
        # ------------------------------------------------------------------
        engine = ElicitationEngine(router, event_log)
        state = engine.start_from_chunks(
            session_id,
            chunk_res,
            source_uri=source_doc.source_uri,
        )
        assert len(state.graph.nodes) >= 1
        assert state.graph.validate_traceability(require_all=True) == []

        if state.pending_questions:
            answers = [
                {
                    "question_id": q.question_id,
                    "answer": f"Enforce deterministic SLA, audit trail, and role sign-off for {q.question_id}.",
                }
                for q in state.pending_questions[:3]
            ]
            state = engine.run_turn(state, answers)
        else:
            state = engine.run_turn(state, [])

        state = engine.finalize(state, accept_defaults=True)
        assert state.finalized is True
        assert event_log.verify_jsonl_parity(session_id) is True

        # ------------------------------------------------------------------
        # Phase 2b: Socio-Technical Triage + Routing ADRs (zero writes to docs/adr/)
        # ------------------------------------------------------------------
        plan = SocioTechnicalTriager(router).triage(state.graph, session_id=session_id)
        assert len(plan.items) == len(state.graph.nodes)
        routing_adrs = build_routing_adrs(state.graph, plan)
        assumption_adrs = build_assumption_adrs(state.assumptions)
        assert len(routing_adrs) >= 1

        # ------------------------------------------------------------------
        # Phase 2c: Literate Alloy 6 Emission + Real Java/Alloy 6 Verification Gate
        # ------------------------------------------------------------------
        spec_md = AlloySpecEmitter().emit_spec_md(
            state.graph,
            triage_plan=plan,
            adrs=[*routing_adrs, *assumption_adrs],
            compat=AlloyCompatMode.ALLOY6,
            max_scope=4,
        )
        blocks = extract_alloy_blocks(spec_md)
        assert "core_domain" in blocks and "behavior" in blocks
        assert lint_core_domain_version_agnostic(blocks["core_domain"].code) == []

        live_runner = _resolve_java_and_alloy_runner()
        if live_runner is not None:
            analyzer = live_runner
        else:
            warnings.warn(
                "Java or REQHQ_ALLOY_JAR_PATH unavailable; falling back to MockAlloyAnalyzer",
                RuntimeWarning,
                stacklevel=2,
            )
            analyzer = MockAlloyAnalyzer()

        alloy_controller = AlloyVerificationController(
            router,
            analyzer,
            max_local_repairs=2,
            max_scope=4,
            work_dir=tmp / "alloy_work",
        )
        outcome = alloy_controller.verify_spec(
            spec_md,
            state.graph,
            compat=AlloyCompatMode.ALLOY6,
            event_log=event_log,
            session_id=session_id,
        )
        assert outcome.status == AlloyRunStatus.VERIFIED, (
            f"Alloy verification failed for {case_label}: status={outcome.status}, "
            f"results={[r.model_dump() for r in outcome.run_results]}"
        )
        assert outcome.vacuity_checked is True
        assert any(r.vacuity_passed for r in outcome.run_results)
        assert event_log.verify_jsonl_parity(session_id) is True

        synth_dir = tmp / "synthesis"
        synth_res = write_synthesis_bundle(
            synth_dir,
            session_id=session_id,
            graph=state.graph,
            plan=plan,
            assumptions=state.assumptions,
            alloy_refs=outcome.req_to_alloy_refs,
            spec_markdown=outcome.spec_markdown,
            raise_on_orphans=True,
        )
        assert synth_res.orphaned_req_ids == []
        assert (synth_dir / "ADRs").is_dir()
        assert _snapshot_tree_hashes(REPO_DOCS_ADR) == docs_adr_before, (
            "Synthesis bundle MUST NEVER write into repo docs/adr/!"
        )

        # ------------------------------------------------------------------
        # Phase 3: PBT_Spec.yaml + guardrails/ + locked tests/properties/ + Execution + ASTMutantEngine
        # ------------------------------------------------------------------
        pbt_doc = PBTSpecEmitter().emit_contract(
            state.graph,
            session_id=session_id,
            spec_markdown=outcome.spec_markdown,
            adrs=[*routing_adrs, *assumption_adrs],
            design_markdown=synth_res.design_md.read_text(encoding="utf-8"),
            seed=42,
        )
        handoff_dir = tmp / "handoff"
        handoff_manifest = build_handoff_bundle(
            handoff_dir,
            session_id=session_id,
            graph=state.graph,
            plan=plan,
            spec_markdown=outcome.spec_markdown,
            pbt_doc=pbt_doc,
            assumptions=state.assumptions,
            alloy_refs=outcome.req_to_alloy_refs,
        )
        assert handoff_manifest.pbt_spec_yaml.is_file()
        assert handoff_manifest.lock_file.is_file()
        assert len(handoff_manifest.guardrail_files) >= 1
        assert len(handoff_manifest.pbt_test_files) >= 1
        assert verify_contract_lock(handoff_dir)

        # Verify all generated Python guardrails and PBT test modules parse cleanly via ast.parse.
        for py_path in [*handoff_manifest.guardrail_files, *handoff_manifest.pbt_test_files]:
            ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))

        # Execute the generated locked Hypothesis property tests in a real subprocess.
        # For large real PURE specs that emit hundreds of fallback properties (200 examples each),
        # run a bounded representative slice including stateless invariants and the stateful machine.
        env = os.environ.copy()
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{handoff_dir}{os.pathsep}{existing_pp}" if existing_pp else str(handoff_dir)
        )
        pytest_cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            str(handoff_dir / "tests" / "properties"),
        ]
        if len(pbt_doc.properties) > 25:
            pytest_cmd.extend(
                ["-k", "test_pbt_001 or test_pbt_002 or test_pbt_003 or test_pbt_004 or test_pbt_005 or StateMachine"]
            )
        pbt_proc = subprocess.run(
            pytest_cmd,
            cwd=str(handoff_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert pbt_proc.returncode == 0, (
            f"Locked PBT suite failed for {case_label}!\n"
            f"STDOUT:\n{pbt_proc.stdout}\nSTDERR:\n{pbt_proc.stderr}"
        )

        # Execute Windows-native ASTMutantEngine on a real target module + contract test.
        mut_ws = tmp / "mutation_ws"
        mut_ws.mkdir(parents=True, exist_ok=True)
        target_mod = mut_ws / "domain_core.py"
        target_mod.write_text(
            "def approve_amount(amount: int, threshold: int) -> bool:\n"
            "    if amount < 0:\n"
            "        return False\n"
            "    return amount <= threshold\n",
            encoding="utf-8",
        )
        test_mod = mut_ws / "test_domain_core.py"
        test_mod.write_text(
            "from domain_core import approve_amount\n\n"
            "def test_approve_boundaries() -> None:\n"
            "    assert approve_amount(0, 100) is True\n"
            "    assert approve_amount(100, 100) is True\n"
            "    assert approve_amount(101, 100) is False\n"
            "    assert approve_amount(-1, 100) is False\n",
            encoding="utf-8",
        )
        mut_engine = ASTMutantEngine(
            timeout_seconds=15.0,
            max_mutants=6,
            python_executable=sys.executable,
        )
        mut_report = mut_engine.run(mut_ws, [target_mod], [test_mod])
        assert mut_report.total_mutants >= 1
        assert 0.0 <= mut_report.mutation_score <= 1.0
        assert mut_report.killed >= 1

        # ------------------------------------------------------------------
        # Phase 4: EvalOrchestrator + Pure-SVG Pareto XML + Report + Release Bundle Verify
        # ------------------------------------------------------------------
        if source_kind == "cctns":
            eval_dir = tmp / "eval_run"
            analysis_dir = eval_dir / "analysis"
            report_dir = eval_dir / "report"
            bundle_dir = tmp / "release_bundle"

            eval_cfg = EvalRunConfig(
                out_dir=eval_dir,
                layers=[1, 2, 3, 4],
                mode=RunMode.MOCK,
                replicates=2,
                seed=42,
                max_tasks=1,
            )
            eval_manifest = EvalOrchestrator(eval_cfg, frozen_root=FROZEN_ROOT).run_all()
            assert eval_manifest.layers_completed == [1, 2, 3, 4]

            summary = analyze_evaluation(eval_dir, analysis_dir, strict_live=False, n_resamples=200)
            assert summary.quarantined is True
            assert summary.verdict_status == "DRY_RUN_UNSCORED"

            # Verify every emitted pure-SVG figure parses cleanly via xml.etree.ElementTree.
            for svg_name, _svg_title in EXPECTED_SVGS:
                svg_path = analysis_dir / svg_name
                assert svg_path.is_file(), f"Missing expected SVG chart: {svg_path}"
                svg_text = svg_path.read_text(encoding="utf-8")
                root = ET.fromstring(svg_text)
                assert root.tag.endswith("svg"), f"Root tag is not <svg>: {root.tag}"
                assert "DRY-RUN MOCK DATA" in svg_text

            report_res = generate_final_report(analysis_dir, report_dir, fmt="both")
            assert report_res.report_md is not None and report_res.report_md.is_file()
            assert report_res.tables_tex is not None and report_res.tables_tex.is_file()

            release_manifest = package_release_bundle(eval_dir, bundle_dir, frozen_root=FROZEN_ROOT)
            assert len(release_manifest.artifacts) >= 1
            manifest_path = bundle_dir / "artifacts_manifest.json"
            verify_res = verify_bundle(manifest_path, raise_on_error=True)
            assert verify_res.valid is True
            assert verify_res.checked_count == len(release_manifest.artifacts)

            # Confirm 1-byte tamper in packaged bundle fails closed with BundleTamperError.
            tampered_bundle = tmp / "tampered_release_bundle"
            shutil.copytree(bundle_dir, tampered_bundle)
            first_artifact = tampered_bundle / release_manifest.artifacts[0].rel_path
            raw = bytearray(first_artifact.read_bytes())
            raw[0] = (raw[0] + 1) % 256
            first_artifact.write_bytes(bytes(raw))
            with pytest.raises(BundleTamperError):
                verify_bundle(tampered_bundle / "artifacts_manifest.json", raise_on_error=True)

    # Post-run immutability checks: zero writes to docs/adr/ or data/frozen/ dev files.
    assert _snapshot_tree_hashes(REPO_DOCS_ADR) == docs_adr_before
    for path_checked, before_hash in dev_hashes_before.items():
        assert _file_sha256(path_checked) == before_hash, (
            f"Read-only dataset file was modified: {path_checked}"
        )


@pytest.mark.alloy_live
def test_real_dataset_alloy6_live_verified_non_vacuous() -> None:
    """Dedicated live Alloy 6 gate failing hard if real Alloy 6 doesn't verify VERIFIED + vacuity_passed=True."""
    runner = _resolve_java_and_alloy_runner()
    assert runner is not None and runner.is_available(), (
        "Real Java + Alloy 6 JAR (REQHQ_ALLOY_JAR_PATH) must be available for @pytest.mark.alloy_live!"
    )

    gamma_path = assert_dev_split_only(DEV_PURE_GAMMA_TEXT)
    assert_dev_split_only(DEV_PURE_GAMMA_GOLD)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_str:
        tmp = Path(tmp_str)
        router = _make_mock_router()
        source_doc = LocalFileLoader().load_file(gamma_path)
        snap = SnapshotStore(tmp / "sessions").save_document("alloy-live-gamma", source_doc)
        normalized = normalize_file(snap.snapshot_path, source_sha256=snap.sha256)
        chunk_res = HierarchicalChunker(max_chunk_chars=8000, overlap_chars=0).chunk(normalized)
        assert chunk_res.truncation_ratio == 0.0

        graph = RequirementDecomposer(router).decompose_chunks(
            chunk_res,
            session_id="alloy-live-gamma",
            source_uri=source_doc.source_uri,
        )
        plan = SocioTechnicalTriager(router).triage(graph, session_id="alloy-live-gamma")
        adrs = build_routing_adrs(graph, plan)

        spec_md = AlloySpecEmitter().emit_spec_md(
            graph,
            triage_plan=plan,
            adrs=adrs,
            compat=AlloyCompatMode.ALLOY6,
            max_scope=4,
        )
        controller = AlloyVerificationController(
            router,
            runner,
            max_local_repairs=2,
            max_scope=4,
            work_dir=tmp / "alloy_live_work",
        )
        outcome = controller.verify_spec(
            spec_md,
            graph,
            compat=AlloyCompatMode.ALLOY6,
        )
        assert outcome.status == AlloyRunStatus.VERIFIED, (
            f"Live Alloy 6 failed on real gamma_j dataset spec: {outcome.status} / "
            f"{[r.model_dump() for r in outcome.run_results]}"
        )
        assert outcome.vacuity_checked is True
        assert len(outcome.run_results) >= 1
        assert outcome.run_results[-1].vacuity_passed is True

        # Also verify --alloy-compat=legacy lowers cleanly and passes real Alloy 6.2 non-vacuously.
        spec_legacy = AlloySpecEmitter().emit_spec_md(
            graph,
            triage_plan=plan,
            adrs=adrs,
            compat=AlloyCompatMode.LEGACY,
            max_scope=4,
        )
        legacy_outcome = controller.verify_spec(
            spec_legacy,
            graph,
            compat=AlloyCompatMode.LEGACY,
        )
        assert legacy_outcome.status == AlloyRunStatus.VERIFIED, (
            f"Live Alloy 6 failed on legacy-compat real gamma_j spec: {legacy_outcome.status}"
        )
        assert legacy_outcome.vacuity_checked is True
        assert legacy_outcome.run_results[-1].vacuity_passed is True

        # Negative vacuity gate check: contradictory axioms MUST be rejected as VACUOUS_UNSAT.
        blocks = extract_alloy_blocks(outcome.spec_markdown)
        contradictory_core = (
            blocks["core_domain"].code
            + "\nfact SystemQaContradiction { some HumanActor and no HumanActor }\n"
        )
        vacuous_res = runner.run_check(
            contradictory_core,
            blocks["behavior"].code,
            work_dir=tmp / "alloy_vacuity_negative",
            max_scope=4,
            check_vacuity=True,
        )
        assert vacuous_res.status == AlloyRunStatus.VACUOUS_UNSAT, (
            f"Real Alloy 6 vacuity gate failed to catch contradictory facts: {vacuous_res.status}"
        )
        assert vacuous_res.vacuity_passed is False


@pytest.mark.live_system
def test_live_ollama_qwen3_4b_gamma_j_tier0_json_and_telemetry() -> None:
    """Opt-in live Ollama (`qwen3:4b`) test on `pure-0000 gamma_j` validating TIER_0 JSON & RAM/VRAM telemetry."""
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    try:
        resp = httpx.get(f"{base_url}/api/tags", timeout=2.0)
        resp.raise_for_status()
        models = [m.get("name", "") for m in resp.json().get("models", [])]
    except Exception as exc:
        pytest.skip(f"Local Ollama server not reachable at {base_url}: {exc}")

    if not any(name == "qwen3:4b" or name.startswith("qwen3:4b") for name in models):
        pytest.skip(f"Ollama model 'qwen3:4b' not pulled at {base_url} (found: {models})")

    gamma_path = assert_dev_split_only(DEV_PURE_GAMMA_TEXT)
    excerpt = gamma_path.read_text(encoding="utf-8", errors="replace")[:2500]

    provider = OllamaLocalProvider(
        base_url=base_url,
        default_model="qwen3:4b",
        timeout=90.0,
    )
    router = ProviderRouter(local_provider=provider, strict_local=True, enable_cache=False)

    collector = StageTelemetryCollector(run_mode=RunMode.LIVE, hardware_tier_override=0)
    collector.start_stage("live_gamma_j_tier0_json")

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "system_name": {"type": "string"},
            "requirements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "statement": {"type": "string"},
                    },
                    "required": ["id", "statement"],
                },
            },
        },
        "required": ["system_name", "requirements"],
    }
    llm_resp = router.route(
        LLMRequest(
            prompt=(
                "Extract 3 concise atomic requirements from this SRS excerpt as strict JSON "
                "matching the schema.\n\n"
                f"{excerpt}"
            ),
            system_prompt="Return ONLY valid JSON matching the requested schema.",
            model="qwen3:4b",
            temperature=0.0,
            max_tokens=512,
            json_schema=schema,
        )
    )
    telemetry = collector.finish_stage(
        "live_gamma_j_tier0_json",
        total_steps=1,
        local_only_steps=1,
        cloud_requests=0,
        all_cloud_baseline_requests=1,
    )

    parsed = json.loads(llm_resp.text)
    assert isinstance(parsed, dict)
    assert isinstance(parsed.get("system_name"), str) and parsed["system_name"].strip()
    assert isinstance(parsed.get("requirements"), list) and len(parsed["requirements"]) >= 1
    assert telemetry.run_mode == RunMode.LIVE
    assert telemetry.hardware_tier == 0
    assert telemetry.peak_ram_mb > 0.0
    assert get_process_peak_ram_mb() > 0.0
    assert telemetry.vram_gb >= 0.0
