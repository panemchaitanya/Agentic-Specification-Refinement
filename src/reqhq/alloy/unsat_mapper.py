"""Map Alloy UNSAT core lines and facts via @trace markers to ConflictQuestions."""

from __future__ import annotations

from collections.abc import Mapping
import re

from reqhq.alloy.models import (
    AlloyRunResult,
    AlloyRunStatus,
    ConflictQuestion,
    ExtractedAlloyBlock,
)
from reqhq.schemas.requirements import RequirementGraph

_FACT_DECL_RE = re.compile(r"^\s*fact\s+([A-Za-z_][A-Za-z0-9_']*)\b")


def _find_fact_lines(block: ExtractedAlloyBlock, fact_names: set[str]) -> list[int]:
    """Find 1-based line numbers where named facts are declared inside an Alloy block."""
    if not fact_names:
        return []
    matched_lines: list[int] = []
    for line_no, line in enumerate(block.code.splitlines(), start=1):
        m = _FACT_DECL_RE.match(line)
        if m and m.group(1) in fact_names:
            matched_lines.append(line_no)
    return matched_lines


def map_unsat_to_conflicts(
    result: AlloyRunResult,
    blocks: Mapping[str, ExtractedAlloyBlock],
    graph: RequirementGraph,
) -> list[ConflictQuestion]:
    """Translate an Alloy VACUOUS_UNSAT / UNSAT core result into ConflictQuestions.

    Uses `@trace` line maps in `blocks` plus fact-name lookup to trace UNSAT
    cores directly back to conflicting `REQ-ID`s and requirement statements in `graph`.
    """
    if (
        result.status != AlloyRunStatus.VACUOUS_UNSAT
        and not result.unsat_lines
        and not result.unsat_facts
    ):
        return []

    req_ids: list[str] = []
    seen_reqs: set[str] = set()
    fact_set = set(result.unsat_facts)

    for module_name in sorted(blocks):
        block = blocks[module_name]
        candidate_lines = list(result.unsat_lines) + _find_fact_lines(block, fact_set)
        for rid in block.req_ids_for_lines(candidate_lines):
            if rid not in seen_reqs:
                seen_reqs.add(rid)
                req_ids.append(rid)

    # Also match fact names containing sanitized REQ-IDs (e.g. Inv_REQ_001 -> REQ-001)
    if fact_set:
        for node in graph.nodes:
            sanitized = re.sub(r"[^A-Za-z0-9_]", "_", node.req_id)
            if any(sanitized in fname or node.req_id in fname for fname in fact_set):
                if node.req_id not in seen_reqs:
                    seen_reqs.add(node.req_id)
                    req_ids.append(node.req_id)

    fact_labels = list(result.unsat_facts)
    if not fact_labels and result.unsat_lines:
        fact_labels = [f"line:{ln}" for ln in result.unsat_lines]

    if req_ids:
        req_summaries: list[str] = []
        for rid in req_ids:
            node = graph.get_node(rid)
            if node is not None:
                req_summaries.append(f"{rid} ({node.title}: {node.statement})")
            else:
                req_summaries.append(rid)

        facts_desc = ", ".join(fact_labels) if fact_labels else "unspecified UNSAT facts"
        prompt = (
            f"The formal Alloy specification is vacuously unsatisfiable (UNSAT) due to "
            f"conflicting constraints in [{facts_desc}] affecting requirements: "
            f"{'; '.join(req_summaries)}. Which requirement should take precedence or be relaxed?"
        )
        resolutions: list[str] = []
        if len(req_ids) >= 2:
            resolutions.append(
                f"Prioritize {req_ids[0]} over {req_ids[1]} and scope {req_ids[1]} as a conditional exception."
            )
            resolutions.append(
                f"Prioritize {req_ids[1]} over {req_ids[0]} and relax the invariant for {req_ids[0]}."
            )
            resolutions.append(
                f"Split {', '.join(req_ids)} into disjoint operational states or roles."
            )
        else:
            resolutions.append(
                f"Relax or clarify the multiplicity/constraint in {req_ids[0]} so a valid instance exists."
            )
            resolutions.append(
                f"Move the non-software obligation in {req_ids[0]} to a Process or Training ADR."
            )
    else:
        facts_desc = ", ".join(fact_labels) if fact_labels else "global facts"
        prompt = (
            f"The formal Alloy specification is vacuously unsatisfiable (UNSAT) in [{facts_desc}], "
            "meaning no valid system instance satisfies all facts simultaneously. "
            "Which constraint should be weakened so a non-empty witness exists?"
        )
        resolutions = [
            "Inspect and relax contradictory facts in core_domain.als or behavior.als.",
            "Add explicit // @trace REQ-ID annotations to all custom facts for precise attribution.",
        ]

    return [
        ConflictQuestion(
            conflict_id="CONFLICT-001",
            req_ids=req_ids,
            unsat_facts=fact_labels,
            prompt=prompt,
            suggested_resolutions=resolutions,
        )
    ]


__all__ = ["map_unsat_to_conflicts"]
