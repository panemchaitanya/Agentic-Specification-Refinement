"""End-to-end pipeline: PRD -> draft FRs -> critics -> targeted questions
-> trace-linked final spec.

Composition is deterministic so every output line's trace is exact.
"""
from __future__ import annotations

from . import gapcheck
from .critics import default_critics
from .orchestrator import draft_frs
from .schemas import (Requirement, Spec, TRACE_CRITIC, TRACE_HUMAN)


def analyze(prd_text: str, llm) -> dict:
    """Stage A: draft FRs, run critics, select questions.

    Returns everything the UI needs to render the Questions screen;
    the caller keeps `draft` and `findings` for finalize().
    """
    draft = draft_frs(prd_text, llm)
    findings = []
    for critic in default_critics(llm):
        findings.extend(critic.review(prd_text, draft))
    findings = gapcheck.dedupe(findings)
    questions = gapcheck.select_questions(findings)
    return {"draft": draft, "findings": findings, "questions": questions}


def finalize(title: str, draft: list, findings: list,
             questions: list, answers: dict) -> Spec:
    """Stage B: compose the trace-linked spec.

    answers maps question id -> answer text (empty means unanswered).
    Every spec line traces to a PRD sentence, a critic, or a human answer.
    """
    reqs = list(draft)  # FRs, already traced to PRD sentences
    n = 0

    def next_id():
        nonlocal n
        n += 1
        return f"NFR-{n}"

    # Auto-resolvable findings (vague wording fixed by a critic).
    for f in gapcheck.auto_resolvable(findings):
        reqs.append(Requirement(
            id=next_id(), kind=f.category, text=f.suggestion,
            trace_kind=TRACE_CRITIC, trace_ref=f.critic))

    # Escalated findings: answered ones become human-traced NFRs;
    # unanswered ones enter as open items marked from the critic.
    by_issue = {}
    for q in questions:
        for ref in q.finding_refs:
            by_issue[ref] = q
    for f in findings:
        if f.kind not in ("missing_context", "conflict"):
            continue
        q = by_issue.get(f.issue)
        answer = (answers.get(q.id, "").strip() if q else "")
        if answer:
            reqs.append(Requirement(
                id=next_id(), kind=f.category,
                text=f"{f.suggestion} (specified: {answer})",
                trace_kind=TRACE_HUMAN, trace_ref=q.id))
        else:
            reqs.append(Requirement(
                id=next_id(), kind=f.category,
                text=f"OPEN: {f.issue} - {f.suggestion}",
                trace_kind=TRACE_CRITIC, trace_ref=f.critic))
    return Spec(title=title, requirements=reqs)
