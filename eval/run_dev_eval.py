"""Bounded dev-split evaluation run on the frozen PURE dev gold subset.

BOUNDED DEV EVALUATION (prefix scope): every baseline receives each
document truncated to the same 120,000-char prefix the LLM pre-labeler
and the human reviewers saw (docs/evaluation.md section 6 rule). This is
a dev-stage correctness repair, not the final protocol: full-document
input remains the only valid scope for the TEST evaluation.

Eligibility is fail-closed (rubric R5): a gold document that fails
eval/gold.py::load_gold (not FROZEN human-verified, incomplete review
scope) or gold_doc_scorable (unresolved skips/ties/contested edits) is
EXCLUDED from the run and reported; no partial-gold PRF is computed.

Requires: GEMINI_API_KEY in the environment. Usage:
    python eval/run_dev_eval.py [--baselines B1,B2,B3] [--dry-run]
--dry-run lists eligible/excluded documents without calling any LLM.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from eval.gold import gold_doc_scorable, load_gold
from eval.harness import ExperimentConfig, run_experiment
from specpipe.llm import GeminiLLM

_ROOT = Path(__file__).parent.parent
GOLD_DEV = _ROOT / "data" / "frozen" / "gold" / "dev"
TEXT_DEV = _ROOT / "data" / "frozen" / "pure" / "text" / "dev"
SCOPE = 120_000  # training/llm_prelabel.py MAX_DOC_CHARS


def collect_items() -> tuple[list, list]:
    """(eligible items, excluded [(item_id, reason)]) for the dev run."""
    items, excluded = [], []
    for path in sorted(GOLD_DEV.glob("*.gold.json")):
        item_id = path.name.replace(".gold.json", "")
        try:
            gold = load_gold(str(path))
        except ValueError as exc:
            excluded.append((item_id, f"load_gold refused: {exc}"))
            continue
        ok, reason = gold_doc_scorable(gold)
        if not ok:
            excluded.append((item_id, reason))
            continue
        # same resolution as training/verify_suggestions.py: the frozen
        # text file is named after the source document, not the item id
        doc_name = Path(gold["prd_path"]).name + ".txt"
        text_path = TEXT_DEV / doc_name
        if not text_path.exists():
            excluded.append((item_id, f"missing text {doc_name}"))
            continue
        items.append({
            "id": item_id,
            "prd_text": text_path.read_text(encoding="utf-8"),
            "reference": gold["gold_requirements"],
            "implied_categories": gold.get("implied_categories", []),
        })
    return items, excluded


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baselines", default="B1,B2,B3")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    items, excluded = collect_items()
    report = {
        "eligible": len(items),
        "excluded": [{"item_id": i, "reason": r} for i, r in excluded],
        "scope_chars": SCOPE,
        "label": "bounded dev evaluation (120k-char prefix scope)",
    }
    print(json.dumps(report, indent=2))
    if args.dry_run or not items:
        return 0 if items else 1
    logs = []
    for baseline in args.baselines.split(","):
        cfg = ExperimentConfig(
            baseline=baseline, dataset_id="pure-dev-frozen", repeats=1,
            input_scope_chars=SCOPE,
            persist_requirements=True,
            note=("bounded dev evaluation (120k-char prefix scope); gold = "
                  "anchored human-verified FROZEN subset only; NOT the "
                  "final TEST protocol"))
        logs.append(run_experiment(cfg, items, GeminiLLM,
                                   str(_ROOT / "eval" / "runs")))
    meta = {"run_id": f"DEV-bounded-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}",
            "label": report["label"], "scope_chars": SCOPE,
            "eligible_items": [i["id"] for i in items],
            "excluded": report["excluded"], "logs": logs}
    out = _ROOT / "eval" / "runs" / f"{meta['run_id']}-meta.json"
    out.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"run complete: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
