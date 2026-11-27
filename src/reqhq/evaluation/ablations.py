"""Proposal B3 (zero-human-input) and leave-one-critic-out ablation runner."""

from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from reqhq.benchmark.gap_injector import GapInjector
from reqhq.benchmark.models import FrozenGoldDocument, GapClause
from reqhq.benchmark.oracle_sim import FrozenOracleSimulator
from reqhq.benchmark.pure_bridge import load_frozen_promise_nfr_gold, load_frozen_pure_gold
from reqhq.elicitation.critics import (
    BaseCritic,
    CriticPanel,
    FunctionalCompletenessCritic,
    LogicalConsistencyCritic,
    NonFunctionalQualityCritic,
    ProcessOperationalCritic,
    TrainingAdoptionCritic,
)
from reqhq.elicitation.models import FindingSeverity
from reqhq.evaluation.models import RunMode
from reqhq.evaluation.runner import (
    LiveExecutionUnavailableError,
    _build_offline_router,
    _extract_keywords,
    _kind_to_gap_category,
)
from reqhq.llm.router import ProviderRouter
from reqhq.schemas.requirements import (
    RequirementGraph,
    RequirementKind,
    RequirementNode,
    RequirementStatus,
    TraceLink,
)

AblationArmName = Literal[
    "full_5_critics",
    "b3_no_human",
    "minus_functional",
    "minus_nfr",
    "minus_logical",
    "minus_process",
    "minus_training",
]

ABLATION_ARMS: tuple[AblationArmName, ...] = (
    "full_5_critics",
    "b3_no_human",
    "minus_functional",
    "minus_nfr",
    "minus_logical",
    "minus_process",
    "minus_training",
)

_ALL_CRITIC_CLASSES: tuple[tuple[str, type[BaseCritic]], ...] = (
    ("functional", FunctionalCompletenessCritic),
    ("nfr", NonFunctionalQualityCritic),
    ("logical", LogicalConsistencyCritic),
    ("process", ProcessOperationalCritic),
    ("training", TrainingAdoptionCritic),
)


def _atomic_write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = text.replace("\r\n", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(normalized)
    os.replace(tmp_path, path)


def _append_jsonl(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)


def build_critics_for_arm(arm: AblationArmName) -> list[BaseCritic]:
    """Instantiate the exact specialist critic subset for a given ablation arm."""
    excluded = arm.removeprefix("minus_") if arm.startswith("minus_") else ""
    return [cls() for key, cls in _ALL_CRITIC_CLASSES if key != excluded]


def _graph_from_retained_clauses(doc: FrozenGoldDocument, clauses: list[GapClause]) -> RequirementGraph:
    kind_map = {
        "security": RequirementKind.SECURITY,
        "process": RequirementKind.PROCESS,
        "training": RequirementKind.PROCESS,
        "nfr": RequirementKind.NON_FUNCTIONAL,
        "boundary": RequirementKind.FUNCTIONAL,
    }
    nodes: list[RequirementNode] = []
    for idx, clause in enumerate(clauses, start=1):
        nodes.append(
            RequirementNode(
                req_id=f"REQ-{idx:03d}",
                title=clause.text[:96],
                statement=clause.text,
                kind=kind_map.get(clause.category.value, RequirementKind.FUNCTIONAL),
                priority="P0" if idx <= 3 else "P1",
                status=RequirementStatus.DRAFT,
                acceptance_criteria=[],
                trace_links=[
                    TraceLink(
                        source_sha256=doc.source_sha256,
                        source_uri=doc.prd_path,
                        anchor=f"clause:{clause.clause_id}",
                        excerpt=clause.text[:160],
                    )
                ],
                tags=[doc.item_id, clause.category.value],
            )
        )
    return RequirementGraph(graph_id=f"ablate-{doc.item_id}", nodes=nodes, edges=[])


class AblationItemRecord(BaseModel):
    """Single (arm, document) ablation result record."""

    model_config = ConfigDict(extra="forbid")

    record_id: str = Field(..., min_length=1)
    arm: AblationArmName
    item_id: str = Field(..., min_length=1)
    split: str = Field(..., min_length=1)
    run_mode: RunMode
    active_critics: int = Field(..., ge=1, le=5)
    findings_count: int = Field(..., ge=0)
    blocker_major_count: int = Field(..., ge=0)
    questions_asked: int = Field(..., ge=0)
    gap_recall: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    precision: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    f1: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    wall_clock_sec: float = Field(..., ge=0.0, allow_inf_nan=False)


class AblationArmSummary(BaseModel):
    """Aggregate metrics for one ablation arm vs. full_5_critics."""

    model_config = ConfigDict(extra="forbid")

    arm: AblationArmName
    n_items: int = Field(..., ge=0)
    mean_findings: float = Field(..., ge=0.0, allow_inf_nan=False)
    mean_blocker_major: float = Field(..., ge=0.0, allow_inf_nan=False)
    mean_questions_asked: float = Field(..., ge=0.0, allow_inf_nan=False)
    mean_gap_recall: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    mean_f1: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    delta_recall_vs_full: float = Field(..., allow_inf_nan=False)
    delta_findings_vs_full: float = Field(..., allow_inf_nan=False)
    total_wall_clock_sec: float = Field(..., ge=0.0, allow_inf_nan=False)


class AblationReport(BaseModel):
    """Complete Proposal B3 & leave-one-critic-out ablation report."""

    model_config = ConfigDict(extra="forbid")

    run_mode: RunMode
    split: Literal["dev", "all"]
    include_promise: bool
    seed: int
    total_records: int = Field(..., ge=0)
    arms: list[AblationArmSummary]


def summarize_ablation_records(
    records: list[AblationItemRecord],
    *,
    run_mode: RunMode,
    split: Literal["dev", "all"],
    include_promise: bool,
    seed: int,
) -> AblationReport:
    """Aggregate per-arm ablation statistics and deltas relative to full_5_critics."""
    by_arm: dict[AblationArmName, list[AblationItemRecord]] = {arm: [] for arm in ABLATION_ARMS}
    for rec in records:
        by_arm[rec.arm].append(rec)

    full_recs = by_arm["full_5_critics"]
    full_recall = (
        sum(r.gap_recall for r in full_recs) / len(full_recs) if full_recs else 0.0
    )
    full_findings = (
        sum(r.findings_count for r in full_recs) / len(full_recs) if full_recs else 0.0
    )

    summaries: list[AblationArmSummary] = []
    for arm in ABLATION_ARMS:
        arm_recs = by_arm[arm]
        n = len(arm_recs)
        if n == 0:
            summaries.append(
                AblationArmSummary(
                    arm=arm,
                    n_items=0,
                    mean_findings=0.0,
                    mean_blocker_major=0.0,
                    mean_questions_asked=0.0,
                    mean_gap_recall=0.0,
                    mean_f1=0.0,
                    delta_recall_vs_full=0.0,
                    delta_findings_vs_full=0.0,
                    total_wall_clock_sec=0.0,
                )
            )
            continue
        mean_f = sum(r.findings_count for r in arm_recs) / n
        mean_bm = sum(r.blocker_major_count for r in arm_recs) / n
        mean_q = sum(r.questions_asked for r in arm_recs) / n
        mean_rec = sum(r.gap_recall for r in arm_recs) / n
        mean_f1 = sum(r.f1 for r in arm_recs) / n
        summaries.append(
            AblationArmSummary(
                arm=arm,
                n_items=n,
                mean_findings=round(mean_f, 4),
                mean_blocker_major=round(mean_bm, 4),
                mean_questions_asked=round(mean_q, 4),
                mean_gap_recall=round(mean_rec, 4),
                mean_f1=round(mean_f1, 4),
                delta_recall_vs_full=round(mean_rec - full_recall, 4),
                delta_findings_vs_full=round(mean_f - full_findings, 4),
                total_wall_clock_sec=round(sum(r.wall_clock_sec for r in arm_recs), 4),
            )
        )

    return AblationReport(
        run_mode=run_mode,
        split=split,
        include_promise=include_promise,
        seed=seed,
        total_records=len(records),
        arms=summaries,
    )


class AblationRunner:
    """Resumable Proposal B3 (no-human) & leave-one-critic-out ablation study runner."""

    def __init__(
        self,
        out_dir: Path | str,
        *,
        mode: RunMode = RunMode.MOCK,
        split: Literal["dev", "all"] = "dev",
        include_promise: bool = True,
        frozen_root: Path | str = Path("data/frozen"),
        seed: int = 42,
        live_router: ProviderRouter | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.out_dir = Path(out_dir).resolve()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.split = split
        self.include_promise = bool(include_promise)
        self.frozen_root = Path(frozen_root).resolve()
        self.seed = int(seed)
        self.live_router = live_router
        self.progress_callback = progress_callback
        self.log_path = self.out_dir / "ablation_records.jsonl"
        self.summary_path = self.out_dir / "ablation_summary.json"

    def _load_gold_docs(self) -> list[FrozenGoldDocument]:
        docs = load_frozen_pure_gold(self.frozen_root, split="dev", strict_fail_closed=False)
        if self.split == "all":
            test_dir = self.frozen_root / "gold" / "test"
            if test_dir.is_dir():
                docs.extend(
                    load_frozen_pure_gold(self.frozen_root, split="test", strict_fail_closed=False)
                )
        if self.include_promise:
            docs.extend(load_frozen_promise_nfr_gold(self.frozen_root, split="dev"))
            if self.split == "all":
                docs.extend(load_frozen_promise_nfr_gold(self.frozen_root, split="test"))
        if not docs:
            raise ValueError(f"No verified gold documents available in {self.frozen_root}")
        return docs

    def _load_existing(self) -> list[AblationItemRecord]:
        if not self.log_path.is_file():
            return []
        records: list[AblationItemRecord] = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped:
                records.append(AblationItemRecord.model_validate_json(stripped))
        return records

    def run(self) -> AblationReport:
        """Execute all 7 ablation arms across verified gold projects with atomic checkpointing."""
        if self.mode == RunMode.LIVE:
            if self.live_router is None:
                raise LiveExecutionUnavailableError(
                    "Cannot run ablation study in mode='live' without live_router."
                )
            router = self.live_router
        else:
            router = _build_offline_router(strict_local=True)

        docs = self._load_gold_docs()
        existing = self._load_existing()
        completed_ids = {rec.record_id for rec in existing}
        total_units = len(ABLATION_ARMS) * len(docs)
        unit_idx = 0

        for arm in ABLATION_ARMS:
            critics = build_critics_for_arm(arm)
            panel = CriticPanel(router, critics=critics)
            for doc_idx, doc in enumerate(docs):
                unit_idx += 1
                record_id = f"ABL:{arm}:{doc.item_id}"
                if record_id in completed_ids:
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
                injected = GapInjector(seed=self.seed + doc_idx).inject(
                    spec_id=doc.item_id,
                    title=doc.item_id,
                    clauses=clauses,
                    drop_ratio=0.30,
                )
                graph = _graph_from_retained_clauses(doc, injected.manifest.retained_clauses)
                findings, _disagreements = panel.run_on_nodes(graph)
                blocker_major = sum(
                    1
                    for f in findings
                    if f.severity in (FindingSeverity.BLOCKER, FindingSeverity.MAJOR)
                )

                oracle = FrozenOracleSimulator(injected.manifest)
                if arm != "b3_no_human":
                    for finding in findings:
                        oracle.answer_question(finding.suggested_question)
                metrics = oracle.score_session()
                elapsed = round(max(0.0, time.perf_counter() - started), 4)

                rec = AblationItemRecord(
                    record_id=record_id,
                    arm=arm,
                    item_id=doc.item_id,
                    split=doc.split,
                    run_mode=self.mode,
                    active_critics=len(critics),
                    findings_count=len(findings),
                    blocker_major_count=blocker_major,
                    questions_asked=metrics.total_questions,
                    gap_recall=round(metrics.gap_recall, 4),
                    precision=round(metrics.precision, 4),
                    f1=round(metrics.f1, 4),
                    wall_clock_sec=elapsed,
                )
                _append_jsonl(self.log_path, rec)
                completed_ids.add(record_id)
                if self.progress_callback is not None:
                    self.progress_callback(
                        f"[ABL {unit_idx}/{total_units}] {arm} | {doc.item_id} | "
                        f"findings={rec.findings_count} q={rec.questions_asked} "
                        f"recall={rec.gap_recall:.3f} ({elapsed:.2f}s)"
                    )

        all_recs = self._load_existing()
        report = summarize_ablation_records(
            all_recs,
            run_mode=self.mode,
            split=self.split,
            include_promise=self.include_promise,
            seed=self.seed,
        )
        _atomic_write_lf(
            self.summary_path,
            json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True),
        )
        return report


__all__ = [
    "ABLATION_ARMS",
    "AblationArmName",
    "AblationArmSummary",
    "AblationItemRecord",
    "AblationReport",
    "AblationRunner",
    "build_critics_for_arm",
    "summarize_ablation_records",
]
