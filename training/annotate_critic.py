"""Annotation CLI for contextual critic supervision (Job 2).

Appends one JSONL record per labelled finding to
training/labels/critic_supervision.<annotator>.jsonl and validates each
record against the Finding contract (src/specpipe/schemas.py):
critic/category/kind enums, non-empty issue/suggestion/reason, and a
prd_sentence that is a VERBATIM substring of the item's PRD text.

Two annotators label the same items into separate files; kappa is
computed over (critic, category, kind) agreement per item before
adjudication - see training/ANNOTATION_GUIDELINES.md.

Usage:
  python training/annotate_critic.py --annotator anjali \
      --item pure-dev-0000_cctns --prd-file path/to/prd.txt
  (then prompts for each field; empty critic ends the item)

No LLM calls: this tool records human labels only. LLM-pre-labelled
candidates must go through the same hand verification before being
appended (guidelines Job 2).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABELS_DIR = ROOT / "training" / "labels"

CRITICS = ("security", "architecture", "performance")
CATEGORIES = ("security", "performance", "scalability", "reliability",
              "usability", "other")
KINDS = ("missing_context", "vague", "conflict")


def ask(prompt: str, choices: tuple | None = None) -> str:
    while True:
        val = input(prompt).strip()
        if choices is None:
            if val:
                return val
            print("  (required)")
        else:
            if val in choices:
                return val
            print(f"  (choose from {', '.join(choices)})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--annotator", required=True)
    ap.add_argument("--item", required=True)
    ap.add_argument("--prd-file", required=True)
    args = ap.parse_args()

    prd_text = Path(args.prd_file).read_text(encoding="utf-8")
    LABELS_DIR.mkdir(parents=True, exist_ok=True)
    out = LABELS_DIR / f"critic_supervision.{args.annotator}.jsonl"
    n = 0
    while True:
        critic = input(f"critic {CRITICS} (empty = done with item): ").strip()
        if not critic:
            break
        if critic not in CRITICS:
            print("unknown critic"); continue
        category = ask(f"category {CATEGORIES}: ", CATEGORIES)
        kind = ask(f"kind {KINDS}: ", KINDS)
        prd_sentence = ask("prd_sentence (verbatim quote): ")
        if prd_sentence not in prd_text:
            print("ERROR: not a verbatim substring of the PRD text; retry finding")
            continue
        issue = ask("issue: ")
        suggestion = ask("suggestion: ")
        reason = ask("reason: ")
        conflicts_with = None
        if kind == "conflict":
            conflicts_with = ask("conflicts_with (other critic): ", CRITICS)
        rec = {
            "item_id": args.item,
            "annotator": args.annotator,
            "labelled_at": datetime.now(timezone.utc).isoformat(),
            "prd_sentence": prd_sentence,
            "finding": {"critic": critic, "category": category, "kind": kind,
                        "issue": issue, "suggestion": suggestion,
                        "reason": reason, "conflicts_with": conflicts_with},
        }
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        n += 1
        print(f"recorded finding #{n} -> {out}")
    print(f"item {args.item}: {n} finding(s) recorded for {args.annotator}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
