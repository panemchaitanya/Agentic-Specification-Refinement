"""Data contracts for the ReqHQ specification-refinement pipeline.

These schemas are the stable contracts every stage depends on:
FR/NFR records with trace, critic findings with a pointer to the PRD
sentence that caused them, human questions, and the final spec.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

# NFR category taxonomy from the proposal's completeness penalty.
NFR_CATEGORIES = [
    "security",
    "performance",
    "scalability",
    "reliability",
    "usability",
    "other",
]

# Trace source kinds: where a spec line came from.
TRACE_PRD = "prd_sentence"
TRACE_CRITIC = "critic"
TRACE_HUMAN = "human_answer"


@dataclass
class Requirement:
    """One functional or non-functional requirement with its trace."""
    id: str
    kind: str  # "FR" or an NFR category from NFR_CATEGORIES
    text: str
    trace_kind: str  # TRACE_PRD / TRACE_CRITIC / TRACE_HUMAN
    trace_ref: str  # PRD sentence quote, critic name, or question id

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Finding:
    """A critic's report of a missing or vague NFR.

    prd_sentence is the exact PRD sentence that caused the flag.
    kind is "missing_context" (needs business input) or "vague"
    (present but unquantified) or "conflict" (clashes with another finding).
    """
    critic: str  # "security" | "architecture" | "performance"
    category: str  # NFR category
    kind: str  # "missing_context" | "vague" | "conflict"
    issue: str  # what is missing or vague
    suggestion: str  # the NFR text the critic proposes
    prd_sentence: str  # exact sentence that caused the flag
    reason: str  # why this matters
    conflicts_with: Optional[str] = None  # other critic name, for conflicts

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Question:
    """A targeted question for the human."""
    id: str
    text: str
    context: str  # why the pipeline is asking (which findings)
    finding_refs: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Spec:
    """The final, trace-linked specification."""
    title: str
    requirements: list = field(default_factory=list)  # list[Requirement]

    def to_dict(self) -> dict:
        return {"title": self.title,
                "requirements": [r.to_dict() for r in self.requirements]}

    def trace_report(self) -> dict:
        """Count of spec lines by trace source; every line must be traced."""
        counts = {TRACE_PRD: 0, TRACE_CRITIC: 0, TRACE_HUMAN: 0}
        untraced = 0
        for r in self.requirements:
            if r.trace_kind in counts:
                counts[r.trace_kind] += 1
            else:
                untraced += 1
        return {"by_source": counts, "untraced": untraced,
                "total": len(self.requirements)}
