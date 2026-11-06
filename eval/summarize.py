"""Aggregate run logs into per-baseline tables and scored result tables.

Two levels, kept separate on purpose:

1. Generation summaries (always available): means over repeats of counts,
   OPEN rate, questions, completeness, LLM cost. These describe behavior,
   not quality.
2. Scored tables (only when frozen gold + rubric verdicts exist): per-item
   PRF from verdicts, ready for the paired readouts in eval/scoring.py.
"""
from __future__ import annotations

import json
from pathlib import Path

from .gold import gold_doc_scorable
from .scoring import score_item


def load_run_log(path: str) -> tuple:
    """Return (header, [run records]) from a harness JSONL log."""
    header, runs = None, []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec.get("type") == "header":
            header = rec
        elif rec.get("type") == "run":
            runs.append(rec)
    if header is None:
        raise ValueError(f"{path} has no header line")
    return header, runs


def summarize_runs(runs: list) -> dict:
    """Mean of numeric fields per (baseline, item) across repeats."""
    groups: dict = {}
    for r in runs:
        groups.setdefault((r["baseline"], r["item_id"]), []).append(r)
    out = {}
    for (baseline, item), recs in sorted(groups.items()):
        def mean(key, default=0.0):
            vals = [r[key] for r in recs if key in r]
            return sum(vals) / len(vals) if vals else default
        out[f"{baseline}/{item}"] = {
            "repeats": len(recs),
            "n_requirements": round(mean("n_requirements"), 3),
            "n_fr": round(mean("n_fr"), 3),
            "n_nfr": round(mean("n_nfr"), 3),
            "open_rate": round(mean("open_rate"), 4),
            "questions_asked": round(mean("questions_asked"), 3),
            "completeness": (round(mean("completeness"), 4)
                             if any("completeness" in r for r in recs) else None),
            "llm_calls": round(mean("calls", 0.0) if recs and "llm" in recs[0]
                               else 0.0, 3),
        }
    # llm sub-dict handled separately for clarity
    for (baseline, item), recs in groups.items():
        key = f"{baseline}/{item}"
        lat = [r["llm"]["total_latency_s"] for r in recs if "llm" in r]
        chars = [r["llm"]["total_chars_in"] + r["llm"]["total_chars_out"]
                 for r in recs if "llm" in r]
        calls = [r["llm"]["calls"] for r in recs if "llm" in r]
        out[key]["mean_llm_calls"] = (round(sum(calls) / len(calls), 3)
                                      if calls else None)
        out[key]["mean_latency_s"] = round(sum(lat) / len(lat), 6) if lat else None
        out[key]["mean_chars_total"] = (round(sum(chars) / len(chars), 1)
                                        if chars else None)
    return out


def score_run(runs: list, gold: dict, verdicts: list) -> dict:
    """Per-item PRF for one run's records against gold + verdicts.

    verdicts: list of verdict dicts (see eval/gold.py) for this run.
    Returns {item_id: ItemScore-as-dict}. Items without verdicts are
    skipped (reported separately by the caller). Raises ValueError when
    the gold document is not scorable (unresolved exclusions, R5).
    """
    scorable, reason = gold_doc_scorable(gold)
    if not scorable:
        raise ValueError(
            f"gold document {gold.get('item_id')!r} refused: {reason}. "
            "Fail-closed: no partial-gold PRF is computed; the caller "
            "must report this document as excluded from scoring.")
    gold_ids = [r["id"] for r in gold["gold_requirements"]]
    by_item = {}
    for v in verdicts:
        item_id = v["item_id"]
        recs = [r for r in runs if r["item_id"] == item_id]
        if not recs:
            continue
        pred_ids = v.get("pred_ids")
        if pred_ids is None:
            raise ValueError(
                f"verdict for {item_id} lacks pred_ids; the rubric process "
                "must record the predicted ids it matched against")
        s = score_item(gold_ids, pred_ids, v["matches"])
        by_item[item_id] = {
            "precision": s.prf.precision, "recall": s.prf.recall,
            "f1": s.prf.f1, "n_gold": s.n_gold, "n_pred": s.n_pred,
            "n_matches": s.n_matches, "many_to_one": s.many_to_one,
            "audited": bool(v.get("audited", False)),
        }
    return by_item

