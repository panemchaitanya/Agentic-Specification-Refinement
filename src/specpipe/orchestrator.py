"""Orchestrator: drafts requirements from a PRD, typed FR vs NFR.

Typing is a conservative keyword heuristic (quality wording -> NFR
category), applied the same way to LLM and mock drafts so the two modes
stay comparable. It is a development aid, not the validated FR/NFR
boundary: the Stage 2 extraction guidelines and matching rubric own that.
"""
from __future__ import annotations

from .llm import _extract_json
from .schemas import Requirement, TRACE_PRD

_SYSTEM = """ROLE: orchestrator
You extract draft functional requirements (FRs) from a product requirements
document (PRD). Return JSON only:
{"frs": [{"text": "<requirement sentence>", "prd_sentence": "<exact PRD sentence it comes from>"}]}
Rules: only requirements the PRD actually states; never invent features;
each prd_sentence must be a verbatim quote from the PRD."""


# Quality wording -> NFR category. Checked in order; first hit wins.
QUALITY_HINTS = [
    ("performance", ("fast", "quick", "slow", "responsive", "latency",
                     "real-time", "realtime", "seconds", "throughput")),
    ("security", ("secure", "security", "encrypted", "encryption",
                  "protected", "private")),
    ("scalability", ("scalable", "scale", "concurrent", "load")),
    ("reliability", ("reliable", "reliability", "durable", "durability",
                     "uptime", "available", "availability")),
    ("usability", ("easy to use", "intuitive", "usable", "usability",
                   "user-friendly")),
]


def classify_statement(text: str) -> str:
    """Type a PRD statement as "FR" or an NFR category (heuristic).

    Only retypes on explicit quality wording; ambiguous statements stay
    FR so the heuristic errs toward the PRD author's literal words.
    """
    low = text.lower()
    for category, words in QUALITY_HINTS:
        if any(w in low for w in words):
            return category
    return "FR"


def draft_frs(prd_text: str, llm) -> list:
    """Draft requirements from the PRD, each traced to a PRD sentence.

    Ids are provisional and per kind; finalize() renumbers the final spec.
    """
    raw = llm.complete(_SYSTEM, prd_text)
    data = _extract_json(raw)
    out = []
    counters = {"FR": 0}
    for item in data.get("frs", []):
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        kind = classify_statement(text)
        counters[kind] = counters.get(kind, 0) + 1
        prefix = "FR" if kind == "FR" else "NFR"
        out.append(Requirement(
            id=f"{prefix}-{counters[kind]}", kind=kind, text=text,
            trace_kind=TRACE_PRD,
            trace_ref=str(item.get("prd_sentence", "")).strip(),
            user_confirmed=True))
    return out
