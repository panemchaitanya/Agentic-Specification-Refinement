# Frozen data (Stage 0)

Contents and owners:

- `promise_nfr/` - frozen by-project split of the PROMISE NFR dataset
  (`dev.csv`, `test.csv`, `FREEZE.json` with hashes, split rule, label
  stats). Produced by `training/prepare_promise.py`.
- `pure/` - `FREEZE.json` inventory + by-document split of the PURE
  requirements documents (documents themselves re-fetched locally by
  `training/fetch_datasets.py` + `training/prepare_pure.py`).
- `pairs/` - `pilot_sample.json`: KEP/RFC/PEP PRD-to-spec pair CANDIDATES
  with a hand-validation checklist. Not frozen, not gold, until validated.

Discipline: nothing in the test split is used for experiments, prompt
tuning or pilots before the final Stage 4 evaluation. See
`training/README.md`.
