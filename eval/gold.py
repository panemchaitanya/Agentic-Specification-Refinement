"""Loaders and validators for ground-truth and match-verdict files.

File formats are fixed here so the datasets lane and the two human
extractors know exactly what to produce. Both files are JSON; see
docs/evaluation.md for the full format description and the freeze rules.

Gold file (one per PRD item, produced by the datasets lane from the two
independent extractions + adjudication, with Cohen's kappa reported in its
own provenance block). Freeze layout (agreed with the datasets lane):
data/frozen/pairs/{dev,test}/<item>.prd.txt + <item>.spec.txt, with the
gold JSON alongside as <item>.gold.json:
{
  "item_id": "kep-NNNN",
  "prd_path": "data/frozen/pairs/test/kep-NNNN.prd.txt",
  "implied_categories": ["security", "performance"],
  "gold_requirements": [{"id": "G1", "kind": "FR|security|...", "text": "..."}],
  "provenance": {"extractor_agreement_kappa": 0.0, "freeze_id": "...",
                 "status": "FROZEN"}
}

Fail-closed rules: an empty gold_requirements list is rejected (a skeleton
must never be consumed as gold), and when provenance.status is present it
must start with "FROZEN" - anything else ("SKELETON - ...", partial, or
any future non-frozen marker) is rejected. Whitelist, not blacklist, so
unknown future statuses fail closed. Only extracted, adjudicated, frozen
gold loads.

Scope gate (anchored-verification flow): when provenance carries a
review_scope block, reviewer_read_full_llm_scope must be true - gold
verified against only part of the reviewed source is incomplete and is
rejected, not merely flagged. Gold produced outside that flow (pairs,
synthetic) has no review_scope and loads normally.

Extended provenance contract (anchored flow, merge_verdicts.py):
  verified_by: [annotator names]
  kappa_accept_reject: {pair: kappa} - ANCHORED verification agreement on
      the shared subsample (not the proposal's independent-extraction
      kappa; reported as such, see docs/evaluation.md D4)
  review_scope: {doc_chars_total, llm_scope_chars,
      chars_presented_to_reviewer, reviewer_read_full_llm_scope}
  implied_categories_verified: true - required for H3 completeness
      eligibility (eval/scoring.py::h3_eligible)
  excluded: {"skipped": [...], "no_majority": [...], "rejected": [...]} -
      suggestions not promoted to gold. "rejected" entries are
      affirmative not-a-requirement judgments: they stay out of gold and
      a prediction matching one still counts as a false positive. Every
      other class ("skipped", "no_majority", contested-edit records) is
      an UNRESOLVED judgment: its presence makes the whole document
      ineligible for scoring - gold_doc_scorable() returns False and
      eval/summarize.py::score_run refuses the document (fail-closed,
      rubric rule R5). No partial-gold PRF is computed.

Verdict file (one per scored item+run, produced by the rubric process):
{
  "item_id": "kep-NNNN", "run_id": "...",
  "pred_ids": ["FR-1", "NFR-1", ...],   # ids of the predicted set matched against
  "matches": [["G1", "NFR-2"], ...],
  "audited": false
}
pred_ids is required for scoring (eval/summarize.py::score_run): PRF needs
the full predicted set, not only the matched ones.
"""
from __future__ import annotations

import json

from specpipe.schemas import NFR_CATEGORIES

_KINDS = set(NFR_CATEGORIES) | {"FR"}


def load_gold(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        gold = json.load(fh)
    for key in ("item_id", "gold_requirements", "implied_categories"):
        if key not in gold:
            raise ValueError(f"gold file {path} missing {key!r}")
    status = (gold.get("provenance") or {}).get("status")
    if status is not None and not str(status).startswith("FROZEN"):
        raise ValueError(
            f"gold file {path} has provenance.status {status!r} - only "
            "'FROZEN...' statuses may be consumed in a run; refusing to "
            "load (fail-closed whitelist)")
    if not gold["gold_requirements"]:
        raise ValueError(
            f"gold file {path} has an empty gold_requirements list - an "
            "unfilled skeleton is not valid gold")
    scope = (gold.get("provenance") or {}).get("review_scope")
    if scope is not None and not scope.get("reviewer_read_full_llm_scope"):
        raise ValueError(
            f"gold file {path} has incomplete review scope "
            f"({scope.get('chars_presented_to_reviewer')}/"
            f"{scope.get('llm_scope_chars')} chars reviewed) - partial "
            "review is not complete gold; refusing to load")
    ids = set()
    for r in gold["gold_requirements"]:
        if r.get("kind") not in _KINDS:
            raise ValueError(f"gold {gold['item_id']}: bad kind {r.get('kind')!r}")
        if not str(r.get("text", "")).strip():
            raise ValueError(f"gold {gold['item_id']}: empty text on {r.get('id')!r}")
        if r["id"] in ids:
            raise ValueError(f"gold {gold['item_id']}: duplicate id {r['id']!r}")
        ids.add(r["id"])
    bad = set(gold["implied_categories"]) - set(NFR_CATEGORIES)
    if bad:
        raise ValueError(f"gold {gold['item_id']}: unknown categories {sorted(bad)}")
    return gold


def gold_doc_scorable(gold: dict) -> tuple:
    """(scorable: bool, reason: str) for one loaded gold document.

    Fail-closed document eligibility (rubric R5): if the gold file's
    provenance.excluded records any UNRESOLVED suggestion - any class
    other than "rejected" with at least one entry - the document is not
    scored at all, because the remaining gold is provably incomplete and
    PRF against it would mislabel unknowns as false positives/negatives.
    "rejected" exclusions do not affect scorability: they are resolved
    judgments, and matching predictions count against precision normally.
    """
    excluded = (gold.get("provenance") or {}).get("excluded") or {}
    unresolved = {cls: ids for cls, ids in excluded.items()
                  if cls != "rejected" and ids}
    if unresolved:
        detail = ", ".join(f"{cls}={len(ids)}" for cls, ids
                           in sorted(unresolved.items()))
        return False, (f"unresolved exclusions ({detail}); document "
                       "ineligible for scoring under rubric R5")
    return True, ""


def load_verdicts(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        v = json.load(fh)
    for key in ("item_id", "run_id", "matches"):
        if key not in v:
            raise ValueError(f"verdict file {path} missing {key!r}")
    for pair in v["matches"]:
        if not (isinstance(pair, list) and len(pair) == 2):
            raise ValueError(f"verdict {v['item_id']}: bad match pair {pair!r}")
    return v

