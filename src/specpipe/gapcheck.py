"""Gap and conflict check: decides which findings reach the human.

Per the proposal, a question goes to the human only when a critic reports
missing business context (missing_context) or when two critics disagree
(conflict). Vague-but-fixable findings become NFRs directly.
"""
from __future__ import annotations

from .schemas import Finding, Question

MAX_QUESTIONS = 5

# Cross-critic conflict detection (proposal: "two critics disagree", e.g.
# an encryption requirement that breaks a latency target). Bounded and
# deterministic on purpose: a suggestion pair from DIFFERENT critics is
# flagged only when (a) the two suggestions contain opposing directive
# keywords from the table below AND (b) they share a target - the same
# PRD sentence or a common content word (>=5 chars). Low recall by
# design: it catches explicit tension, it cannot prove none exists.
# Recorded as deviation D2 in docs/evaluation.md.
CONFLICT_KEYWORD_PAIRS = [
    ({"encrypt", "encryption", "encrypted", "hash", "hashing"},
     {"latency", "fast", "p95", "p99", "milliseconds", "real-time",
      "realtime", "throughput"}),
    ({"replicate", "replication", "redundant", "redundancy"},
     {"single copy", "minimal storage", "storage cost"}),
]

_STOP = {"shall", "should", "must", "system", "requirement", "within",
         "every", "after", "before", "their", "there", "which", "about"}


def _content_words(text: str) -> set:
    import re
    return {w for w in re.findall(r"[a-z]+", text.lower())
            if len(w) >= 5 and w not in _STOP}


def _has_opposing_keywords(a: str, b: str) -> bool:
    la, lb = a.lower(), b.lower()
    for set_a, set_b in CONFLICT_KEYWORD_PAIRS:
        if ((any(k in la for k in set_a) and any(k in lb for k in set_b))
                or (any(k in lb for k in set_a) and any(k in la for k in set_b))):
            return True
    return False


def detect_cross_critic_conflicts(findings: list) -> list:
    """Flag plausible cross-critic contradictions (kind="conflict").

    Returns a new list; inputs are not mutated. A finding can conflict
    with at most one other (first match wins). Findings already marked
    conflict by their own critic are left untouched.
    """
    import copy
    out = [copy.copy(f) for f in findings]
    flagged = set()
    for i in range(len(out)):
        if i in flagged or out[i].kind == "conflict":
            continue
        for j in range(i + 1, len(out)):
            if j in flagged or out[j].kind == "conflict":
                continue
            if out[i].critic == out[j].critic:
                continue
            if not _has_opposing_keywords(out[i].suggestion, out[j].suggestion):
                continue
            same_sentence = (out[i].prd_sentence
                             and out[i].prd_sentence == out[j].prd_sentence)
            shared_target = bool(
                _content_words(out[i].suggestion + " " + out[i].issue)
                & _content_words(out[j].suggestion + " " + out[j].issue))
            if not (same_sentence or shared_target):
                continue
            out[i].kind = "conflict"
            out[i].conflicts_with = out[j].critic
            out[j].kind = "conflict"
            out[j].conflicts_with = out[i].critic
            flagged.update((i, j))
            break
    return out


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
