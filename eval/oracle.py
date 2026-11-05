"""Simulated human oracle (proposal section 4): answers from the reference
spec only, so trials are repeatable.

Leakage guard (proposal risk register / plan): the oracle answers ONLY the
question asked, every answer is logged with the reference lines it drew
on, and the log records the verbatim-overlap ratio of each answer against
its support lines. This deterministic oracle is extractive, so it is a
worst-case (upper bound) for phrasing leakage into the final spec; the
log makes that leakage measurable and auditable on a sample, and the
limitation is reported with H3 results.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

_STOP = {"what", "should", "the", "this", "that", "with", "from", "which",
         "when", "requirement", "apply", "priority", "resolving", "affect",
         "another", "take"}


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower())
            if len(w) >= 4 and w not in _STOP}


@dataclass
class OracleLogEntry:
    question_id: str
    question_text: str
    answer: str
    support_ids: list
    verbatim_overlap: float  # jaccard(answer words, support-line words)

    def to_dict(self) -> dict:
        return asdict(self)


class ReferenceOracle:
    """Answers targeted questions from reference-spec lines.

    reference: list of dicts with at least {"id", "text"}. An answer is
    the single highest-overlap reference line (extractive). When nothing
    overlaps, the oracle honestly has no information and returns "".
    """

    def __init__(self, reference: list):
        self.reference = [dict(r) for r in reference]
        self._word_index = [(r["id"], _words(r["text"]), r["text"])
                            for r in self.reference]
        self.log: list = []

    def answer(self, question) -> str:
        q_words = _words(question.text)
        best_id, best_text, best_score = None, "", 0
        for rid, r_words, r_text in self._word_index:
            score = len(q_words & r_words)
            if score > best_score:
                best_id, best_text, best_score = rid, r_text, score
        if best_id is None:
            answer, support, overlap = "", [], 0.0
        else:
            answer = best_text  # extractive: bounded, logged leakage
            support = [best_id]
            a_words = _words(answer)
            union = a_words | _words(best_text)
            overlap = len(a_words & _words(best_text)) / len(union) if union else 0.0
        self.log.append(OracleLogEntry(
            question_id=question.id, question_text=question.text,
            answer=answer, support_ids=support, verbatim_overlap=overlap))
        return answer

    def audit_report(self) -> dict:
        """Leakage audit: per-answer overlap plus the share of answers that
        copy reference phrasing verbatim (overlap == 1.0)."""
        n = len(self.log)
        verbatim = sum(1 for e in self.log if e.verbatim_overlap >= 0.999)
        return {
            "answers": n,
            "verbatim_copies": verbatim,
            "verbatim_share": (verbatim / n) if n else 0.0,
            "entries": [e.to_dict() for e in self.log],
        }
