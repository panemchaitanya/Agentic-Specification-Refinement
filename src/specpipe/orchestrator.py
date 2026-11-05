"""Orchestrator: drafts functional requirements from a PRD."""
from __future__ import annotations

from .llm import _extract_json
from .schemas import Requirement, TRACE_PRD

_SYSTEM = """ROLE: orchestrator
You extract draft functional requirements (FRs) from a product requirements
document (PRD). Return JSON only:
{"frs": [{"text": "<requirement sentence>", "prd_sentence": "<exact PRD sentence it comes from>"}]}
Rules: only requirements the PRD actually states; never invent features;
each prd_sentence must be a verbatim quote from the PRD."""


def draft_frs(prd_text: str, llm) -> list:
    """Draft FRs from the PRD. Every FR traces to a PRD sentence."""
    raw = llm.complete(_SYSTEM, prd_text)
    data = _extract_json(raw)
    out = []
    for i, item in enumerate(data.get("frs", []), start=1):
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        out.append(Requirement(
            id=f"FR-{i}", kind="FR", text=text,
            trace_kind=TRACE_PRD,
            trace_ref=str(item.get("prd_sentence", "")).strip()))
    return out
