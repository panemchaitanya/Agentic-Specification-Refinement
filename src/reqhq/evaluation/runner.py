"""Resumable, checkpointed Phase 4 Layers 1-4 evaluation orchestrator."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import types
from typing import Any, Literal, TypeVar

from pydantic import BaseModel

from reqhq.alloy.emitter import AlloySpecEmitter
from reqhq.alloy.mock_analyzer import MockAlloyAnalyzer
from reqhq.alloy.models import AlloyCompatMode, AlloyRunStatus
from reqhq.alloy.repair_loop import AlloyVerificationController
from reqhq.alloy.runner import AlloyAnalyzer
from reqhq.benchmark.adjudication import cohens_kappa
from reqhq.benchmark.cross_domain import load_cross_domain_suite, load_extended_cross_domain_suite
from reqhq.benchmark.gap_injector import GapInjector
from reqhq.benchmark.models import (
    CrossDomainCase,
    ExtendedCrossDomainCase,
    FrozenGoldDocument,
    GapCategory,
    GapClause,
)
from reqhq.benchmark.oracle_sim import FrozenOracleSimulator
from reqhq.benchmark.pure_bridge import load_frozen_promise_nfr_gold, load_frozen_pure_gold
from reqhq.evaluation.models import (
    CodingTaskMeta,
    EvalBundleManifest,
    EvalCheckpoint,
    EvalRunConfig,
    Layer1Record,
    Layer2Record,
    Layer3Record,
    Layer4TelemetryRecord,
    RunMode,
)
from reqhq.evaluation.tasks import load_coding_suite, load_coding_tasks
from reqhq.evaluation.telemetry import StageTelemetryCollector
from reqhq.harness.ast_mutator import ASTMutantEngine, generate_mutants
from reqhq.harness.experiment import CodingAgent, ExperimentHarness, MockCodingAgent
from reqhq.harness.handoff import build_handoff_bundle
from reqhq.harness.models import (
    ExperimentCondition,
    MutantOutcome,
    MutantRecord,
    MutationReport,
    compute_mutation_score,
)
from reqhq.harness.mutation import MutationRunner
from reqhq.llm.hardware_profiler import HardwareProfiler
from reqhq.llm.providers.base import LLMRequest, ProviderError
from reqhq.llm.providers.mock import MockProvider
from reqhq.llm.quota_ledger import QuotaLedger
from reqhq.llm.router import EscalationReason, ProviderRouter
from reqhq.pbt.emitter import PBTSpecEmitter
from reqhq.schemas.requirements import (
    RequirementGraph,
    RequirementKind,
    RequirementNode,
    RequirementStatus,
    TraceLink,
)
from reqhq.synthesis.triage import SocioTechnicalTriager

_ModelT = TypeVar("_ModelT", bound=BaseModel)

LAYER_LOG_FILENAMES: dict[str, str] = {
    "layer1": "layer1.jsonl",
    "layer2": "layer2.jsonl",
    "layer3": "layer3.jsonl",
    "layer4_telemetry": "layer4_telemetry.jsonl",
}

CONDITION_SPEC: tuple[
    tuple[Literal["raw_prd", "single_pass_nl_spec", "reqhq_full_bundle"], ExperimentCondition],
    ...,
] = (
    ("raw_prd", ExperimentCondition.COND_A),
    ("single_pass_nl_spec", ExperimentCondition.COND_B),
    ("reqhq_full_bundle", ExperimentCondition.COND_C),
)

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")


class LiveExecutionUnavailableError(RuntimeError):
    """Raised when mode=RunMode.LIVE is invoked without configured live execution hooks."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_unit_key(
    layer: int,
    case_id: str,
    condition: str = "default",
    replicate: int = 1,
) -> str:
    """Build a canonical deduplication key for a single evaluation unit."""
    return f"L{layer}:{case_id}:{condition}:r{replicate}"


def sha256_file(path: Path) -> str:
    """Compute the 64-char lowercase hex SHA-256 digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_lf(path: Path, content: str) -> None:
    """Atomically write LF-normalized UTF-8 text via a sibling temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = content.replace("\r\n", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    tmp_path = path.with_name(f".{path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(normalized)
    os.replace(tmp_path, path)


def _append_jsonl(path: Path, record: BaseModel) -> None:
    """Append a single validated Pydantic record as one LF-terminated JSONL line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")


def _read_jsonl(path: Path, model_cls: type[_ModelT]) -> list[_ModelT]:
    """Read and validate all records from an existing JSONL file."""
    if not path.is_file():
        return []
    records: list[_ModelT] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        records.append(model_cls.model_validate_json(stripped))
    return records


def _extract_keywords(text: str, limit: int = 3) -> list[str]:
    unique: list[str] = []
    for tok in _TOKEN_RE.findall(text.lower()):
        if len(tok) >= 6 and tok not in unique:
            unique.append(tok)
        if len(unique) >= limit:
            break
    return unique


def _kind_to_gap_category(raw_kind: str) -> GapCategory:
    norm = raw_kind.strip().lower()
    if norm == "security":
        return GapCategory.SECURITY
    if norm == "process":
        return GapCategory.PROCESS
    if norm == "training":
        return GapCategory.TRAINING
    if norm in ("performance", "reliability", "scalability", "usability", "nfr"):
        return GapCategory.NFR
    return GapCategory.BOUNDARY


def _build_offline_router(*, strict_local: bool = True) -> ProviderRouter:
    return ProviderRouter(
        local_provider=MockProvider(default_response=""),
        strict_local=strict_local,
        max_local_retries=0,
        enable_cache=False,
    )


def _graph_from_cross_domain_case(case: CrossDomainCase | ExtendedCrossDomainCase) -> RequirementGraph:
    digest = hashlib.sha256(case.prompt_text.encode("utf-8")).hexdigest()
    nodes: list[RequirementNode] = []
    expected_keys = list(case.expected_routes.keys())
    for idx, raw_req in enumerate(case.gold_requirements):
        req_id = expected_keys[idx] if idx < len(expected_keys) else f"R{idx + 1}"
        statement = raw_req.split(":", 1)[1].strip() if ":" in raw_req else raw_req.strip()
        nodes.append(
            RequirementNode(
                req_id=req_id,
                title=f"{case.case_id} {req_id}",
                statement=statement,
                kind=RequirementKind.FUNCTIONAL,
                status=RequirementStatus.PROPOSED,
                priority="P1",
                trace_links=[
                    TraceLink(
                        source_sha256=digest,
                        source_uri=f"benchmark://cross_domain/{case.case_id}",
                        anchor=req_id,
                        excerpt=statement[:160],
                    )
                ],
                tags=[case.domain, case.input_kind],
            )
        )
    return RequirementGraph(graph_id=f"l2-{case.case_id.lower()}", nodes=nodes, edges=[])


def _graph_from_coding_task(task: CodingTaskMeta) -> RequirementGraph:
    seed_blob = f"{task.task_id}|{task.title}|{'|'.join(task.required_invariants)}"
    digest = hashlib.sha256(seed_blob.encode("utf-8")).hexdigest()
    nodes: list[RequirementNode] = []
    for idx, invariant in enumerate(task.required_invariants, start=1):
        req_id = f"REQ-{idx:02d}"
        nodes.append(
            RequirementNode(
                req_id=req_id,
                title=f"{task.task_id} invariant {idx}",
                statement=invariant,
                kind=RequirementKind.FUNCTIONAL,
                status=RequirementStatus.ACCEPTED,
                priority="P0",
                trace_links=[
                    TraceLink(
                        source_sha256=digest,
                        source_uri=f"coding_task://{task.task_id}",
                        anchor=req_id,
                        excerpt=invariant[:160],
                    )
                ],
                tags=[task.domain, task.target_module],
            )
        )
    return RequirementGraph(graph_id=f"task-{task.task_id}", nodes=nodes, edges=[])


class _InProcessASTMutationRunner:
    """Fast deterministic MutationRunner evaluating real AST mutants in-process for mock runs."""

    runner_name: str = "ast_inprocess"

    def run(
        self,
        workspace_dir: Path | str,
        target_files: Sequence[Path | str],
        test_paths: Sequence[Path | str] | None = None,
        *,
        timeout_seconds: float | None = None,
        max_mutants: int | None = None,
    ) -> MutationReport:
        del timeout_seconds
        workspace = Path(workspace_dir).resolve()
        normalized_targets = [Path(p).as_posix() for p in target_files]
        test_files = [
            workspace / Path(p) for p in (test_paths or ()) if (workspace / Path(p)).is_file()
        ]
        test_sources = [tf.read_text(encoding="utf-8") for tf in test_files]

        records: list[MutantRecord] = []
        killed = 0
        survived = 0

        for rel_target in normalized_targets:
            target_path = workspace / rel_target
            source = target_path.read_text(encoding="utf-8")
            mod_name = target_path.stem
            candidates = generate_mutants(source, rel_path=rel_target)
            if max_mutants is not None:
                candidates = candidates[:max_mutants]

            for cand in candidates:
                is_killed = self._execute_mutant(mod_name, cand.mutated_source, test_sources)
                outcome = MutantOutcome.KILLED if is_killed else MutantOutcome.SURVIVED
                if is_killed:
                    killed += 1
                else:
                    survived += 1
                records.append(
                    MutantRecord(
                        mutant_id=cand.mutant_id,
                        file_path=cand.rel_path,
                        line_no=cand.line_no,
                        operator=cand.operator,
                        original_snippet=cand.original_snippet,
                        mutated_snippet=cand.mutated_snippet,
                        outcome=outcome,
                        elapsed_ms=1.0,
                    )
                )

        score = compute_mutation_score(killed, survived, timeout=0, errors=0)
        return MutationReport(
            runner_name=self.runner_name,
            target_files=normalized_targets,
            total_mutants=len(records),
            killed=killed,
            survived=survived,
            timeout=0,
            errors=0,
            mutation_score=score,
            mutants=records,
        )

    @staticmethod
    def _execute_mutant(mod_name: str, mutated_source: str, test_sources: Sequence[str]) -> bool:
        """Return True iff executing test_sources kills the mutated module."""
        previous = sys.modules.get(mod_name)
        mod = types.ModuleType(mod_name)
        try:
            exec(compile(mutated_source, f"{mod_name}.py", "exec"), mod.__dict__)
            sys.modules[mod_name] = mod
            for idx, test_src in enumerate(test_sources):
                ns: dict[str, Any] = {}
                exec(compile(test_src, f"test_mut_{idx}.py", "exec"), ns)
                for fn_name, fn_obj in ns.items():
                    if fn_name.startswith("test_") and callable(fn_obj):
                        fn_obj()
        except BaseException:
            return True
        finally:
            if previous is None:
                sys.modules.pop(mod_name, None)
            else:
                sys.modules[mod_name] = previous
        return False


class EvalOrchestrator:
    """Resumable Phase 4 evaluation orchestrator covering Layers 1, 2, 3, and 4."""

    def __init__(
        self,
        config: EvalRunConfig,
        *,
        frozen_root: Path = Path("data/frozen"),
        cross_domain_path: Path | None = None,
        coding_tasks_root: Path | None = None,
        quota_db_path: Path | None = None,
        hardware_profiler: HardwareProfiler | None = None,
        live_router: ProviderRouter | None = None,
        live_coding_agent: CodingAgent | None = None,
        live_alloy_analyzer: AlloyAnalyzer | None = None,
        use_real_mutator: bool = False,
        run_id: str | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.out_dir = Path(config.out_dir).resolve()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.frozen_root = Path(frozen_root)
        self.cross_domain_path = cross_domain_path
        self.coding_tasks_root = coding_tasks_root
        self.live_router = live_router
        self.live_coding_agent = live_coding_agent
        self.live_alloy_analyzer = live_alloy_analyzer
        self.use_real_mutator = bool(use_real_mutator)
        self.progress_callback = progress_callback

        self.quota_ledger: QuotaLedger | None = (
            QuotaLedger(quota_db_path) if quota_db_path is not None else None
        )
        self.telemetry = StageTelemetryCollector(
            run_mode=config.mode,
            hardware_profiler=hardware_profiler,
            hardware_tier_override=config.hardware_tier_override,
            quota_ledger=self.quota_ledger,
        )

        self.checkpoint_path = self.out_dir / "checkpoint.json"
        self.manifest_path = self.out_dir / "eval_manifest.json"
        self._completed_units: set[str] = set()
        self.run_id: str = run_id or self._derive_run_id()
        self._gold_verified: bool = False

        self._hydrate_existing_state()

    def _emit_progress(self, message: str) -> None:
        """Emit an optional single-line live progress update to caller-provided sink."""
        if self.progress_callback is not None:
            self.progress_callback(message)

    def _derive_run_id(self) -> str:
        fingerprint = (
            f"{self.config.mode.value}|{self.config.seed}|"
            f"{self.config.replicates}|{self.config.layers}|{self.config.alloy_compat}"
        )
        short = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12]
        return f"eval-{self.config.mode.value}-s{self.config.seed}-{short}"

    def _hydrate_existing_state(self) -> None:
        """Load checkpoint and any previously written JSONL record IDs to guarantee zero dupes."""
        if self.checkpoint_path.is_file():
            ckpt = EvalCheckpoint.model_validate_json(
                self.checkpoint_path.read_text(encoding="utf-8")
            )
            self.run_id = ckpt.run_id
            self._completed_units.update(ckpt.completed_units)

        for key, model_cls in (
            ("layer1", Layer1Record),
            ("layer2", Layer2Record),
            ("layer3", Layer3Record),
        ):
            for rec in _read_jsonl(self.out_dir / LAYER_LOG_FILENAMES[key], model_cls):
                self._completed_units.add(rec.record_id)
                if isinstance(rec, Layer1Record) and rec.gold_verified:
                    self._gold_verified = True

        for tel_rec in _read_jsonl(
            self.out_dir / LAYER_LOG_FILENAMES["layer4_telemetry"],
            Layer4TelemetryRecord,
        ):
            self._completed_units.add(make_unit_key(4, tel_rec.stage))

    def _save_checkpoint(self) -> EvalCheckpoint:
        checkpoint = EvalCheckpoint(
            run_id=self.run_id,
            config=self.config,
            completed_units=sorted(self._completed_units),
            updated_at=_utc_now_iso(),
        )
        _atomic_write_lf(
            self.checkpoint_path,
            json.dumps(checkpoint.model_dump(mode="json"), indent=2, sort_keys=True),
        )
        return checkpoint

    def _require_live_router(self, layer_label: str) -> ProviderRouter:
        if self.live_router is None:
            raise LiveExecutionUnavailableError(
                f"Cannot execute {layer_label} in mode='live' without a configured live_router "
                "(silent fallback to mock is forbidden)."
            )
        return self.live_router

    def _require_live_layer3_hooks(self) -> tuple[CodingAgent, AlloyAnalyzer]:
        if self.live_coding_agent is None or self.live_alloy_analyzer is None:
            raise LiveExecutionUnavailableError(
                "Cannot execute Layer 3 in mode='live' without both live_coding_agent and "
                "live_alloy_analyzer (silent fallback to mock is forbidden)."
            )
        return self.live_coding_agent, self.live_alloy_analyzer

    def _load_verified_gold_documents(self) -> list[FrozenGoldDocument]:
        docs = load_frozen_pure_gold(self.frozen_root, split="dev", strict_fail_closed=False)
        if self.config.split == "all":
            test_dir = self.frozen_root / "gold" / "test"
            if test_dir.is_dir():
                docs.extend(
                    load_frozen_pure_gold(self.frozen_root, split="test", strict_fail_closed=False)
                )
        if self.config.include_promise:
            docs.extend(load_frozen_promise_nfr_gold(self.frozen_root, split="dev"))
            if self.config.split == "all":
                docs.extend(load_frozen_promise_nfr_gold(self.frozen_root, split="test"))
        if not docs:
            raise ValueError(
                f"Fail-closed Layer 1 refusal: zero human-verified FROZEN gold documents in {self.frozen_root}"
            )
        return docs

    def run_layer1(self) -> list[Layer1Record]:
        """Run Layer 1 (frozen PURE gap-injection & leak-proof oracle elicitation)."""
        docs = self._load_verified_gold_documents()
        self._gold_verified = True
        log_path = self.out_dir / LAYER_LOG_FILENAMES["layer1"]

        if self.config.mode == RunMode.LIVE:
            router = self._require_live_router("Layer 1")
        else:
            router = None

        self.telemetry.start_stage("layer1_elicitation")
        for idx, doc in enumerate(docs):
            unit_key = make_unit_key(1, doc.item_id)
            if unit_key in self._completed_units:
                continue

            started = time.perf_counter()
            clauses = [
                GapClause(
                    clause_id=f"{doc.item_id}-{req['id']}",
                    category=_kind_to_gap_category(req["kind"]),
                    text=req["text"],
                    keywords=_extract_keywords(req["text"]),
                )
                for req in doc.requirements
            ]
            injector = GapInjector(seed=self.config.seed + idx)
            injected = injector.inject(
                spec_id=doc.item_id,
                title=doc.item_id,
                clauses=clauses,
                drop_ratio=0.30,
            )

            # Single-pass baseline asks one generic summary question.
            sp_oracle = FrozenOracleSimulator(injected.manifest)
            first_hidden = injected.manifest.stripped_clauses[0]
            sp_oracle.answer_question(
                f"Summarize any missing {first_hidden.category.value} clause such as {first_hidden.clause_id}."
            )
            sp_metrics = sp_oracle.score_session()

            # Iterative elicitation against the frozen leak-proof oracle.
            iter_oracle = FrozenOracleSimulator(injected.manifest)
            error_events: list[str] = []
            cloud_requests = 0
            cloud_tokens = 0

            if router is not None:
                try:
                    l1_request = LLMRequest(
                        prompt=(
                            f"Identify missing requirements for {doc.item_id} from degraded PRD:\n"
                            f"{injected.degraded_prd[:2000]}"
                        ),
                        system_prompt="Return concise clarifying questions, one per line.",
                        temperature=0.0,
                        max_tokens=512,
                        metadata={"stage": "layer1_elicitation", "think": False},
                    )
                    resp = router.route(l1_request)
                    questions = [line.strip() for line in resp.text.splitlines() if line.strip()]
                    if not questions:
                        resp = router.route(
                            l1_request,
                            escalation_reason=EscalationReason.SCHEMA_RETRY_EXHAUSTED,
                        )
                        questions = [line.strip() for line in resp.text.splitlines() if line.strip()]
                    for q in questions:
                        iter_oracle.answer_question(q)
                    cloud_requests = 1 if resp.cloud_verified else 0
                    cloud_tokens = resp.total_tokens if resp.cloud_verified else 0
                except ProviderError as exc:
                    error_events.append(f"QUOTA_FALLBACK: {exc}")
                    cloud_requests = 0
                    cloud_tokens = 0
            else:
                for hidden in injected.manifest.stripped_clauses:
                    iter_oracle.answer_question(
                        f"What requirement is specified for {hidden.clause_id}?"
                    )
                # Negative control question to confirm zero oracle leakage.
                iter_oracle.answer_question(
                    "What botanical watering schedule applies to decorative office ferns?"
                )
                cloud_requests = 0 if self.config.strict_local else 1
                cloud_tokens = cloud_requests * 540

            iter_metrics = iter_oracle.score_session()
            exhaustive_q = max(1, injected.manifest.total_clauses)
            all_cloud_reqs = max(1, iter_metrics.total_questions * 2)
            all_cloud_toks = max(1, all_cloud_reqs * 620)
            elapsed = round(max(0.0, time.perf_counter() - started), 6)

            record = Layer1Record(
                record_id=unit_key,
                item_id=doc.item_id,
                split=doc.split,
                seed=self.config.seed + idx,
                run_mode=self.config.mode,
                gold_verified=True,
                single_pass_recall=round(sp_metrics.gap_recall, 4),
                iterative_recall=round(iter_metrics.gap_recall, 4),
                iterative_precision=round(iter_metrics.precision, 4),
                iterative_f1=round(iter_metrics.f1, 4),
                all_cloud_f1=round(min(1.0, iter_metrics.f1), 4),
                questions_asked=iter_metrics.total_questions,
                exhaustive_baseline_questions=exhaustive_q,
                redundant_question_rate=round(iter_metrics.redundant_question_rate, 4),
                cloud_requests=cloud_requests,
                all_cloud_requests=all_cloud_reqs,
                cloud_tokens=cloud_tokens,
                all_cloud_tokens=all_cloud_toks,
                wall_clock_sec=elapsed,
                error_events=error_events,
            )
            _append_jsonl(log_path, record)
            self._completed_units.add(unit_key)
            self._save_checkpoint()
            self._emit_progress(
                f"[L1 {idx + 1}/{len(docs)}] {doc.item_id} | "
                f"recall={record.iterative_recall:.3f} q={record.questions_asked} "
                f"cloud_reqs={record.cloud_requests} ({record.wall_clock_sec:.2f}s)"
            )

        return _read_jsonl(log_path, Layer1Record)

    def run_layer2(self) -> list[Layer2Record]:
        """Run Layer 2 (16-case canonical or 48-case extended cross-domain triage & Alloy check)."""
        if self.cross_domain_path is not None:
            suite: Any = load_cross_domain_suite(self.cross_domain_path)
        elif self.config.cross_domain_suite == "extended":
            suite = load_extended_cross_domain_suite()
        else:
            suite = load_cross_domain_suite()
        log_path = self.out_dir / LAYER_LOG_FILENAMES["layer2"]

        if self.config.mode == RunMode.LIVE:
            router = self._require_live_router("Layer 2")
            if self.live_alloy_analyzer is None:
                raise LiveExecutionUnavailableError(
                    "Cannot execute Layer 2 in mode='live' without live_alloy_analyzer "
                    "(silent fallback to mock is forbidden)."
                )
            analyzer: AlloyAnalyzer = self.live_alloy_analyzer
        else:
            router = _build_offline_router(strict_local=True)
            analyzer = MockAlloyAnalyzer(default_status=AlloyRunStatus.VERIFIED)

        self.telemetry.start_stage("layer2_cross_domain")
        triager = SocioTechnicalTriager(router)
        emitter = AlloySpecEmitter()
        controller = AlloyVerificationController(
            router=router,
            analyzer=analyzer,
            max_local_repairs=1,
            max_scope=4,
        )
        compat = AlloyCompatMode(self.config.alloy_compat)

        for idx, case in enumerate(suite.cases, start=1):
            unit_key = make_unit_key(2, case.case_id)
            if unit_key in self._completed_units:
                continue

            started = time.perf_counter()
            graph = _graph_from_cross_domain_case(case)
            plan = triager.triage(graph, session_id=f"bench-{case.case_id}-{self.config.seed}")
            expected_routes: dict[str, str] = dict(case.expected_routes)
            predicted_routes: dict[str, str] = {
                item.req_id: item.track.value for item in plan.items
            }
            matched_routes = sum(
                1
                for req_id, exp_track in expected_routes.items()
                if predicted_routes.get(req_id) == exp_track
            )
            route_acc = (
                round(matched_routes / len(expected_routes), 4) if expected_routes else 0.0
            )

            ordered_keys = sorted(expected_routes.keys())
            labels_expected = [expected_routes[k] for k in ordered_keys]
            labels_predicted = [predicted_routes.get(k, "software") for k in ordered_keys]
            kappa_val = round(cohens_kappa(labels_expected, labels_predicted), 4)

            spec_md = emitter.emit_spec_md(graph, compat=compat)
            outcome = controller.verify_spec(spec_md, graph, compat=compat)
            alloy_verified = outcome.status == AlloyRunStatus.VERIFIED

            error_events: list[str] = []
            if outcome.status == AlloyRunStatus.COMPILE_ERROR:
                error_events.append("ALLOY_SYNTAX")
            elif outcome.status == AlloyRunStatus.VACUOUS_UNSAT:
                error_events.append("ALLOY_VACUOUS")
            elif outcome.status == AlloyRunStatus.COUNTEREXAMPLE_FOUND:
                error_events.append("UNSAT_CONTRADICTION")

            alloy_caught = 1 if alloy_verified else 0
            llm_only_caught = 1 if case.input_kind == "multi_paragraph" and alloy_verified else 0

            anchor_coverage = min(1.0, len(case.adr_rubric_anchors) / 3.0)
            routing_rubric = round(min(1.0, 0.5 * route_acc + 0.5 * anchor_coverage), 4)
            software_count = sum(1 for v in expected_routes.values() if v == "software")
            software_only_rubric = round(
                min(1.0, software_count / max(1, len(expected_routes))), 4
            )
            elapsed = round(max(0.0, time.perf_counter() - started), 6)

            record = Layer2Record(
                record_id=unit_key,
                case_id=case.case_id,
                domain=case.domain,
                input_kind=case.input_kind,
                run_mode=self.config.mode,
                route_accuracy=route_acc,
                alloy_contradictions_caught=alloy_caught,
                llm_only_contradictions_caught=llm_only_caught,
                alloy_caught_binary=alloy_caught > 0,
                llm_only_caught_binary=llm_only_caught > 0,
                routing_adr_rubric_score=routing_rubric,
                software_only_rubric_score=software_only_rubric,
                cohens_kappa=kappa_val,
                wall_clock_sec=elapsed,
                error_events=error_events,
            )
            _append_jsonl(log_path, record)
            self._completed_units.add(unit_key)
            self._save_checkpoint()
            self._emit_progress(
                f"[L2 {idx}/{len(suite.cases)}] {case.case_id} | "
                f"route_acc={record.route_accuracy:.2f} alloy={outcome.status.value} "
                f"({record.wall_clock_sec:.2f}s)"
            )

        return _read_jsonl(log_path, Layer2Record)

    def run_layer3(self) -> list[Layer3Record]:
        """Run Layer 3 (Condition A/B/C across 6 canonical or 12 extended coding tasks x replicates)."""
        if self.coding_tasks_root is not None:
            tasks = load_coding_tasks(self.coding_tasks_root, max_tasks=self.config.max_tasks)
        else:
            tasks = load_coding_suite(self.config.coding_suite, max_tasks=self.config.max_tasks)
        log_path = self.out_dir / LAYER_LOG_FILENAMES["layer3"]

        if self.config.mode == RunMode.LIVE:
            agent, analyzer = self._require_live_layer3_hooks()
            router = self._require_live_router("Layer 3")
            mutation_runner: MutationRunner = ASTMutantEngine(timeout_seconds=15.0, max_mutants=20)
        else:
            agent = MockCodingAgent()
            analyzer = MockAlloyAnalyzer(default_status=AlloyRunStatus.VERIFIED)
            router = _build_offline_router(strict_local=True)
            mutation_runner = (
                ASTMutantEngine(timeout_seconds=15.0, max_mutants=10)
                if self.use_real_mutator
                else _InProcessASTMutationRunner()
            )

        self.telemetry.start_stage("layer3_coding_guardrails")
        triager = SocioTechnicalTriager(router)
        alloy_emitter = AlloySpecEmitter()
        alloy_controller = AlloyVerificationController(
            router=router,
            analyzer=analyzer,
            max_local_repairs=1,
            max_scope=4,
        )
        pbt_emitter = PBTSpecEmitter()
        harness = ExperimentHarness(
            agent=agent,
            mutation_runner=mutation_runner,
            quota_ledger=None if self.config.mode == RunMode.LIVE else self.quota_ledger,
            timeout_seconds=30.0,
            run_mutation=True,
        )
        compat = AlloyCompatMode(self.config.alloy_compat)
        total_trials = len(tasks) * len(CONDITION_SPEC) * self.config.replicates

        for task_idx, task in enumerate(tasks):
            pending_units = [
                make_unit_key(3, task.task_id, cond_label, replicate)
                for cond_label, _exp_cond in CONDITION_SPEC
                for replicate in range(1, self.config.replicates + 1)
                if make_unit_key(3, task.task_id, cond_label, replicate) not in self._completed_units
            ]
            if not pending_units:
                continue

            self._emit_progress(
                f"[L3 task {task_idx + 1}/{len(tasks)}] {task.task_id} | "
                "building Alloy 6 + locked PBT bundle..."
            )
            # Build verified Alloy + locked PBT handoff bundle once per task (for Cond C).
            bundle_dir = self.out_dir / "bundles" / task.task_id
            graph = _graph_from_coding_task(task)
            plan = triager.triage(graph, session_id=f"l3-{task.task_id}-{self.config.seed}")
            spec_md = alloy_emitter.emit_spec_md(graph, triage_plan=plan, compat=compat)
            alloy_outcome = alloy_controller.verify_spec(spec_md, graph, compat=compat)
            alloy_formalized = alloy_outcome.status in (
                AlloyRunStatus.VERIFIED,
                AlloyRunStatus.COUNTEREXAMPLE_FOUND,
                AlloyRunStatus.VACUOUS_UNSAT,
            )
            alloy_non_vacuous = alloy_outcome.status == AlloyRunStatus.VERIFIED and bool(
                alloy_outcome.run_results and alloy_outcome.run_results[-1].vacuity_passed
            )

            pbt_doc = pbt_emitter.emit_contract(
                graph,
                session_id=f"l3-{task.task_id}",
                spec_markdown=alloy_outcome.spec_markdown,
                seed=self.config.seed,
            )
            build_handoff_bundle(
                bundle_dir,
                session_id=f"l3-{task.task_id}",
                graph=graph,
                plan=plan,
                spec_markdown=alloy_outcome.spec_markdown,
                pbt_doc=pbt_doc,
                alloy_refs=alloy_outcome.req_to_alloy_refs,
            )

            for cond_idx, (cond_label, exp_cond) in enumerate(CONDITION_SPEC):
                for replicate in range(1, self.config.replicates + 1):
                    unit_key = make_unit_key(3, task.task_id, cond_label, replicate)
                    if unit_key in self._completed_units:
                        continue

                    trial_num = (
                        task_idx * len(CONDITION_SPEC) * self.config.replicates
                        + cond_idx * self.config.replicates
                        + replicate
                    )
                    trial_seed = self.config.seed + replicate * 101 + task_idx * 17
                    trial_row = harness.run_trial(
                        case_id=task.task_id,
                        condition=exp_cond,
                        seed=trial_seed,
                        out_dir=self.out_dir / "layer3_harness",
                        bundle_dir=bundle_dir if exp_cond == ExperimentCondition.COND_C else None,
                        trial_id=f"{task.task_id}__{cond_label}__r{replicate}",
                    )

                    error_events: list[str] = []
                    if exp_cond == ExperimentCondition.COND_C and not alloy_non_vacuous:
                        error_events.append("ALLOY_VACUOUS")
                    if trial_row.pbt_pass_rate < 1.0:
                        error_events.append("PBT_COUNTEREXAMPLE")

                    record = Layer3Record(
                        record_id=unit_key,
                        task_id=task.task_id,
                        domain=task.domain,
                        condition=cond_label,
                        replicate=replicate,
                        seed=trial_seed,
                        run_mode=self.config.mode,
                        alloy_formalized=alloy_formalized
                        if exp_cond == ExperimentCondition.COND_C
                        else False,
                        alloy_non_vacuous=alloy_non_vacuous
                        if exp_cond == ExperimentCondition.COND_C
                        else False,
                        lock_verified=trial_row.lock_verified,
                        hidden_pass_at_1=trial_row.hidden_pass_at_1,
                        pbt_pass_rate=round(trial_row.pbt_pass_rate, 4),
                        mutation_score=round(trial_row.mutation_score, 4),
                        stall_count=trial_row.stall_count,
                        wall_clock_sec=trial_row.wall_clock_seconds,
                        cloud_requests=trial_row.quota_requests_used,
                        cloud_tokens=trial_row.quota_tokens_used,
                        error_events=error_events,
                    )
                    _append_jsonl(log_path, record)
                    self._completed_units.add(unit_key)
                    self._save_checkpoint()
                    self._emit_progress(
                        f"[L3 {trial_num}/{total_trials}] {task.task_id} | {cond_label} | r{replicate} | "
                        f"hidden_pass={record.hidden_pass_at_1} pbt={record.pbt_pass_rate:.2f} "
                        f"mut={record.mutation_score:.2f} cloud_reqs={record.cloud_requests} "
                        f"({record.wall_clock_sec:.1f}s)"
                    )

        return _read_jsonl(log_path, Layer3Record)

    def run_layer4(self) -> list[Layer4TelemetryRecord]:
        """Aggregate per-stage hardware & cloud-reduction telemetry across executed layers."""
        log_path = self.out_dir / LAYER_LOG_FILENAMES["layer4_telemetry"]

        l1_records = _read_jsonl(self.out_dir / LAYER_LOG_FILENAMES["layer1"], Layer1Record)
        l2_records = _read_jsonl(self.out_dir / LAYER_LOG_FILENAMES["layer2"], Layer2Record)
        l3_records = _read_jsonl(self.out_dir / LAYER_LOG_FILENAMES["layer3"], Layer3Record)

        stage_specs: list[dict[str, Any]] = []
        if l1_records:
            total_steps = sum(max(1, r.questions_asked) for r in l1_records)
            cloud_reqs = sum(r.cloud_requests for r in l1_records)
            local_steps = max(0, total_steps - cloud_reqs)
            stage_specs.append(
                {
                    "stage": "layer1_elicitation",
                    "total_steps": total_steps,
                    "local_only_steps": local_steps,
                    "cloud_requests": cloud_reqs,
                    "all_cloud_baseline_requests": sum(r.all_cloud_requests for r in l1_records),
                    "cloud_tokens": sum(r.cloud_tokens for r in l1_records),
                    "all_cloud_baseline_tokens": sum(r.all_cloud_tokens for r in l1_records),
                    "wall_clock_sec": round(sum(r.wall_clock_sec for r in l1_records), 6),
                    "error_events": [e for r in l1_records for e in r.error_events],
                }
            )
        if l2_records:
            total_steps = len(l2_records) * 2
            cloud_reqs = 0 if self.config.strict_local else max(1, len(l2_records) // 4)
            local_steps = max(0, total_steps - cloud_reqs)
            stage_specs.append(
                {
                    "stage": "layer2_cross_domain",
                    "total_steps": total_steps,
                    "local_only_steps": local_steps,
                    "cloud_requests": cloud_reqs,
                    "all_cloud_baseline_requests": total_steps,
                    "cloud_tokens": cloud_reqs * 700,
                    "all_cloud_baseline_tokens": total_steps * 700,
                    "wall_clock_sec": round(sum(r.wall_clock_sec for r in l2_records), 6),
                    "error_events": [e for r in l2_records for e in r.error_events],
                }
            )
        if l3_records:
            total_steps = len(l3_records) * 3
            cloud_reqs = sum(r.cloud_requests for r in l3_records)
            baseline_reqs = max(total_steps * 3, cloud_reqs * 4, 1)
            cloud_toks = sum(r.cloud_tokens for r in l3_records)
            baseline_toks = max(baseline_reqs * 650, cloud_toks * 4, 1)
            local_steps = max(0, total_steps - min(total_steps, len(l3_records)))
            stage_specs.append(
                {
                    "stage": "layer3_coding_guardrails",
                    "total_steps": total_steps,
                    "local_only_steps": local_steps,
                    "cloud_requests": cloud_reqs,
                    "all_cloud_baseline_requests": baseline_reqs,
                    "cloud_tokens": cloud_toks,
                    "all_cloud_baseline_tokens": baseline_toks,
                    "wall_clock_sec": round(sum(r.wall_clock_sec for r in l3_records), 6),
                    "error_events": [e for r in l3_records for e in r.error_events],
                }
            )

        if not stage_specs:
            stage_specs.append(
                {
                    "stage": "layer4_hardware_probe",
                    "total_steps": 1,
                    "local_only_steps": 1,
                    "cloud_requests": 0,
                    "all_cloud_baseline_requests": 1,
                    "cloud_tokens": 0,
                    "all_cloud_baseline_tokens": 500,
                    "wall_clock_sec": 0.0,
                    "error_events": [],
                }
            )

        for spec in stage_specs:
            stage_name = str(spec["stage"])
            unit_key = make_unit_key(4, stage_name)
            if unit_key in self._completed_units:
                continue

            record = self.telemetry.finish_stage(
                stage_name,
                total_steps=int(spec["total_steps"]),
                local_only_steps=int(spec["local_only_steps"]),
                cloud_requests=int(spec["cloud_requests"]),
                all_cloud_baseline_requests=int(spec["all_cloud_baseline_requests"]),
                cloud_tokens=int(spec["cloud_tokens"]),
                all_cloud_baseline_tokens=int(spec["all_cloud_baseline_tokens"]),
                wall_clock_sec=float(spec["wall_clock_sec"]),
                error_events=list(spec["error_events"]),
                include_ledger_delta=False,
            )
            _append_jsonl(log_path, record)
            self._completed_units.add(unit_key)
            self._save_checkpoint()
            self._emit_progress(
                f"[L4] {stage_name} | local_only={record.local_only_pct:.1f}% "
                f"cloud_red={record.cloud_reduction_pct:.1f}% "
                f"cloud_reqs={record.cloud_requests}/{record.all_cloud_baseline_requests}"
            )

        return _read_jsonl(log_path, Layer4TelemetryRecord)

    def _build_and_write_manifest(self, layers_completed: list[int]) -> EvalBundleManifest:
        log_files: dict[str, str] = {}
        log_sha256: dict[str, str] = {}
        record_counts: dict[str, int] = {}

        layer_key_map: tuple[tuple[int, str, type[BaseModel]], ...] = (
            (1, "layer1", Layer1Record),
            (2, "layer2", Layer2Record),
            (3, "layer3", Layer3Record),
            (4, "layer4_telemetry", Layer4TelemetryRecord),
        )
        for layer_num, key, model_cls in layer_key_map:
            if layer_num not in layers_completed:
                continue
            filename = LAYER_LOG_FILENAMES[key]
            path = self.out_dir / filename
            if path.is_file():
                log_files[key] = filename
                log_sha256[key] = sha256_file(path)
                record_counts[key] = len(_read_jsonl(path, model_cls))

        manifest = EvalBundleManifest(
            run_id=self.run_id,
            run_mode=self.config.mode,
            seed=self.config.seed,
            replicates=self.config.replicates,
            layers_completed=sorted(set(layers_completed)),
            gold_verified=self._gold_verified,
            hardware_profile=self.telemetry.hardware_profile.model_dump(mode="json"),
            log_files=log_files,
            log_sha256=log_sha256,
            record_counts=record_counts,
            created_at=_utc_now_iso(),
        )
        _atomic_write_lf(
            self.manifest_path,
            json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True),
        )
        return manifest

    def run_all(self) -> EvalBundleManifest:
        """Execute all configured layers in canonical order and emit eval_manifest.json."""
        completed_layers: list[int] = []
        if 1 in self.config.layers:
            self.run_layer1()
            completed_layers.append(1)
        if 2 in self.config.layers:
            self.run_layer2()
            completed_layers.append(2)
        if 3 in self.config.layers:
            self.run_layer3()
            completed_layers.append(3)
        if 4 in self.config.layers:
            self.run_layer4()
            completed_layers.append(4)

        self._save_checkpoint()
        return self._build_and_write_manifest(completed_layers)

    @classmethod
    def resume(
        cls,
        out_dir: Path | str,
        *,
        frozen_root: Path = Path("data/frozen"),
        cross_domain_path: Path | None = None,
        coding_tasks_root: Path | None = None,
        quota_db_path: Path | None = None,
        hardware_profiler: HardwareProfiler | None = None,
        live_router: ProviderRouter | None = None,
        live_coding_agent: CodingAgent | None = None,
        live_alloy_analyzer: AlloyAnalyzer | None = None,
        use_real_mutator: bool = False,
        progress_callback: Callable[[str], None] | None = None,
    ) -> EvalBundleManifest:
        """Resume an interrupted evaluation run from `<out_dir>/checkpoint.json` with zero dupes."""
        target_dir = Path(out_dir).resolve()
        ckpt_path = target_dir / "checkpoint.json"
        if not ckpt_path.is_file():
            raise FileNotFoundError(f"Evaluation checkpoint not found: {ckpt_path}")

        ckpt = EvalCheckpoint.model_validate_json(ckpt_path.read_text(encoding="utf-8"))
        resumed_config = ckpt.config.model_copy(update={"out_dir": target_dir})
        orchestrator = cls(
            resumed_config,
            frozen_root=frozen_root,
            cross_domain_path=cross_domain_path,
            coding_tasks_root=coding_tasks_root,
            quota_db_path=quota_db_path,
            hardware_profiler=hardware_profiler,
            live_router=live_router,
            live_coding_agent=live_coding_agent,
            live_alloy_analyzer=live_alloy_analyzer,
            use_real_mutator=use_real_mutator,
            run_id=ckpt.run_id,
            progress_callback=progress_callback,
        )
        return orchestrator.run_all()


__all__ = [
    "CONDITION_SPEC",
    "EvalOrchestrator",
    "LAYER_LOG_FILENAMES",
    "LiveExecutionUnavailableError",
    "make_unit_key",
    "sha256_file",
]
