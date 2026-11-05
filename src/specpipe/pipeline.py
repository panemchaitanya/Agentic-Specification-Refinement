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
    Every spec line traces to a PRD sentence, a critic, or a human answer,
    and carries user_confirmed so lines a critic auto-applied are visible
    as not human-approved (they are evaluation targets, not silent edits).
    """
    reqs = list(draft)  # PRD-traced lines (FR + NFR-typed), user-stated

    # Auto-resolvable findings (vague wording fixed by a critic).
    # These enter WITHOUT human confirmation: the escalation rule applies
    # them directly, so they are marked user_confirmed=False.
    for f in gapcheck.auto_resolvable(findings):
        reqs.append(Requirement(
            id="", kind=f.category, text=f.suggestion,
            trace_kind=TRACE_CRITIC, trace_ref=f.critic,
            user_confirmed=False))

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
                id="", kind=f.category,
                text=f"{f.suggestion} (specified: {answer})",
                trace_kind=TRACE_HUMAN, trace_ref=q.id,
                user_confirmed=True))
        else:
            reqs.append(Requirement(
                id="", kind=f.category,
                text=f"OPEN: {f.issue} - {f.suggestion}",
                trace_kind=TRACE_CRITIC, trace_ref=f.critic,
                user_confirmed=False))

    # One consistent renumbering pass: FR-1.. for kind FR, NFR-1.. for
    # every NFR category, in spec order. Draft ids were provisional.
    fr_n = nfr_n = 0
    for r in reqs:
        if r.kind == "FR":
            fr_n += 1
            r.id = f"FR-{fr_n}"
        else:
            nfr_n += 1
            r.id = f"NFR-{nfr_n}"
    return Spec(title=title, requirements=reqs)
