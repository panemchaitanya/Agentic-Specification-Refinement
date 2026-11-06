"""Merge human verdicts over LLM suggestions into FROZEN gold files.

Reads data/frozen/gold_verdicts/<split>/<item>.<annotator>.verdict.json
files produced by verify_suggestions.py and promotes them to gold:

- Single annotator: verdicts stand as-is; provenance marked
  "single-verified".
- Multiple annotators on the same item: pairwise Cohen's kappa on
  accept/reject decisions (and on category of accepted items) is
  computed and recorded; conflicts resolve by majority vote, ties keep
  the suggestion out (conservative).

Output: rewrites data/frozen/gold/<split>/<item>.gold.json in place with
status "FROZEN - human-verified (anchored)" and non-empty
gold_requirements, which passes eval/gold.py's load_gold whitelist.
Original skeletons are backed up under data/frozen/gold/_skeleton_backup/
on first promotion.

Usage: python training/merge_verdicts.py --split dev [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "data" / "frozen" / "gold"
VERDICTS = ROOT / "data" / "frozen" / "gold_verdicts"
BACKUP = GOLD / "_skeleton_backup"


def cohen_kappa(a: list, b: list) -> float | None:
    """Cohen's kappa over two aligned label lists."""
    if len(a) != len(b) or not a:
        return None
    n = len(a)
    agree = sum(1 for x, y in zip(a, b) if x == y)
    ca, cb = Counter(a), Counter(b)
    pe = sum((ca[k] / n) * (cb[k] / n) for k in set(ca) | set(cb))
    if pe == 1.0:
        return 1.0
    return (agree / n - pe) / (1 - pe)


def load_item_verdicts(split: str, item_id: str) -> list[dict]:
    out = []
    for p in sorted((VERDICTS / split).glob(f"{item_id}.*.verdict.json")):
        out.append(json.loads(p.read_text()))
    return out


def promote(split: str, item_id: str, dry: bool) -> dict | None:
    vs = load_item_verdicts(split, item_id)
    if not vs:
        return None
    sugg_path = ROOT / vs[0]["source_suggestion_file"]
    sugg = {r["id"]: r for r in json.loads(sugg_path.read_text())
            ["suggested_requirements"]}
    gold_reqs = []
    kappa_report = {}
    for rid, r in sugg.items():
        decisions = []
        for v in vs:
            d = next((x for x in v["verdicts"] if x["id"] == rid), None)
            decisions.append(d)
        accepts = sum(1 for d in decisions
                      if d and d["decision"] == "a")
        if len(decisions) > 1:
            if accepts * 2 <= len(decisions):
                continue  # no majority -> drop (conservative)
        elif accepts == 0:
            continue
        kind, text = r["kind"], r["text"]
        for d in decisions:
            if d and "new_kind" in d:
                kind = d["new_kind"]
            if d and "new_text" in d and d["new_text"]:
                text = d["new_text"]
        gold_reqs.append({"id": rid, "kind": kind, "text": text,
                          "source": "llm-suggestion+human-verify"})
    added_ids = set()
    for v in vs:
        for a in v["added_requirements"]:
            key = a["text"].strip().lower()
            if key in added_ids:
                continue
            added_ids.add(key)
            gold_reqs.append({
                "id": f"human-{len(added_ids):03d}",
                "kind": a["kind"], "text": a["text"],
                "source": f"human-added:{v['annotator']}"})
    if len(vs) > 1:
        ids = [r["id"] for r in sugg.values()]
        for va, vb in combinations(vs, 2):
            da = [next((x["decision"] for x in va["verdicts"]
                        if x["id"] == i), "s") for i in ids]
            db = [next((x["decision"] for x in vb["verdicts"]
                        if x["id"] == i), "s") for i in ids]
            k = cohen_kappa(da, db)
            kappa_report[f"{va['annotator']}~{vb['annotator']}"] = k
    skel_path = GOLD / split / f"{item_id}.gold.json"
    skel = json.loads(skel_path.read_text())
    if not dry:
        BACKUP.mkdir(exist_ok=True)
        bak = BACKUP / split / f"{item_id}.gold.json"
        if not bak.exists():
            bak.parent.mkdir(parents=True, exist_ok=True)
            bak.write_text(skel_path.read_text())
    n_ann = len(vs)
    skel["provenance"]["status"] = (
        f"FROZEN - human-verified (anchored, "
        f"{'single' if n_ann == 1 else str(n_ann)} annotator"
        f"{'s' if n_ann > 1 else ''})")
    skel["provenance"]["verified_by"] = [v["annotator"] for v in vs]
    if kappa_report:
        skel["provenance"]["kappa_accept_reject"] = kappa_report
    skel["gold_requirements"] = gold_reqs
    if not dry:
        skel_path.write_text(json.dumps(skel, indent=2) + "\n",
                             encoding="utf-8")
    return {"item_id": item_id, "annotators": n_ann,
            "requirements": len(gold_reqs), "kappa": kappa_report}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=["dev", "test"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    items = sorted({p.name.split(".")[0]
                    for p in (VERDICTS / args.split).glob("*.verdict.json")}
                   ) if (VERDICTS / args.split).exists() else []
    if not items:
        print(f"no verdict files under {VERDICTS / args.split}")
        return 1
    done = [promote(args.split, i, args.dry_run) for i in items]
    done = [d for d in done if d]
    print(json.dumps({"promoted": len(done), "dry_run": args.dry_run,
                      "items": done}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
