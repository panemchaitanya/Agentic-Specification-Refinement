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
must never be consumed as gold), and provenance.status == "SKELETON" is
rejected outright. Only extracted, adjudicated gold loads.

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
    if status == "SKELETON":
        raise ValueError(
            f"gold file {path} is a SKELETON (provenance.status) - not "
            "extracted gold; refusing to consume it in a run")
    if not gold["gold_requirements"]:
        raise ValueError(
            f"gold file {path} has an empty gold_requirements list - an "
            "unfilled skeleton is not valid gold")
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
