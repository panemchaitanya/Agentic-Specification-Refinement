# NFR classifier checkpoint manifest (Drive artifact)

Canonical object: **unseeded retrain feasibility checkpoint** of the
Stage 3 DistilBERT NFR classifier. It is NOT the sweep-best run and NOT
the earlier preservation run; it is a fresh retrain of the same config
after the original Colab VM was recycled (2026-11-06 ~16:10 IST). Byte
durability of this artifact is verified; quality parity with any
recorded run is NOT claimed.

## Where the bytes live

Weights stay outside git (size). Google Drive folder `ASE-weights/`
(folder id `1ichiDmg2QCHFftcrwkfM1QtMdtDGHdq3`), shared anyone-with-link
view. The zip is stored as six ordered parts:

| Order | File | Bytes | Drive URL |
| --- | --- | --- | --- |
| 1 | distilbert-nfr-best.zip.part000 | 41,943,040 | https://drive.google.com/file/d/18rplanmZS3ssEDp74H9405B4W3ObAvLU/view |
| 2 | distilbert-nfr-best.zip.part001 | 41,943,040 | https://drive.google.com/file/d/1HGf9zsNa3CE31pRzQp6Rrokn0dJUbiel/view |
| 3 | distilbert-nfr-best.zip.part002 | 41,943,040 | https://drive.google.com/file/d/1DXeLIXcAH6pcdb7LDI3ebtLcD8RGx9c-/view |
| 4 | distilbert-nfr-best.zip.part003 | 41,943,040 | https://drive.google.com/file/d/1gQHo6gvvF7rMSdc44_Kr7KzlmpHEITlt/view |
| 5 | distilbert-nfr-best.zip.part004 | 41,943,040 | https://drive.google.com/file/d/1CXc3AABcV3kkSXRLld90ZBg1Pb5Orrxj/view |
| 6 | distilbert-nfr-best.zip.part005 | 37,302,947 | https://drive.google.com/file/d/1Z09v7kSDWFsnHBxlZ6WtLdc29Bq5MPhq/view |

Assembly:

```bash
cat distilbert-nfr-best.zip.part000 distilbert-nfr-best.zip.part001 \
    distilbert-nfr-best.zip.part002 distilbert-nfr-best.zip.part003 \
    distilbert-nfr-best.zip.part004 distilbert-nfr-best.zip.part005 \
    > distilbert-nfr-best.zip
```

Expected assembled artifact:

- size: 247,018,147 bytes
- sha256: `f036825316a2ed3fda6bec2c3223dac3fd2371d706d1209e5a4693667346d6f4`
- zip entries: `distilbert-nfr-best/{model.safetensors, config.json,
  label_order.json, tokenizer.json, tokenizer_config.json}`

Verification performed 2026-11-06 ~16:24 IST: all six parts downloaded
from the Drive links above, concatenated in order, exact size match,
`zipfile.testzip()` CRC check passed on every entry, sha256 as above.

## Training identity of this checkpoint

- Script: `training/train_nfr_classifier.py --backend distilbert
  --epochs 6 --batch 16 --max-len 128 --lr 5e-5
  --save-dir machines/hug/distilbert-nfr-best` (same config as the GPU
  sweep's best row).
- Input: `data/frozen/promise_nfr/dev.csv`, sha256
  `20eac7f8ce71234ffed5ca51cf2099e308be3d126a18215828a4828f4712fdec`
  (fetched into Colab and hash-asserted before training; the Drive copy
  `ase-dev-frozen.csv` in the same folder is byte-identical).
- Split: dev-internal stratified holdout, `SEED = 20261106`,
  validation fraction 0.2 (437 dev rows -> 351 train / 86 validation,
  12 classes).
- Hardware: Colab T4; cell wall time 51.836 s including zip.
- Run time: 2026-11-06 ~16:20 IST.

## Observed metrics (provenance-separated)

- This checkpoint's run: macro-F1 **0.6489** on the dev holdout,
  observed in the Colab cell output ~16:20 IST. Accuracy was not
  captured before the session ended - **unknown**, not estimated.
  torch RNG is unseeded, so this run differs from earlier runs of the
  same config.
- Historical recorded runs (see `training/pilot_results.json`; do NOT
  attribute these to the preserved checkpoint):
  - GPU sweep best config row: accuracy 0.826 / macro-F1 0.782.
  - Earlier preservation run (run 8): accuracy 0.8605 / macro-F1
    0.7571. Its checkpoint was lost with the recycled VM; only the
    recorded numbers survive.

## Open gap

Training is unseeded: identical configs produce materially different
dev-holdout metrics run to run (0.782 / 0.7571 / 0.6489 observed for
the same config). Deterministic seeded training and a reproducibility
check are real open work, not solved here.
