"""Create gold-file skeletons for the frozen evaluation items.

One skeleton per item, in the exact `eval/gold.py` format, so the two
independent extractors (docs/extraction-guidelines.md) fill in
`gold_requirements` and `implied_categories`, and adjudication records
`provenance.extractor_agreement_kappa`. Skeletons are NOT gold: they
carry `provenance.status = "SKELETON - ..."`, and the evaluation lane
must not consume them until requirements are filled and kappa recorded
(a skeleton with empty gold_requirements is a placeholder, never ground
truth).

Status convention (ruled by the evaluation lane, 6 Nov 2026): load_gold
whitelists - a gold file's provenance.status must START WITH "FROZEN"
when present; anything else (this skeleton's sentence, bare "SKELETON",
"PARTIAL", unknown future statuses) is rejected, with non-empty
gold_requirements as the second layer. When the two-extractor pass +
adjudication completes, flip the status to e.g.
"FROZEN - two-extractor pass + adjudication complete" and record kappa.

Sources of items:
  --pairs   validated pair records from data/frozen/pairs/pilot_sample.json
            (only pairs whose validation entry has checks_passed=true)
  --pure    documents from data/frozen/pure/FREEZE.json (split selectable)

Output: data/frozen/gold/<split>/<item_id>.gold.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / "data" / "frozen"
OUT = FROZEN / "gold"


def skeleton(item_id: str, prd_path: str, freeze_id: str) -> dict:
    return {
        "item_id": item_id,
        "prd_path": prd_path,
        "implied_categories": [],
        "gold_requirements": [],
        "provenance": {
            "extractor_agreement_kappa": None,
            "freeze_id": freeze_id,
            "status": "SKELETON - not gold until two-extractor pass + "
                      "adjudication + kappa are recorded",
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", action="store_true")
    ap.add_argument("--pure", action="store_true")
    ap.add_argument("--split", choices=["dev", "test"], default="test")
    args = ap.parse_args()
    if not (args.pairs or args.pure):
        ap.error("choose --pairs and/or --pure")

    written = 0
    if args.pairs:
        pilot = json.loads((FROZEN / "pairs" / "pilot_sample.json").read_text())
        validation = pilot.get("validation", {})
        for rec in pilot["pairs"]:
            key = f"{rec['source']}:{rec['id']}"
            if not validation.get(key, {}).get("checks_passed"):
                continue  # unvalidated candidates never get skeletons
            item_id = f"{rec['source']}-{rec['id']}"
            prd_path = f"data/frozen/pairs/{args.split}/{item_id}.prd.txt"
            dest = OUT / args.split / f"{item_id}.gold.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(
                skeleton(item_id, prd_path, "pairs/pilot_sample.json"),
                indent=2) + "\n")
            written += 1
    if args.pure:
        freeze = json.loads((FROZEN / "pure" / "FREEZE.json").read_text())
        for doc in freeze["documents"]:
            if doc["split"] != args.split:
                continue
            stem = Path(doc["document"]).stem.replace(" ", "_")
            item_id = f"pure-{stem}"
            prd_path = f"data/raw/pure/extracted/{doc['document']}"
            dest = OUT / args.split / f"{item_id}.gold.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            sk = skeleton(item_id, prd_path, "pure/FREEZE.json")
            sk["provenance"]["source_document_sha256"] = doc["sha256"]
            sk["provenance"]["note"] = (
                "prd_path points at the raw document (re-fetch via "
                "training/fetch_datasets.py); pdf/doc/rtf need text "
                "extraction before labelling")
            dest.write_text(json.dumps(sk, indent=2) + "\n")
            written += 1
    print(f"wrote {written} skeleton(s) under {OUT}/{args.split}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
