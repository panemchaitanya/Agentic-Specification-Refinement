"""Leak-proof Frozen Oracle Simulator for gap-injection elicitation benchmarks."""

from __future__ import annotations

import re

from reqhq.benchmark.models import (
    GapClause,
    GoldGapManifest,
    OracleAnswer,
    OracleEvalMetrics,
)

NOT_SPECIFIED_REPLY = "Not specified in ground truth"

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_WHITESPACE_RE = re.compile(r"\s+")

_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "about",
        "all",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "can",
        "clause",
        "clauses",
        "could",
        "did",
        "do",
        "does",
        "each",
        "else",
        "every",
        "for",
        "from",
        "gap",
        "gaps",
        "had",
        "has",
        "have",
        "how",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "may",
        "might",
        "must",
        "no",
        "not",
        "of",
        "on",
        "onto",
        "or",
        "our",
        "per",
        "please",
        "requirement",
        "requirements",
        "shall",
        "should",
        "spec",
        "specification",
        "system",
        "tell",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "will",
        "with",
        "would",
        "your",
    }
)


def _normalize_lower(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip().lower()


def _content_tokens(text: str, *, min_len: int = 4) -> set[str]:
    return {
        tok
        for tok in _TOKEN_RE.findall(text.lower())
        if len(tok) >= min_len and tok not in _STOPWORDS
    }


def _phrase_matches(phrase: str, question_norm: str) -> bool:
    clean = _normalize_lower(phrase)
    pattern = r"(?<![a-z0-9])" + re.escape(clean) + r"(?![a-z0-9])"
    return re.search(pattern, question_norm) is not None


def _clause_matches_question(clause: GapClause, question_norm: str, question_tokens: set[str]) -> bool:
    """Return True iff question directly targets this hidden clause without guessing."""
    if _phrase_matches(clause.clause_id, question_norm):
        return True

    for keyword in clause.keywords:
        if _phrase_matches(keyword, question_norm):
            return True

    clause_tokens = _content_tokens(clause.text, min_len=4)
    if not clause_tokens:
        return False

    overlap = question_tokens & clause_tokens
    if len(clause_tokens) == 1:
        return len(overlap) == 1
    return len(overlap) >= 2


class FrozenOracleSimulator:
    """Answers only targeted questions matching hidden stripped clauses; never leaks unasked facts."""

    def __init__(self, manifest: GoldGapManifest) -> None:
        self.manifest = manifest
        self._revealed_clause_ids: set[str] = set()
        self._history: list[OracleAnswer] = []
        self._convergence_question_index: int | None = None

    @property
    def history(self) -> list[OracleAnswer]:
        """Return a shallow copy of the recorded question-answer history."""
        return list(self._history)

    @property
    def revealed_clause_ids(self) -> frozenset[str]:
        """Return the set of stripped clause IDs uncovered so far."""
        return frozenset(self._revealed_clause_ids)

    def answer_question(self, question: str) -> OracleAnswer:
        """Answer a single elicitation question strictly from matching stripped clauses."""
        clean_question = question.strip()
        if not clean_question:
            raise ValueError("question must not be blank")

        q_norm = _normalize_lower(clean_question)
        q_tokens = _content_tokens(clean_question, min_len=4)

        matched: list[GapClause] = [
            clause
            for clause in self.manifest.stripped_clauses
            if _clause_matches_question(clause, q_norm, q_tokens)
        ]

        if not matched:
            answer = OracleAnswer(
                question=clean_question,
                answer=NOT_SPECIFIED_REPLY,
                matched_clause_ids=[],
                is_redundant=False,
                found_in_ground_truth=False,
            )
            self._history.append(answer)
            return answer

        matched_ids = [clause.clause_id for clause in matched]
        is_redundant = all(cid in self._revealed_clause_ids for cid in matched_ids)
        reply_text = "\n".join(f"[{clause.clause_id}] {clause.text}" for clause in matched)

        self._revealed_clause_ids.update(matched_ids)
        answer = OracleAnswer(
            question=clean_question,
            answer=reply_text,
            matched_clause_ids=matched_ids,
            is_redundant=is_redundant,
            found_in_ground_truth=True,
        )
        self._history.append(answer)

        total_gaps = len(self.manifest.stripped_clauses)
        if (
            self._convergence_question_index is None
            and total_gaps > 0
            and len(self._revealed_clause_ids) == total_gaps
        ):
            self._convergence_question_index = len(self._history)

        return answer

    def score_session(self) -> OracleEvalMetrics:
        """Compute exact recall, precision, F1, convergence turn, and redundancy rate."""
        total_gaps = len(self.manifest.stripped_clauses)
        matched_gaps = len(self._revealed_clause_ids)
        total_questions = len(self._history)

        gap_recall = (matched_gaps / total_gaps) if total_gaps > 0 else 0.0
        useful_questions = sum(
            1 for entry in self._history if entry.found_in_ground_truth and not entry.is_redundant
        )
        precision = (useful_questions / total_questions) if total_questions > 0 else 0.0
        f1 = (
            (2.0 * precision * gap_recall) / (precision + gap_recall)
            if (precision + gap_recall) > 0.0
            else 0.0
        )
        redundant_count = sum(1 for entry in self._history if entry.is_redundant)
        redundant_rate = (redundant_count / total_questions) if total_questions > 0 else 0.0

        questions_to_convergence = (
            self._convergence_question_index
            if self._convergence_question_index is not None
            else total_questions
        )

        return OracleEvalMetrics(
            gap_recall=gap_recall,
            precision=precision,
            f1=f1,
            questions_to_convergence=questions_to_convergence,
            redundant_question_rate=redundant_rate,
            total_questions=total_questions,
            matched_gaps=matched_gaps,
            total_gaps=total_gaps,
        )


__all__ = ["FrozenOracleSimulator", "NOT_SPECIFIED_REPLY"]
