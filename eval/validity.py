"""Trace-fidelity measurement: does a claimed trace source actually exist?

The pipeline guarantees every spec line NAMES a source (trace coverage),
but nothing checks that a prd_sentence trace is actually a verbatim quote
from the PRD - a model can hallucinate the citation itself. These pure
functions measure that per run; the pipeline itself does not validate or
repair traces (coordinator decision whether it should).
"""
from __future__ import annotations


def _norm(text: str) -> str:
    return " ".join(str(text).split())


def verbatim_in(source_text: str, quote: str) -> bool:
    """True when quote appears in source_text, whitespace-normalized.

    Strictly verbatim: case-sensitive (the orchestrator contract asks for
    exact quotes; a re-cased quote is not verbatim). Whitespace is
    normalized on both sides so line-wrap artifacts do not count as
    hallucinations."""
    q = _norm(quote)
    return bool(q) and q in _norm(source_text)


def fr_trace_verbatim_rate(requirements, prd_text: str) -> float | None:
    """Share of PRD-traced requirements whose trace_ref is a verbatim PRD
    quote. None when there are no PRD-traced lines."""
    traced = [r for r in requirements
              if getattr(r, "trace_kind", None) == "prd_sentence"]
    if not traced:
        return None
    hits = sum(1 for r in traced if verbatim_in(prd_text, r.trace_ref))
    return round(hits / len(traced), 4)


def finding_trace_verbatim_rate(findings, prd_text: str) -> float | None:
    """Share of critic findings whose prd_sentence pointer is verbatim."""
    if not findings:
        return None
    hits = sum(1 for f in findings if verbatim_in(prd_text, f.prd_sentence))
    return round(hits / len(findings), 4)
