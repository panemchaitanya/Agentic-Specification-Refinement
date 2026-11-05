"""Baselines B1-B4 from the submitted proposal (section 4, Comparisons).

Exact operationalization (fixed here before any evaluation data exists;
the rationale for each choice is in docs/evaluation.md):

- B1 single LLM: one prompt turns the PRD into a full spec (FRs + NFRs).
  No critics, no questions. Output is untraced by design (trace_kind
  "baseline_b1"); only kind/text are scored.
- B2 orchestrator with LLM critics: draft FRs + the critic set, with EVERY
  finding's suggestion applied as an NFR. No gap check, no human loop.
- B3 pipeline without human input: the real pipeline (gap check + targeted
  questions) run with no answers. Escalated findings stay OPEN placeholders.
- B4 pipeline with ask-everything review: EVERY finding (vague included)
  becomes a question, with no MAX_QUESTIONS cap; the oracle answers all.

Ablations ("each critic removed in turn") use critic_names subsets on the
B2/B3/targeted builders.
"""
from __future__ import annotations

from specpipe.schemas import (Finding, NFR_CATEGORIES, Question,
                              Requirement, Spec, TRACE_CRITIC, TRACE_HUMAN)
from specpipe.llm import _extract_json
from specpipe.orchestrator import draft_frs
from specpipe.critics import Critic
from specpipe.gapcheck import dedupe, select_questions, auto_resolvable
from specpipe.pipeline import finalize

ALL_CRITICS = ("security", "architecture", "performance")

_B1_SYSTEM = """ROLE: baseline_b1
You are a single LLM asked to turn a PRD directly into a complete
specification in one shot. Return JSON only:
{"requirements": [{"kind": "FR|security|performance|scalability|reliability|usability|other",
                   "text": "<requirement sentence>"}]}
Include the functional and non-functional requirements you believe a
complete spec needs, based only on what the PRD states or implies."""


def _norm_kind(kind: str) -> str:
    kind = str(kind or "").strip()
    if kind == "FR":
        return "FR"
    return kind if kind in NFR_CATEGORIES else "other"


def b1_single_llm(prd_text: str, llm, title: str = "Untitled",
                  meta: dict | None = None) -> Spec:
    """B1: one monolithic LLM call, no critics, no human loop."""
    if meta is not None:
        meta["questions_asked"] = 0
        meta["n_findings"] = 0
    raw = llm.complete(_B1_SYSTEM, prd_text)
    data = _extract_json(raw)
    items = data if isinstance(data, list) else data.get("requirements", [])
    reqs = []
    for i, item in enumerate(items, start=1):
        if isinstance(item, str):
            item = {"kind": "FR", "text": item}
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        reqs.append(Requirement(
            id=f"B1-{i}", kind=_norm_kind(item.get("kind")), text=text,
            trace_kind="baseline_b1",  # untraced by design; see docs/evaluation.md
            trace_ref=""))
    return Spec(title=title, requirements=reqs)


def _review_with(prd_text: str, llm, critic_names) -> tuple:
    """Draft FRs and run a configurable critic subset (ablation support)."""
    draft = draft_frs(prd_text, llm)
    findings = []
    for name in critic_names:
        findings.extend(Critic(name, llm).review(prd_text, draft))
    return draft, dedupe(findings)


def _next_nfr_n(reqs: list) -> int:
    """First free NFR number after any NFR-typed draft lines (integration
    fix: draft_frs can now emit NFR-typed PRD lines, so finding ids must
    not collide with them)."""
    return sum(1 for r in reqs if r.kind != "FR") + 1


def _apply_all_findings(draft: list, findings: list, title: str) -> Spec:
    reqs = list(draft)
    for n, f in enumerate(findings, start=_next_nfr_n(reqs)):
        reqs.append(Requirement(
            id=f"NFR-{n}", kind=f.category, text=f.suggestion,
            trace_kind=TRACE_CRITIC, trace_ref=f.critic))
    return Spec(title=title, requirements=reqs)


def b2_orchestrator_critics(prd_text: str, llm, title: str = "Untitled",
                            critic_names=ALL_CRITICS,
                            meta: dict | None = None) -> Spec:
    """B2: orchestrator + critics, every suggestion applied, no human."""
    draft, findings = _review_with(prd_text, llm, critic_names)
    if meta is not None:
        meta["questions_asked"] = 0
        meta["n_findings"] = len(findings)
    return _apply_all_findings(draft, findings, title)


def b3_pipeline_no_human(prd_text: str, llm, title: str = "Untitled",
                         critic_names=ALL_CRITICS,
                         meta: dict | None = None) -> Spec:
    """B3: the full pipeline with targeted questions left unanswered."""
    draft, findings = _review_with(prd_text, llm, critic_names)
    questions = select_questions(findings)
    if meta is not None:
        meta["questions_asked"] = len(questions)
        meta["n_findings"] = len(findings)
    return finalize(title, draft, findings, questions, {})


def b3_targeted_with_oracle(prd_text: str, llm, oracle, title: str = "Untitled",
                            critic_names=ALL_CRITICS,
                            meta: dict | None = None) -> Spec:
    """The H3 'targeted' arm: pipeline questions answered by the oracle."""
    draft, findings = _review_with(prd_text, llm, critic_names)
    questions = select_questions(findings)
    if meta is not None:
        meta["questions_asked"] = len(questions)
        meta["n_findings"] = len(findings)
    answers = {q.id: oracle.answer(q) for q in questions}
    return finalize(title, draft, findings, questions, answers)


def _ask_everything_questions(findings: list) -> list:
    """One question per finding, all kinds, no cap (B4's review-everything)."""
    questions = []
    for i, f in enumerate(findings, start=1):
        if f.kind == "conflict":
            text = (f"{f.issue} - resolving this may affect "
                    f"{f.conflicts_with or 'another requirement'}. "
                    f"What should take priority?")
        elif f.kind == "vague":
            text = f"{f.issue}. What exact requirement should apply?"
        else:
            text = f"{f.issue}. What should the requirement be?"
        questions.append(Question(
            id=f"Q-{i}", text=text,
            context=f"{f.critic} critic: {f.reason}",
            finding_refs=[f.issue]))
    return questions


def b4_ask_everything(prd_text: str, llm, oracle, title: str = "Untitled",
                      critic_names=ALL_CRITICS,
                      meta: dict | None = None) -> Spec:
    """B4: every finding escalated to the (simulated) human and answered."""
    draft, findings = _review_with(prd_text, llm, critic_names)
    questions = _ask_everything_questions(findings)
    if meta is not None:
        meta["questions_asked"] = len(questions)
        meta["n_findings"] = len(findings)
    by_issue = {f.issue: q for f, q in zip(findings, questions)}
    answers = {q.id: oracle.answer(q) for q in questions}
    reqs = list(draft)
    n = _next_nfr_n(reqs) - 1
    for f in findings:
        n += 1
        answer = answers.get(by_issue[f.issue].id, "").strip()
        if answer:
            reqs.append(Requirement(
                id=f"NFR-{n}", kind=f.category,
                text=f"{f.suggestion} (specified: {answer})",
                trace_kind=TRACE_HUMAN, trace_ref=by_issue[f.issue].id))
        else:
            reqs.append(Requirement(
                id=f"NFR-{n}", kind=f.category,
                text=f"OPEN: {f.issue} - {f.suggestion}",
                trace_kind=TRACE_CRITIC, trace_ref=f.critic))
    return Spec(title=title, requirements=reqs)


# Registry used by the harness. Oracle-taking baselines are marked.
BASELINES = {
    "B1": {"fn": b1_single_llm, "oracle": False},
    "B2": {"fn": b2_orchestrator_critics, "oracle": False},
    "B3": {"fn": b3_pipeline_no_human, "oracle": False},
    "B3T": {"fn": b3_targeted_with_oracle, "oracle": True},
    "B4": {"fn": b4_ask_everything, "oracle": True},
}
