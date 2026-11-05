"""Harness smoke run: exercises B1-B4 end to end with the deterministic
MockLLM on the repo's two synthetic PRDs.

This proves the plumbing (baselines compose, oracle answers and logs, run
logs are written and parse). It is NOT evaluation evidence: MockLLM output
is rule-based demo text, the tiny references below are synthetic, and no
frozen split exists yet. Real runs need the datasets lane's frozen split
and an LLM budget decision.

Known smoke artifact: MockLLM builds critic prd_sentence quotes from the
WRAPPED prompt ("PRD:\n..."), so finding_trace_verbatim_rate is 0.0 in
smoke logs. That is the validity metric correctly catching a mock
artifact, not a pipeline defect; the real model is instructed to quote
the PRD verbatim and this metric measures whether it actually does.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from eval.harness import ExperimentConfig, run_experiment
from specpipe.llm import MockLLM

_ROOT = Path(__file__).parent.parent

# Synthetic references for plumbing only - deliberately obvious keyword
# overlap so the extractive oracle path is exercised.
_REFERENCE = {
    "shopping_app": [
        {"id": "G1", "kind": "security",
         "text": "Customer data shall be encrypted at rest and in transit."},
        {"id": "G2", "kind": "performance",
         "text": "Peak load shall be 500 concurrent users at under 2s p95."},
    ],
    "classroom_poll": [
        {"id": "G1", "kind": "security",
         "text": "Student records shall be encrypted and access-logged."},
        {"id": "G2", "kind": "performance",
         "text": "The system shall support 200 concurrent voters."},
    ],
}


def main():
    items = []
    for path in sorted((_ROOT / "data" / "sample_prds").glob("*.txt")):
        items.append({
            "id": path.stem,
            "prd_text": path.read_text(encoding="utf-8"),
            "reference": _REFERENCE.get(path.stem, []),
            "implied_categories": ["security", "performance"],
        })
    logs = []
    for baseline in ("B1", "B2", "B3", "B3T", "B4"):
        cfg = ExperimentConfig(
            baseline=baseline, dataset_id="sample-prds", repeats=1,
            note=("SMOKE RUN - deterministic MockLLM, synthetic references; "
                  "not evaluation evidence"))
        logs.append(run_experiment(cfg, items, MockLLM,
                                   str(_ROOT / "eval" / "runs")))
    for log in logs:
        print(log)
        for line in Path(log).read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec["type"] == "run":
                print(f"  {rec['item_id']}: reqs={rec['n_requirements']} "
                      f"nfr={rec['n_nfr']} open_rate={rec['open_rate']} "
                      f"llm_calls={rec['llm']['calls']}")


if __name__ == "__main__":
    main()
