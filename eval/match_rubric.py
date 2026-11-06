"""LLM rubric matcher: propose gold<->prediction matches for one run log.

For every run record in a harness log written with
persist_requirements=true, asks the LLM to judge which predicted
requirements state the SAME requirement as which gold entry (paraphrase
counts; different scope/precision does not). CONSERVATIVE: the prompt
forbids guessing, and any pair the parser cannot validate against the
actual id sets is dropped and counted, never kept.

Output: one verdict file per (item, baseline) under
eval/verdicts/<run_id>/<item_id>.<baseline>.json in the eval/gold.py
verdict format (pred_ids required for PRF; audited=false - the 20%
hand-audit in eval/audit.py samples these later). Documents that fail
load_gold / gold_doc_scorable are REFUSED with the reason recorded in
the summary, never scored (fail-closed R5).

This produces rubric verdicts for PRF scoring; it is a judged matcher,
not ground truth - the audit step and docs/evaluation.md section 10
labels apply. Usage:
    python eval/match_rubric.py <run_log.jsonl> [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from eval.gold import gold_doc_scorable, load_gold
from eval.summarize import load_run_log
from specpipe.llm import GeminiLLM, LLMError

_ROOT = Path(__file__).parent.parent
GOLD_DEV = _ROOT / "data" / "frozen" / "gold" / "dev"

_SYSTEM = """You are a strict requirements-matching judge. You are given GOLD
requirements (human-verified) and PREDICTED requirements (model output)
from the same source document. Reply with ONLY a JSON array of
[gold_id, pred_id] pairs where the two entries state the SAME
requirement (paraphrases count; a looser, stronger, or merely related
statement does NOT). A match is valid only when both entries have the
SAME kind/class: FR matches FR, and an NFR category matches only the
same category (shown in brackets). One gold entry matches at most one
prediction and vice versa. When unsure, do NOT match. No prose, only
the JSON array."""


def parse_pairs(raw: str, gold_ids: set, pred_ids: set,
                gold_kinds: dict | None = None,
                pred_kinds: dict | None = None) -> tuple:
    """Validated (pairs, dropped_count). Anything unverifiable drops.

    gold_kinds/pred_kinds (id -> kind/class) enforce the rubric's
    same-kind constraint in the parser, not only in the prompt: a
    cross-kind proposal is invalid and counted in dropped, never kept.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [], -1  # unparseable: caller records an error
    pairs, dropped = [], 0
    seen_g, seen_p = set(), set()
    if not isinstance(data, list):
        return [], -1
    for entry in data:
        if not (isinstance(entry, list) and len(entry) == 2
                and entry[0] in gold_ids and entry[1] in pred_ids
                and entry[0] not in seen_g and entry[1] not in seen_p):
            dropped += 1
            continue
        if (gold_kinds is not None and pred_kinds is not None
                and gold_kinds[entry[0]] != pred_kinds[entry[1]]):
            dropped += 1  # cross-kind proposal: invalid, never kept
            continue
        pairs.append([entry[0], entry[1]])
        seen_g.add(entry[0])
        seen_p.add(entry[1])
    return pairs, dropped


def build_prompt(gold_reqs: list, pred_reqs: list) -> str:
    def fmt(reqs):
        return "\n".join(f"{r['id']} [{r['kind']}]: {r['text']}"
                         for r in reqs)
    return (f"GOLD:\n{fmt(gold_reqs)}\n\nPREDICTED:\n{fmt(pred_reqs)}\n\n"
            "JSON array of [gold_id, pred_id] same-requirement pairs:")


def match_run_log(log_path: str, llm, dry: bool = False) -> dict:
    header, runs = load_run_log(log_path)
    run_id = header["run_id"]
    out_dir = _ROOT / "eval" / "verdicts" / run_id
    summary = {"run_id": run_id, "verdicts": 0, "refused": [],
               "errors": [], "dropped_pairs": 0, "unmatched_preds": 0}
    for rec in runs:
        item_id, baseline = rec["item_id"], rec["baseline"]
        if not rec.get("requirements"):
            summary["errors"].append(
                f"{item_id}/{baseline}: no persisted requirements "
                "(rerun with persist_requirements=true)")
            continue
        gold_path = GOLD_DEV / f"{item_id}.gold.json"
        try:
            gold = load_gold(str(gold_path))
        except (ValueError, FileNotFoundError) as exc:
            summary["refused"].append(f"{item_id}: {exc}")
            continue
        ok, reason = gold_doc_scorable(gold)
        if not ok:
            summary["refused"].append(f"{item_id}: {reason}")
            continue
        preds = rec["requirements"]
        pred_ids = {p["id"] for p in preds}
        pred_kinds = {p["id"]: p["kind"] for p in preds}
        gold_reqs = gold["gold_requirements"]
        gold_ids = {g["id"] for g in gold_reqs}
        gold_kinds = {g["id"]: g["kind"] for g in gold_reqs}
        if dry:
            continue
        try:
            raw = llm.complete(_SYSTEM, build_prompt(gold_reqs, preds))
        except LLMError as exc:
            summary["errors"].append(f"{item_id}/{baseline}: {exc}")
            continue
        pairs, dropped = parse_pairs(raw, gold_ids, pred_ids,
                                     gold_kinds, pred_kinds)
        if dropped == -1:
            summary["errors"].append(
                f"{item_id}/{baseline}: unparseable rubric output")
            continue
        summary["dropped_pairs"] += dropped
        matched_preds = {p for _, p in pairs}
        summary["unmatched_preds"] += len(pred_ids - matched_preds)
        verdict = {"item_id": item_id,
                   "run_id": f"{run_id}:{baseline}",
                   "pred_ids": sorted(pred_ids),
                   "matches": pairs, "audited": False,
                   "dropped_pairs": dropped,
                   "matcher": {"type": "llm-rubric",
                               "model": getattr(llm, "model", "unknown"),
                               "matched_at":
                                   f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"}}
        if not dry:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{item_id}.{baseline}.json").write_text(
                json.dumps(verdict, indent=2) + "\n", encoding="utf-8")
        summary["verdicts"] += 1
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_log")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    summary = match_run_log(args.run_log, GeminiLLM(), dry=args.dry_run)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
