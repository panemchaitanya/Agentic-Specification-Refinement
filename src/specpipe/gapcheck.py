"""Gap and conflict check: decides which findings reach the human.

Per the proposal, a question goes to the human only when a critic reports
missing business context (missing_context) or when two critics disagree
(conflict). Vague-but-fixable findings become NFRs directly.
"""
from __future__ import annotations

from .schemas import Finding, Question

MAX_QUESTIONS = 5


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def dedupe(findings: list) -> list:
    """Merge identical flags raised by more than one critic."""
    seen = {}
    for f in findings:
        key = (f.category, _norm(f.issue))
        if key not in seen:
            seen[key] = f
    return list(seen.values())


def select_questions(findings: list) -> list:
    """Turn escalated findings into targeted human questions."""
    questions = []
    escalated = [f for f in findings
                 if f.kind in ("missing_context", "conflict")]
    for i, f in enumerate(escalated[:MAX_QUESTIONS], start=1):
        if f.kind == "conflict":
            text = (f"{f.issue} - resolving this may affect "
                    f"{f.conflicts_with or 'another requirement'}. "
                    f"What should take priority?")
        else:
            text = f"{f.issue}. What should the requirement be?"
        questions.append(Question(
            id=f"Q-{i}", text=text,
            context=f"{f.critic} critic: {f.reason}",
            finding_refs=[f.issue]))
    return questions


def auto_resolvable(findings: list) -> list:
    """Findings the pipeline can apply without the human (kind=vague)."""
    return [f for f in findings if f.kind == "vague"]
