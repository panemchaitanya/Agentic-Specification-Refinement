# Datasets and training

Owns: `data/raw/`, `data/frozen/`, `training/` (see LANES.md).

## What is here

| Path | Purpose |
| --- | --- |
| `fetch_datasets.py` | Download pinned public datasets into `data/raw/`, record sha256 + provenance in `RAW_MANIFEST.json`. Idempotent. |
| `prepare_promise.py` | Parse PROMISE NFR (Zenodo 268542), dedupe, map labels to the project taxonomy, freeze the by-project dev/test split. |
| `prepare_pure.py` | Freeze the PURE (Zenodo 7118517) document inventory: per-document sha256, deterministic by-document split. |
| `build_pairs.py` | Build KEP/Rust-RFC/PEP PRD-to-spec pair candidates from git history (proposal = adding-commit content, rename-followed). |
| `train_nfr_classifier.py` | Stage 3 pilot on the frozen PROMISE **dev** split: TF-IDF baseline and optional DistilBERT fine-tune, measured runs recorded. |
| `ANNOTATION_GUIDELINES.md` | FR/NFR extraction rules, critic contextual-supervision format, two-labeller + kappa protocol. |
| `build_gold_skeletons.py` | Emit per-item gold-file skeletons (`eval/gold.py` format, status SKELETON) for the two-extractor pass. |
| `annotate_critic.py` | Hand-labelling CLI for contextual critic supervision (JSONL, verbatim-quote validation, per-annotator files for kappa). |
| `validate_pairs.py` | Interactive hand-validation of pair candidates; verdicts recorded in pilot_sample.json, only passing pairs reach gold/freeze. |
| `extract_pure_text.py` | Convert PURE pdf/doc/rtf/html to text under `data/frozen/pure/text/<split>/`; per-document coverage in EXTRACTION_REPORT.json (failures recorded, never dropped silently). |
| `pilot_results.json` | Recorded pilot runs (created by the pilot; numbers are never hand-edited). |
| `WEIGHTS_CHECKPOINT.md` | Manifest for the Drive-hosted DistilBERT checkpoint: links, assembly, hashes, run identity, unseeded-retrain caveat. |

## Reproduce from scratch

```bash
python training/fetch_datasets.py     # raw downloads -> data/raw/ (not committed)
python training/prepare_promise.py    # -> data/frozen/promise_nfr/{dev,test}.csv + FREEZE.json
python training/prepare_pure.py       # -> data/frozen/pure/FREEZE.json
python training/build_pairs.py --sample 4   # -> data/frozen/pairs/pilot_sample.json
python -m pip install scikit-learn    # for the baseline pilot
python training/train_nfr_classifier.py --backend tfidf
# optional, heavier: pip install torch transformers
# python training/train_nfr_classifier.py --backend distilbert
```

Every step is deterministic (no random seeds except the pilot's fixed
`SEED = 20261106`), so the frozen files should hash identically on a
re-run; FREEZE.json records the expected sha256 values.

## Freeze discipline

- The test split (`data/frozen/promise_nfr/test.csv`, PURE test documents,
  validated test pairs) is **not touched** by any experiment, prompt
  tuning, or pilot until the final Stage 4 evaluation.
- The pilot trains and validates **inside the dev split only**.
- Pair candidates are NOT frozen yet: `pilot_sample.json` awaits the
  hand-validation checklist inside it, then a by-proposal freeze lands.

## LLM-use rule for this lane

No LLM calls on real dataset text with the course free-tier key without
project sign-off; synthetic PRDs (`data/sample_prds/`) are the safe
inputs for pipeline development. Model-generated text is never recorded
as human gold or verified critic supervision.
