"""Experiment harness: runs a baseline over items, repeats, and writes an
append-only JSONL run log (eval/runs/).

The log is the raw evidence for every later claim. Each file starts with a
header line (config, code version, dataset/freeze id, timestamp) followed
by one line per (item, repeat). Scoring against gold only happens when a
frozen gold file and rubric verdicts exist; generation runs without them
still log counts, categories, question counts, OPEN rates and LLM cost.
"""
from __future__ import annotations

import inspect
import json
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from .baselines import BASELINES, ALL_CRITICS
from .llm_logging import LoggingLLM
from .oracle import ReferenceOracle
from .scoring import open_rate, nfr_kinds, spec_completeness


def _code_version() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            cwd=Path(__file__).parent.parent, timeout=10,
        ).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


@dataclass(frozen=True)
class ExperimentConfig:
    baseline: str            # key into BASELINES: B1, B2, B3, B3T, B4
    dataset_id: str          # e.g. "sample-prds" or a freeze id
    repeats: int = 3         # proposal: every run repeated three times
    seed: int = 0
    critic_names: tuple = ALL_CRITICS  # subset for ablations
    code_version: str = field(default_factory=_code_version)
    note: str = ""


def run_experiment(config: ExperimentConfig, items: list, llm_factory,
                   out_dir: str, run_id: str | None = None) -> str:
    """Run one baseline over items; return the JSONL log path.

    items: [{"id", "prd_text", "reference"?, "implied_categories"?}].
    llm_factory: callable returning a fresh LLM per repeat (call counters
    must not bleed across repeats).
    """
    if config.baseline not in BASELINES:
        raise ValueError(f"unknown baseline {config.baseline!r}")
    entry = BASELINES[config.baseline]
    run_id = run_id or (f"{config.baseline}-{config.dataset_id}-"
                        f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
    out_path = Path(out_dir) / f"{run_id}.jsonl"
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "header", "run_id": run_id,
            "config": {**asdict(config),
                       "critic_names": list(config.critic_names)},
            "started_utc": f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
        }) + "\n")
        for item in items:
            for rep in range(config.repeats):
                llm = LoggingLLM(llm_factory())
                oracle = (ReferenceOracle(item.get("reference", []))
                          if entry["oracle"] else None)
                params = inspect.signature(entry["fn"]).parameters
                kwargs = {"title": item["id"]}
                if "critic_names" in params:
                    kwargs["critic_names"] = config.critic_names
                meta: dict = {}
                if "meta" in params:
                    kwargs["meta"] = meta
                if entry["oracle"]:
                    spec = entry["fn"](item["prd_text"], llm, oracle, **kwargs)
                else:
                    spec = entry["fn"](item["prd_text"], llm, **kwargs)
                rec = {
                    "type": "run", "run_id": run_id, "item_id": item["id"],
                    "repeat": rep, "baseline": config.baseline,
                    "n_requirements": len(spec.requirements),
                    "n_fr": sum(1 for r in spec.requirements if r.kind == "FR"),
                    "n_nfr": sum(1 for r in spec.requirements if r.kind != "FR"),
                    "nfr_categories": sorted(nfr_kinds(spec.requirements)),
                    "open_rate": round(open_rate(spec.requirements), 4),
                    "questions_asked": meta.get("questions_asked", 0),
                    "questions_answered": sum(
                        1 for r in spec.requirements
                        if r.trace_kind == "human_answer"),
                    "n_findings": meta.get("n_findings", 0),
                    "llm": llm.summary(),
                }
                if item.get("implied_categories") is not None:
                    rec["completeness"] = round(spec_completeness(
                        spec.requirements, item["implied_categories"]), 4)
                if oracle is not None:
                    rec["oracle_audit"] = oracle.audit_report()
                fh.write(json.dumps(rec) + "\n")
    return str(out_path)
