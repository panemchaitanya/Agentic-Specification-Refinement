"""Bounded real-model PILOT on the repo's synthetic PRDs (label: pilot).

Purpose: measure feasibility of the free Gemini route - call counts,
latency, rate-limit behavior, output-parse failures - before any dev-split
experiment. Synthetic inputs only (per project constraint); results are
pilot evidence, never hypothesis evidence.

Requires: GEMINI_API_KEY in the environment. Aborts cleanly on the first
unrecoverable LLM error and marks the log partial; no retry storms.

Usage: python eval/run_pilot.py [max_calls]
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from eval.baselines import b1_single_llm, b2_orchestrator_critics, b3_pipeline_no_human
from eval.llm_logging import LoggingLLM
from specpipe.llm import GeminiLLM, LLMError

_ROOT = Path(__file__).parent.parent
DEFAULT_MAX_CALLS = 40  # hard budget for the whole pilot; free-tier safe


def main():
    max_calls = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_MAX_CALLS
    out_dir = _ROOT / "eval" / "runs"
    run_id = f"PILOT-sample-prds-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    log_path = out_dir / f"{run_id}.jsonl"
    items = [{"id": p.stem, "prd_text": p.read_text(encoding="utf-8")}
             for p in sorted((_ROOT / "data" / "sample_prds").glob("*.txt"))]
    calls_used = 0
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "header", "run_id": run_id, "label": "pilot",
            "note": ("free-route feasibility pilot on synthetic PRDs; "
                     "NOT hypothesis evidence"),
            "max_calls": max_calls,
            "started_utc": f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
        }) + "\n")
        for name, fn in (("B1", b1_single_llm),
                         ("B2", b2_orchestrator_critics),
                         ("B3", b3_pipeline_no_human)):
            for item in items:
                if calls_used >= max_calls:
                    fh.write(json.dumps({
                        "type": "aborted", "reason": "call budget reached",
                        "calls_used": calls_used}) + "\n")
                    print(f"budget reached at {calls_used} calls; "
                          f"partial log: {log_path}")
                    return
                llm = LoggingLLM(GeminiLLM())
                try:
                    spec = fn(item["prd_text"], llm, title=item["id"])
                except LLMError as exc:
                    fh.write(json.dumps({
                        "type": "error", "baseline": name,
                        "item_id": item["id"], "error": str(exc)[:300],
                        "calls_used": calls_used + llm.summary()["calls"]}) + "\n")
                    print(f"LLM error on {name}/{item['id']}: {exc}; "
                          f"partial log: {log_path}")
                    return
                s = llm.summary()
                calls_used += s["calls"]
                fh.write(json.dumps({
                    "type": "pilot_run", "baseline": name,
                    "item_id": item["id"],
                    "n_requirements": len(spec.requirements),
                    "n_fr": sum(1 for r in spec.requirements if r.kind == "FR"),
                    "n_nfr": sum(1 for r in spec.requirements if r.kind != "FR"),
                    "llm": s,
                }) + "\n")
        fh.write(json.dumps({"type": "complete", "calls_used": calls_used}) + "\n")
    print(f"pilot complete, {calls_used} calls: {log_path}")


if __name__ == "__main__":
    main()
