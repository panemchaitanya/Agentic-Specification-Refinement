"""Stage 5a Synthesis Bundle writer and traceability matrix generator."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
from typing import Any

from reqhq.schemas.requirements import RequirementGraph, RequirementNode, RequirementStatus
from reqhq.synthesis.design_emitter import emit_design_md
from reqhq.synthesis.models import (
    AssumptionLike,
    EmittedADRFile,
    SynthesisBundleResult,
    TraceabilityMatrix,
    TraceabilityRow,
    TriagePlan,
)
from reqhq.synthesis.process_training_emitter import emit_process_and_training_md
from reqhq.synthesis.routing_adr_emitter import build_assumption_adrs, build_routing_adrs

_INACTIVE_STATUSES: frozenset[RequirementStatus] = frozenset(
    {RequirementStatus.DEPRECATED, RequirementStatus.SUPERSEDED}
)


def _assert_not_repo_nygard_adr_dir(out_dir: Path) -> None:
    """Prevent generated target-system ADRs from ever writing into repo docs/adr/."""
    resolved = out_dir.resolve()
    parts_lower = [part.lower() for part in resolved.parts]
    for idx in range(len(parts_lower) - 1):
        if parts_lower[idx] == "docs" and parts_lower[idx + 1] == "adr":
            raise ValueError(
                f"Refusing to write target-system synthesis bundle into repo Nygard ADR directory: {resolved}"
            )


def _node_source_trace(node: RequirementNode) -> tuple[str, str]:
    if node.trace_links:
        first = node.trace_links[0]
        span = first.anchor.strip() or first.excerpt.strip() or first.source_uri
        return first.source_sha256, span
    fallback_sha = hashlib.sha256(
        f"{node.req_id}:{node.statement}".encode("utf-8")
    ).hexdigest()
    return fallback_sha, f"synthetic:{node.req_id}"


def _write_lf(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = content.replace("\r\n", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(normalized)


def build_traceability_matrix(
    *,
    session_id: str,
    graph: RequirementGraph,
    plan: TriagePlan,
    adrs: Sequence[EmittedADRFile],
    alloy_refs: Mapping[str, Sequence[str]] | None = None,
    pbt_refs: Mapping[str, Sequence[str]] | None = None,
) -> TraceabilityMatrix:
    """Build end-to-end TraceabilityMatrix and detect any orphaned active requirements."""
    alloy_map = alloy_refs or {}
    pbt_map = pbt_refs or {}

    rows: list[TraceabilityRow] = []
    orphaned_req_ids: list[str] = []

    for node in graph.nodes:
        if node.status in _INACTIVE_STATUSES:
            continue

        source_sha, span = _node_source_trace(node)
        item = plan.for_req(node.req_id)

        matching_adrs = [
            adr
            for adr in adrs
            if node.req_id in (adr.frontmatter.get("requirement_ids") or [])
        ]
        routing_adrs = [adr for adr in matching_adrs if adr.kind == "routing"]
        adr_ids = [adr.adr_id for adr in matching_adrs]

        has_unconfirmed = any(
            adr.frontmatter.get("user_confirmed") is False for adr in matching_adrs
        )
        req_alloy = [str(ref).strip() for ref in alloy_map.get(node.req_id, ()) if str(ref).strip()]
        req_pbt = [str(ref).strip() for ref in pbt_map.get(node.req_id, ()) if str(ref).strip()]

        if item is None or not routing_adrs:
            orphaned_req_ids.append(node.req_id)

        rows.append(
            TraceabilityRow(
                req_id=node.req_id,
                source_sha256=source_sha,
                span=span,
                track=item.track.value if item is not None else "unassigned",
                adr_ids=adr_ids,
                alloy_refs=req_alloy,
                pbt_refs=req_pbt,
                user_confirmed=not has_unconfirmed,
            )
        )

    return TraceabilityMatrix(
        session_id=session_id,
        rows=rows,
        orphaned_req_ids=orphaned_req_ids,
    )


def write_synthesis_bundle(
    out_dir: Path | str,
    *,
    session_id: str,
    graph: RequirementGraph,
    plan: TriagePlan,
    assumptions: Sequence[AssumptionLike | Mapping[str, Any]] = (),
    alloy_refs: Mapping[str, Sequence[str]] | None = None,
    pbt_refs: Mapping[str, Sequence[str]] | None = None,
    spec_markdown: str | None = None,
    raise_on_orphans: bool = False,
) -> SynthesisBundleResult:
    """Write ADRs/, Design.md, Process_and_Training.md, optional Spec.md, and traceability_matrix.json."""
    target_dir = Path(out_dir)
    _assert_not_repo_nygard_adr_dir(target_dir)

    routing_adrs = build_routing_adrs(
        graph,
        plan,
        alloy_refs=alloy_refs,
        pbt_refs=pbt_refs,
    )
    assumption_adrs = build_assumption_adrs(assumptions)
    all_adrs: list[EmittedADRFile] = [*routing_adrs, *assumption_adrs]

    matrix = build_traceability_matrix(
        session_id=session_id,
        graph=graph,
        plan=plan,
        adrs=all_adrs,
        alloy_refs=alloy_refs,
        pbt_refs=pbt_refs,
    )
    if raise_on_orphans and matrix.orphaned_req_ids:
        raise ValueError(
            f"Traceability incomplete: orphaned active requirements detected: {matrix.orphaned_req_ids}"
        )

    adrs_dir = target_dir / "ADRs"
    adrs_dir.mkdir(parents=True, exist_ok=True)

    written_adr_paths: list[Path] = []
    for emitted in all_adrs:
        adr_path = adrs_dir / emitted.filename
        _write_lf(adr_path, emitted.markdown)
        emitted.path = adr_path
        written_adr_paths.append(adr_path)

    design_md_content = emit_design_md(graph, plan, all_adrs)
    design_md_path = target_dir / "Design.md"
    _write_lf(design_md_path, design_md_content)

    process_training_content = emit_process_and_training_md(graph, plan, all_adrs)
    process_training_path = target_dir / "Process_and_Training.md"
    _write_lf(process_training_path, process_training_content)

    spec_md_path: Path | None = None
    if spec_markdown is not None:
        spec_md_path = target_dir / "Spec.md"
        _write_lf(spec_md_path, spec_markdown)

    matrix_path = target_dir / "traceability_matrix.json"
    matrix_json = json.dumps(matrix.model_dump(mode="json"), indent=2, sort_keys=True)
    _write_lf(matrix_path, matrix_json)

    return SynthesisBundleResult(
        session_id=session_id,
        out_dir=target_dir,
        adr_files=written_adr_paths,
        design_md=design_md_path,
        process_training_md=process_training_path,
        traceability_matrix_json=matrix_path,
        spec_md=spec_md_path,
        orphaned_req_ids=list(matrix.orphaned_req_ids),
    )


__all__ = [
    "build_traceability_matrix",
    "write_synthesis_bundle",
]
