"""Hand-validation CLI for PRD-to-spec pair candidates.

Walks data/frozen/pairs/pilot_sample.json, shows each candidate's
proposal/spec heads and sizes, and records the reviewer's checklist
verdict back into pilot_sample.json["validation"]. Only pairs with
checks_passed=true are eligible for gold skeletons
(training/build_gold_skeletons.py) and the eventual pair freeze.

Checklist (from build_pairs.py, PROJECT_PLAN.md research gap 1):
  1. proposal side reads like a proposal/motivation, not a finished spec
  2. spec side is the evolved version of the SAME proposal
  3. no later revisions folded into the proposal side
  4. pair is usable: enough content on both sides to extract FRs/NFRs
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "data" / "frozen" / "pairs" / "pilot_sample.json"
CAND = ROOT / "data" / "raw" / "pairs" / "candidates"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reviewer", required=True)
    ap.add_argument("--only", help="validate one pair, e.g. kep-4802")
    args = ap.parse_args()
    pilot = json.loads(PILOT.read_text())
    for rec in pilot["pairs"]:
        key = f"{rec['source']}:{rec['id']}"
        item = f"{rec['source']}-{rec['id']}"
        if args.only and item != args.only:
            continue
        cur = pilot["validation"].get(key, {})
        if cur.get("checks_passed") is not None and not args.only:
            continue
        prop = (CAND / rec["source"] / rec["id"] / "proposal.txt").read_text()
        spec = (CAND / rec["source"] / rec["id"] / "spec.txt").read_text()
        print("=" * 72)
        print(f"{item}  added={rec['added_at'][:10]}  "
              f"proposal={rec['proposal_bytes']}B  spec={rec['spec_bytes']}B")
        print("--- proposal head ---"); print(prop[:600])
        print("--- spec head ---"); print(spec[:600])
        ok = input("passes ALL 4 checklist checks? [y/n/s(kip)] ").strip().lower()
        if ok == "s" or ok not in ("y", "n"):
            continue
        notes = input("notes (optional): ").strip()
        pilot["validation"][key] = {
            "reviewer": args.reviewer,
            "checks_passed": ok == "y",
            "notes": notes or None,
        }
        PILOT.write_text(json.dumps(pilot, indent=2) + "\n")
        print(f"recorded {key}: checks_passed={ok == 'y'}")
    done = sum(1 for v in pilot["validation"].values() if v.get("checks_passed") is not None)
    passed = sum(1 for v in pilot["validation"].values() if v.get("checks_passed"))
    print(f"validation progress: {done}/{len(pilot['pairs'])} reviewed, {passed} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
