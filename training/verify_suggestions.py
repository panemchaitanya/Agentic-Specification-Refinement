"""Human verification of LLM-suggested gold labels (mid-submission flow).

One annotator reviews LLM suggestions per document: accept / reject /
edit-kind / edit-text, then optionally add missed requirements. Output is
one verdict JSON per (annotator, document) under
data/frozen/gold_verdicts/<split>/. Verdicts are the input to
training/merge_verdicts.py, which promotes them to FROZEN gold (with
kappa where a document was verified by more than one annotator).

Stdlib only. Resumable: documents with an existing verdict file for this
annotator are skipped.

Usage:
  python training/verify_suggestions.py --annotator anjali --split dev
  python training/verify_suggestions.py --annotator anjali --split dev \
      --docs pure-0000_-_cctns,pure-1998_-_themas
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "data" / "frozen" / "gold"
SUGG = ROOT / "data" / "frozen" / "gold_llm_suggestions"
TEXT = ROOT / "data" / "frozen" / "pure" / "text"
OUT = ROOT / "data" / "frozen" / "gold_verdicts"

sys.path.insert(0, str(ROOT / "src"))
from specpipe.schemas import NFR_CATEGORIES  # noqa: E402

KINDS = ["FR"] + NFR_CATEGORIES


def ask(prompt: str, valid: set[str]) -> str:
    while True:
        a = input(prompt).strip().lower()
        if a in valid:
            return a
        print(f"  expected one of: {', '.join(sorted(valid))}")


def verify_doc(item_id: str, annotator: str, split: str) -> dict:
    skel = json.loads((GOLD / split / f"{item_id}.gold.json").read_text())
    sugg = json.loads((SUGG / split / f"{item_id}.llm_suggestions.json")
                      .read_text())
    doc_name = Path(skel["prd_path"]).name + ".txt"
    doc = (TEXT / split / doc_name).read_text(encoding="utf-8",
                                              errors="replace")
    print("=" * 72)
    print(f"ITEM {item_id}  ({len(doc)} chars, "
          f"{len(sugg['suggested_requirements'])} suggestions)")
    print("-" * 72)
    print("DOCUMENT EXCERPT (first 2000 chars; full text is the ground):")
    print(doc[:2000])
    print("-" * 72)
    verdicts = []
    for r in sugg["suggested_requirements"]:
        print(f"\n[{r['id']}] proposed kind: {r['kind']}")
        print(f"  text: {r['text']}")
        a = ask("  [a]ccept / [r]eject / [k]ind edit / [t]ext edit / "
                "[s]kip item? ", {"a", "r", "k", "t", "s"})
        v = {"id": r["id"], "decision": a}
        if a == "k":
            print("  kinds:", ", ".join(KINDS))
            nk = ask("  new kind: ", set(KINDS))
            v["new_kind"] = nk
            v["decision"] = "a"
        elif a == "t":
            v["new_text"] = input("  new text: ").strip()
            v["decision"] = "a"
        verdicts.append(v)
    added = []
    while ask("\nAdd a requirement the LLM missed? [y/n] ", {"y", "n"}) == "y":
        print("  kinds:", ", ".join(KINDS))
        k = ask("  kind: ", set(KINDS))
        t = input("  text: ").strip()
        if t:
            added.append({"kind": k, "text": t})
    return {
        "item_id": item_id, "annotator": annotator, "split": split,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "verdicts": verdicts, "added_requirements": added,
        "source_suggestion_file": f"data/frozen/gold_llm_suggestions/"
                                  f"{split}/{item_id}.llm_suggestions.json",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--annotator", required=True)
    ap.add_argument("--split", default="dev", choices=["dev", "test"])
    ap.add_argument("--docs", default="",
                    help="comma-separated item ids; default: full queue")
    args = ap.parse_args()
    out_dir = OUT / args.split
    out_dir.mkdir(parents=True, exist_ok=True)
    queue = sorted(p.stem.replace(".gold", "")
                   for p in (GOLD / args.split).glob("*.gold.json"))
    if args.docs:
        want = set(args.docs.split(","))
        queue = [q for q in queue if q in want]
    todo = [q for q in queue
            if not (out_dir / f"{q}.{args.annotator}.verdict.json").exists()]
    print(f"{len(todo)} documents to verify for {args.annotator} "
          f"(of {len(queue)} in scope)")
    t0 = time.time()
    for i, item_id in enumerate(todo, 1):
        print(f"\n### doc {i}/{len(todo)} - elapsed "
              f"{(time.time()-t0)/60:.0f} min")
        v = verify_doc(item_id, args.annotator, args.split)
        (out_dir / f"{item_id}.{args.annotator}.verdict.json").write_text(
            json.dumps(v, indent=2) + "\n", encoding="utf-8")
        print(f"saved verdict for {item_id}")
    print(f"\nDONE: {len(todo)} verdicts written to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
