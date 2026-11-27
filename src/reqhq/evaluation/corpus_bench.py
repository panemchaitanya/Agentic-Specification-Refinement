"""Checkpointed industrial SRS corpus scalability benchmark (61 PURE dev / 79 all docs)."""

from __future__ import annotations

from collections.abc import Callable
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from reqhq.alloy.emitter import AlloySpecEmitter
from reqhq.alloy.mock_analyzer import MockAlloyAnalyzer
from reqhq.alloy.models import AlloyCompatMode, AlloyRunStatus
from reqhq.alloy.repair_loop import AlloyVerificationController
from reqhq.alloy.runner import AlloyAnalyzer
from reqhq.elicitation.critics import CriticPanel
from reqhq.elicitation.decomposer import RequirementDecomposer
from reqhq.evaluation.models import RunMode
from reqhq.evaluation.runner import LiveExecutionUnavailableError, _build_offline_router
from reqhq.evaluation.telemetry import get_process_peak_ram_mb
from reqhq.ingestion import (
    ChunkingResult,
    DocumentChunk,
    HierarchicalChunker,
    LocalFileLoader,
    PDFTextCleaner,
    SemanticSRSChunker,
    SnapshotStore,
    normalize_file,
)
from reqhq.llm.router import ProviderRouter
from reqhq.pbt import PBTSpecEmitter, export_pbt_bundle, verify_contract_lock
from reqhq.synthesis.triage import SocioTechnicalTriager

V1_TRUNCATION_THRESHOLD_CHARS = 120_000


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


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return round(ordered[idx], 4)


class CorpusDocRecord(BaseModel):
    """Per-document end-to-end reliability & scalability record across real PURE SRS documents."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(..., min_length=1)
    rel_path: str = Field(..., min_length=1)
    split: Literal["dev", "test"]
    run_mode: RunMode
    char_count: int = Field(..., ge=0)
    cleaned_char_count: int = Field(default=0, ge=0)
    smart_frontend_enabled: bool = False
    skipped_zero_llm_chunks: int = Field(default=0, ge=0)
    over_120k_chars: bool
    chunk_count: int = Field(..., ge=1)
    truncation_ratio: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    node_count: int = Field(..., ge=0)
    finding_count: int = Field(..., ge=0)
    triage_tracks: dict[str, int] = Field(default_factory=dict)
    alloy_status: str = Field(..., min_length=1)
    alloy_non_vacuous: bool
    pbt_property_count: int = Field(..., ge=0)
    lock_verified: bool
    wall_clock_sec: float = Field(..., ge=0.0, allow_inf_nan=False)
    peak_ram_mb: float = Field(..., ge=0.0, allow_inf_nan=False)
    error_events: list[str] = Field(default_factory=list)


class CorpusBenchSummary(BaseModel):
    """Aggregate corpus-scale reliability summary over all executed PURE SRS documents."""

    model_config = ConfigDict(extra="forbid")

    run_mode: RunMode
    split: Literal["dev", "all"]
    total_docs: int = Field(..., ge=0)
    over_120k_docs: int = Field(..., ge=0)
    zero_truncation_rate: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    alloy_verified_rate: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    alloy_non_vacuous_rate: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    lock_verified_rate: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    total_nodes: int = Field(..., ge=0)
    total_findings: int = Field(..., ge=0)
    total_pbt_properties: int = Field(..., ge=0)
    median_wall_clock_sec: float = Field(..., ge=0.0, allow_inf_nan=False)
    p95_wall_clock_sec: float = Field(..., ge=0.0, allow_inf_nan=False)
    max_peak_ram_mb: float = Field(..., ge=0.0, allow_inf_nan=False)
    error_count: int = Field(..., ge=0)


def summarize_corpus_records(
    records: list[CorpusDocRecord],
    *,
    run_mode: RunMode,
    split: Literal["dev", "all"],
) -> CorpusBenchSummary:
    """Compute deterministic aggregate scalability & formal-verification rates over corpus records."""
    n = len(records)
    if n == 0:
        return CorpusBenchSummary(
            run_mode=run_mode,
            split=split,
            total_docs=0,
            over_120k_docs=0,
            zero_truncation_rate=0.0,
            alloy_verified_rate=0.0,
            alloy_non_vacuous_rate=0.0,
            lock_verified_rate=0.0,
            total_nodes=0,
            total_findings=0,
            total_pbt_properties=0,
            median_wall_clock_sec=0.0,
            p95_wall_clock_sec=0.0,
            max_peak_ram_mb=0.0,
            error_count=0,
        )

    times = [r.wall_clock_sec for r in records]
    return CorpusBenchSummary(
        run_mode=run_mode,
        split=split,
        total_docs=n,
        over_120k_docs=sum(1 for r in records if r.over_120k_chars),
        zero_truncation_rate=round(sum(1 for r in records if r.truncation_ratio == 0.0) / n, 4),
        alloy_verified_rate=round(
            sum(1 for r in records if r.alloy_status == AlloyRunStatus.VERIFIED.value) / n, 4
        ),
        alloy_non_vacuous_rate=round(sum(1 for r in records if r.alloy_non_vacuous) / n, 4),
        lock_verified_rate=round(sum(1 for r in records if r.lock_verified) / n, 4),
        total_nodes=sum(r.node_count for r in records),
        total_findings=sum(r.finding_count for r in records),
        total_pbt_properties=sum(r.pbt_property_count for r in records),
        median_wall_clock_sec=_percentile(times, 50.0),
        p95_wall_clock_sec=_percentile(times, 95.0),
        max_peak_ram_mb=round(max(r.peak_ram_mb for r in records), 3),
        error_count=sum(len(r.error_events) for r in records),
    )


class CorpusBenchmarkRunner:
    """Resumable end-to-end scalability runner across all 61 PURE dev (or 79 all) SRS documents."""

    def __init__(
        self,
        out_dir: Path | str,
        *,
        mode: RunMode = RunMode.MOCK,
        split: Literal["dev", "all"] = "dev",
        frozen_root: Path | str = Path("data/frozen"),
        max_docs: int | None = None,
        seed: int = 42,
        alloy_compat: Literal["alloy6", "legacy"] = "alloy6",
        use_smart_frontend: bool = True,
        live_router: ProviderRouter | None = None,
        live_alloy_analyzer: AlloyAnalyzer | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        if max_docs is not None and max_docs < 1:
            raise ValueError("max_docs must be >= 1 when set")
        self.out_dir = Path(out_dir).resolve()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.split = split
        self.frozen_root = Path(frozen_root).resolve()
        self.max_docs = max_docs
        self.seed = int(seed)
        self.alloy_compat = AlloyCompatMode(alloy_compat)
        self.use_smart_frontend = bool(use_smart_frontend)
        self.live_router = live_router
        self.live_alloy_analyzer = live_alloy_analyzer
        self.progress_callback = progress_callback
        self.log_path = self.out_dir / "corpus_bench.jsonl"
        self.summary_path = self.out_dir / "corpus_summary.json"

    def _discover_documents(self) -> list[tuple[Literal["dev", "test"], Path]]:
        splits: tuple[Literal["dev", "test"], ...] = ("dev", "test") if self.split == "all" else ("dev",)
        discovered: list[tuple[Literal["dev", "test"], Path]] = []
        for sp in splits:
            text_dir = self.frozen_root / "pure" / "text" / sp
            if not text_dir.is_dir():
                raise FileNotFoundError(f"PURE text split directory not found: {text_dir}")
            for file_path in sorted(text_dir.glob("*.txt")):
                discovered.append((sp, file_path))
        if not discovered:
            raise ValueError(f"No PURE SRS .txt files found under {self.frozen_root / 'pure' / 'text'}")
        if self.max_docs is not None:
            discovered = discovered[: self.max_docs]
        return discovered

    def _load_existing_records(self) -> list[CorpusDocRecord]:
        if not self.log_path.is_file():
            return []
        records: list[CorpusDocRecord] = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped:
                records.append(CorpusDocRecord.model_validate_json(stripped))
        return records

    def run(self) -> CorpusBenchSummary:
        """Execute (or resume) the full PURE corpus scalability benchmark with atomic JSONL checkpoints."""
        if self.mode == RunMode.LIVE:
            if self.live_router is None or self.live_alloy_analyzer is None:
                raise LiveExecutionUnavailableError(
                    "Cannot run corpus-bench in mode='live' without both live_router and live_alloy_analyzer."
                )
            router = self.live_router
            analyzer: AlloyAnalyzer = self.live_alloy_analyzer
        else:
            router = _build_offline_router(strict_local=True)
            analyzer = MockAlloyAnalyzer(default_status=AlloyRunStatus.VERIFIED)

        docs = self._discover_documents()
        existing = self._load_existing_records()
        completed_ids = {rec.doc_id for rec in existing}

        loader = LocalFileLoader()
        chunker = HierarchicalChunker(max_chunk_chars=8000, overlap_chars=0)
        cleaner = PDFTextCleaner()
        sem_chunker = SemanticSRSChunker()
        decomposer = RequirementDecomposer(router)
        critics = CriticPanel(router)
        triager = SocioTechnicalTriager(router)
        alloy_emitter = AlloySpecEmitter()
        alloy_controller = AlloyVerificationController(
            router=router,
            analyzer=analyzer,
            max_local_repairs=1,
            max_scope=4,
        )
        pbt_emitter = PBTSpecEmitter()

        for idx, (sp, doc_path) in enumerate(docs, start=1):
            safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", doc_path.stem).strip("._-")[:90] or f"doc_{idx}"
            doc_id = f"pure-{sp}-{safe_stem}"
            if doc_id in completed_ids:
                continue

            started = time.perf_counter()
            error_events: list[str] = []
            with tempfile.TemporaryDirectory(
                prefix="reqhq_corpus_",
                ignore_cleanup_errors=True,
            ) as tmp_str:
                tmp_root = Path(tmp_str).resolve()
                ingested = loader.load_file(doc_path)
                snap = SnapshotStore(tmp_root / "sessions").save_document(doc_id, ingested)
                normalized = normalize_file(snap.snapshot_path, source_sha256=snap.sha256)

                cleaned_char_count = len(normalized.markdown)
                skipped_zero_llm_chunks = 0
                if self.use_smart_frontend:
                    clean_res = cleaner.clean_text(
                        normalized.markdown,
                        doc_id=doc_id,
                        source_path=doc_path.as_posix(),
                    )
                    cleaned_char_count = clean_res.cleaned_chars
                    sem_plan = sem_chunker.plan_document(clean_res.cleaned_text, doc_id=doc_id)
                    skipped_zero_llm_chunks = sem_plan.skipped_chunks
                    glossary = HierarchicalChunker.extract_glossary(sem_plan.promoted_markdown)
                    active_sem_chunks = [c for c in sem_plan.chunks if c.route_to_llm] or list(sem_plan.chunks)
                    doc_chunks: list[DocumentChunk] = []
                    cursor = 0
                    for c_idx, sc in enumerate(active_sem_chunks):
                        end_pos = cursor + len(sc.text)
                        doc_chunks.append(
                            DocumentChunk(
                                chunk_id=sc.chunk_id,
                                chunk_index=c_idx,
                                heading_path=list(sc.heading_path),
                                anchors=[],
                                start_char=cursor,
                                end_char=end_pos,
                                char_count=len(sc.text),
                                text=sc.text,
                                source_sha256=snap.sha256,
                            )
                        )
                        cursor = end_pos
                    chunking = ChunkingResult(
                        source_sha256=snap.sha256,
                        source_path=snap.snapshot_path,
                        total_chars=cursor,
                        covered_chars=cursor,
                        truncation_ratio=0.0,
                        chunk_count=len(doc_chunks),
                        max_chunk_chars=sem_chunker.config.target_chunk_chars,
                        overlap_chars=0,
                        glossary=glossary,
                        chunks=doc_chunks,
                    )
                else:
                    chunking = chunker.chunk(normalized)

                if self.progress_callback is not None:
                    self.progress_callback(
                        f"[CORPUS {idx}/{len(docs)}] {doc_path.name} | "
                        f"prep chars={len(normalized.markdown)}->{cleaned_char_count} "
                        f"chunks={len(chunking.chunks)} (skipped={skipped_zero_llm_chunks}) -> decomposing..."
                    )

                graph = decomposer.decompose_chunks(
                    chunking,
                    session_id=doc_id,
                    deduplicate_near_duplicates=self.use_smart_frontend,
                )
                findings, _disagreements = critics.run_on_nodes(
                    graph,
                    glossary_header=chunking.glossary.to_prompt_header(),
                )
                plan = triager.triage(graph, session_id=doc_id)
                track_counts: dict[str, int] = {}
                for item in plan.items:
                    tkey = item.track.value
                    track_counts[tkey] = track_counts.get(tkey, 0) + 1

                spec_md = alloy_emitter.emit_spec_md(graph, triage_plan=plan, compat=self.alloy_compat)
                outcome = alloy_controller.verify_spec(spec_md, graph, compat=self.alloy_compat)
                alloy_non_vacuous = outcome.status == AlloyRunStatus.VERIFIED and bool(
                    outcome.run_results and outcome.run_results[-1].vacuity_passed
                )
                if outcome.status == AlloyRunStatus.COMPILE_ERROR:
                    error_events.append("ALLOY_SYNTAX")
                elif outcome.status == AlloyRunStatus.VACUOUS_UNSAT:
                    error_events.append("ALLOY_VACUOUS")
                elif outcome.status == AlloyRunStatus.COUNTEREXAMPLE_FOUND:
                    error_events.append("UNSAT_CONTRADICTION")

                pbt_doc = pbt_emitter.emit_contract(
                    graph,
                    session_id=doc_id,
                    spec_markdown=outcome.spec_markdown,
                    seed=self.seed + idx,
                )
                pbt_dir = tmp_root / "pbt_out"
                export_pbt_bundle(pbt_doc, pbt_dir)
                verify_contract_lock(pbt_dir)

            elapsed = round(max(0.0, time.perf_counter() - started), 4)
            char_count = len(normalized.markdown)
            record = CorpusDocRecord(
                doc_id=doc_id,
                rel_path=f"data/frozen/pure/text/{sp}/{doc_path.name}",
                split=sp,
                run_mode=self.mode,
                char_count=char_count,
                cleaned_char_count=cleaned_char_count,
                smart_frontend_enabled=self.use_smart_frontend,
                skipped_zero_llm_chunks=skipped_zero_llm_chunks,
                over_120k_chars=char_count > V1_TRUNCATION_THRESHOLD_CHARS,
                chunk_count=len(chunking.chunks),
                truncation_ratio=round(chunking.truncation_ratio, 6),
                node_count=len(graph.nodes),
                finding_count=len(findings),
                triage_tracks=track_counts,
                alloy_status=outcome.status.value,
                alloy_non_vacuous=alloy_non_vacuous,
                pbt_property_count=len(pbt_doc.properties),
                lock_verified=True,
                wall_clock_sec=elapsed,
                peak_ram_mb=get_process_peak_ram_mb(),
                error_events=error_events,
            )
            _append_jsonl(self.log_path, record)
            completed_ids.add(doc_id)
            if self.progress_callback is not None:
                self.progress_callback(
                    f"[CORPUS {idx}/{len(docs)}] {doc_path.name} | chars={record.char_count} "
                    f"chunks={record.chunk_count} trunc={record.truncation_ratio * 100:.1f}% "
                    f"nodes={record.node_count} alloy={record.alloy_status} "
                    f"vacuity={record.alloy_non_vacuous} lock={record.lock_verified} ({elapsed:.2f}s)"
                )

        all_records = self._load_existing_records()
        summary = summarize_corpus_records(all_records, run_mode=self.mode, split=self.split)
        _atomic_write_lf(
            self.summary_path,
            json.dumps(summary.model_dump(mode="json"), indent=2, sort_keys=True),
        )
        return summary


__all__ = [
    "CorpusBenchSummary",
    "CorpusBenchmarkRunner",
    "CorpusDocRecord",
    "V1_TRUNCATION_THRESHOLD_CHARS",
    "summarize_corpus_records",
]
