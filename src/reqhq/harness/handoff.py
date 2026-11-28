"""Antigravity Contract-First Handoff Bundler combining Synthesis + PBT Guardrails."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from reqhq.harness.models import HandoffBundleManifest
from reqhq.schemas.requirements import RequirementGraph
from reqhq.synthesis.bundle import write_synthesis_bundle
from reqhq.synthesis.models import TriagePlan

if TYPE_CHECKING:
    from reqhq.pbt.models import PBTContractDocument


def _assert_not_repo_nygard_adr_dir(out_dir: Path) -> None:
    """Refuse to write handoff artifacts inside repository docs/adr/ governance tree."""
    resolved = out_dir.resolve()
    parts_lower = [part.lower() for part in resolved.parts]
    for idx in range(len(parts_lower) - 1):
        if parts_lower[idx] == "docs" and parts_lower[idx + 1] == "adr":
            raise ValueError(
                f"Refusing to write Antigravity handoff bundle into repo Nygard ADR directory: {resolved}"
            )


def _write_lf(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = content.replace("\r\n", "\n").rstrip("\n") + "\n"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(normalized)


def _render_handoff_instructions(
    *,
    session_id: str,
    target_dir: Path,
    adr_files: Sequence[Path],
    guardrail_files: Sequence[Path],
    pbt_test_files: Sequence[Path],
    lock_file: Path,
    pbt_refs_by_req: Mapping[str, Sequence[str]],
    seed: int,
) -> str:
    """Render HANDOFF_INSTRUCTIONS.md encoding the Contract-First Verification Gate."""
    adr_lines = [
        f"- `{p.relative_to(target_dir).as_posix()}`" for p in adr_files
    ] or ["- _(none emitted)_"]
    guardrail_lines = [
        f"- `{p.relative_to(target_dir).as_posix()}`" for p in guardrail_files
    ] or ["- _(none emitted)_"]
    pbt_test_lines = [
        f"- `{p.relative_to(target_dir).as_posix()}`" for p in pbt_test_files
    ] or ["- _(none emitted)_"]

    req_summary_lines = [
        f"- `{req_id}`: {', '.join(f'`{prop_id}`' for prop_id in prop_ids)}"
        for req_id, prop_ids in sorted(pbt_refs_by_req.items())
    ] or ["- _(no mapped PBT properties)_"]

    lock_rel = lock_file.relative_to(target_dir).as_posix()

    sections = [
        f"# Antigravity Handoff Instructions — Session `{session_id}`",
        "",
        "## Bundle Inventory",
        "- `Spec.md` — Verified literate specification and Alloy model.",
        "- `Design.md` — Software architecture & module boundaries.",
        "- `Process_and_Training.md` — Process and human-training obligations.",
        "- `traceability_matrix.json` — End-to-end `source_sha256 -> REQ -> ADR -> Alloy -> PBT` trace.",
        "- `PBT_Spec.yaml` — Canonical Hypothesis + Pydantic guardrail contract (LOCKED).",
        f"- `{lock_rel}` — SHA-256 tamper-detection manifest for locked contract artifacts.",
        "",
        "### Target-System ADRs (`ADRs/`)",
        *adr_lines,
        "",
        "### Runtime Guardrails (`guardrails/`)",
        *guardrail_lines,
        "",
        "### Locked Property-Based Tests (`tests/properties/`)",
        *pbt_test_lines,
        "",
        "### Requirement-to-Property Trace",
        *req_summary_lines,
        "",
        "## Contract-First Verification Gate (MANDATORY)",
        "",
        "1. **Read-Only Contract Lock Enforcement**:",
        f"   - NEVER modify `PBT_Spec.yaml`, `guardrails/*.py`, `tests/properties/*.py`, or `{lock_rel}`.",
        "   - Every verification run MUST pass `verify_contract_lock()` before grading.",
        "   - Any 1-byte modification triggers `ContractTamperError` and fails the run closed.",
        "2. **Governance Separation**:",
        "   - Target-system ADRs live strictly under bundle `ADRs/`.",
        "   - NEVER copy or emit target-system ADRs into repository `docs/adr/`.",
        "3. **Implement Against Design & Runtime Guardrails**:",
        "   - Implement target modules according to `Design.md` and `ADRs/`.",
        "   - Import Pydantic v2 validators and `@enforce_contract` wrappers from `guardrails/`.",
        "4. **Property-Based Testing Gate**:",
        f"   - Execute `pytest tests/properties` with deterministic seed `{seed}` (`derandomize=True`).",
        "   - Classify any falsifying counterexample into `CODE_DEFECT`, `SPEC_AMBIGUITY`, or `STRATEGY_MISMATCH`.",
        "   - Fix code defects in the implementation; escalate spec ambiguities back to human review.",
        "5. **Mutation Testing Gate**:",
        "   - Run the AST mutation engine (`ASTMutantEngine` on Windows 11 / `MutmutRunner` on POSIX) against implemented modules.",
        "   - Ensure unit + PBT suites kill injected relational, boolean, arithmetic, return, and boundary mutations.",
    ]
    return "\n".join(sections) + "\n"


def build_handoff_bundle(
    out_dir: Path | str,
    *,
    session_id: str,
    graph: RequirementGraph,
    plan: TriagePlan,
    spec_markdown: str,
    pbt_doc: PBTContractDocument,
    assumptions: Sequence[Any] = (),
    alloy_refs: Mapping[str, Sequence[str]] | None = None,
) -> HandoffBundleManifest:
    """Build a complete, contract-locked Antigravity handoff bundle.

    Writes:
    - Phase 2 synthesis bundle (`Spec.md`, `ADRs/`, `Design.md`,
      `Process_and_Training.md`, `traceability_matrix.json` populated with
      `pbt_refs=pbt_doc.to_pbt_refs_by_req()`)
    - Phase 3 PBT guardrail bundle (`PBT_Spec.yaml`, `guardrails/`,
      `tests/properties/`, `pbt_lock.sha256`)
    - `HANDOFF_INSTRUCTIONS.md` encoding the Contract-First Verification Gate

    Refuses to write inside repository `docs/adr/` and fails closed if lock
    verification fails immediately after emission.
    """
    cleaned_session = session_id.strip()
    if not cleaned_session:
        raise ValueError("session_id must not be blank or whitespace-only")

    target_dir = Path(out_dir)
    _assert_not_repo_nygard_adr_dir(target_dir)

    pbt_refs = pbt_doc.to_pbt_refs_by_req()

    synthesis_result = write_synthesis_bundle(
        target_dir,
        session_id=cleaned_session,
        graph=graph,
        plan=plan,
        assumptions=assumptions,
        alloy_refs=alloy_refs,
        pbt_refs=pbt_refs,
        spec_markdown=spec_markdown,
    )
    assert synthesis_result.spec_md is not None

    from reqhq.pbt import export_pbt_bundle, verify_contract_lock

    pbt_bundle = export_pbt_bundle(pbt_doc, target_dir)
    verify_contract_lock(target_dir, lock_filename=pbt_bundle.lock_file.name)

    instructions_path = target_dir / "HANDOFF_INSTRUCTIONS.md"
    instructions_md = _render_handoff_instructions(
        session_id=cleaned_session,
        target_dir=target_dir,
        adr_files=synthesis_result.adr_files,
        guardrail_files=pbt_bundle.guardrail_files,
        pbt_test_files=pbt_bundle.test_files,
        lock_file=pbt_bundle.lock_file,
        pbt_refs_by_req=pbt_refs,
        seed=int(getattr(pbt_doc, "seed", 42)),
    )
    _write_lf(instructions_path, instructions_md)

    return HandoffBundleManifest(
        session_id=cleaned_session,
        out_dir=target_dir,
        instructions_md=instructions_path,
        spec_md=synthesis_result.spec_md,
        design_md=synthesis_result.design_md,
        process_training_md=synthesis_result.process_training_md,
        pbt_spec_yaml=pbt_bundle.pbt_spec_yaml,
        traceability_matrix_json=synthesis_result.traceability_matrix_json,
        adr_files=list(synthesis_result.adr_files),
        guardrail_files=list(pbt_bundle.guardrail_files),
        pbt_test_files=list(pbt_bundle.test_files),
        lock_file=pbt_bundle.lock_file,
    )


__all__ = [
    "build_handoff_bundle",
]
