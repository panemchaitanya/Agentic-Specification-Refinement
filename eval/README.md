# eval/ - evaluation lane

Harness, baselines B1-B4, oracle, scoring and run logs for the ReqHQ
Agentic Specification Refinement project. The protocol and the fixed
hypothesis-readout rules live in `docs/evaluation.md`; read it first.

## Layout

- `baselines.py` - B1 single LLM, B2 orchestrator+critics, B3 pipeline
  without human, B3T targeted+oracle (H3 arm), B4 ask-everything; critic
  subsets for ablations.
- `oracle.py` - ReferenceOracle: answers questions from the reference
  spec only, with a full leakage log.
- `scoring.py` - pure scoring and H1/H2/H3 readout functions (fixed
  pre-data rules).
- `gold.py` - loaders/validators for gold and match-verdict files.
- `harness.py` - `run_experiment`: repeats, JSONL run logs.
- `summarize.py` - aggregate run logs; scored tables from gold+verdicts.
- `audit.py` - deterministic 20% match-audit sampling (seeded).
- `llm_logging.py` - per-call latency/character logging wrapper.
- `run_smoke.py` - MockLLM plumbing check on the two sample PRDs.
- `runs/` - append-only JSONL run logs.

## Run

```bash
python -m pytest tests/          # unit tests incl. scoring/readout
python eval/run_smoke.py         # plumbing smoke run (NOT evidence)
```

A real evaluation run needs a frozen dataset (datasets lane) and an LLM
route; then:

```python
from eval.harness import ExperimentConfig, run_experiment
from specpipe.llm import GeminiLLM
cfg = ExperimentConfig(baseline="B3", dataset_id="<freeze-id>", repeats=3)
run_experiment(cfg, items, GeminiLLM, "eval/runs")
```

Smoke-run logs already in `runs/` are deterministic-mock plumbing checks
and are never cited as results.
