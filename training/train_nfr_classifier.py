"""Stage 3 pilot: NFR classification subtask on the frozen PROMISE dev split.

The proposal's critics need contextual supervision (docs/datasets.md label
gap), but the first measurable step is the classification subtask PROMISE
actually labels: given a requirement sentence, predict its class
(F / availability / ... / portability). This pilot measures quality and
cost for that subtask on the DEV split only - the frozen test split stays
untouched until the final Stage 4 evaluation (FREEZE.json discipline).

Two backends:
  --backend tfidf      TF-IDF + logistic regression. Always runnable on
                       CPU in seconds; the honest floor any trained model
                       must beat.
  --backend distilbert Fine-tunes distilbert-base-uncased (needs torch +
                       transformers installed). Reports measured wall time
                       and peak RSS; on a GPU host it also reports peak
                       CUDA memory. No quality claim is made without a
                       measured run.

Results are appended to training/pilot_results.json with the exact
configuration, dev-split hashes and timestamps: reported numbers must be
traceable to a recorded run.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import resource
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / "data" / "frozen" / "promise_nfr"
RESULTS = Path(__file__).resolve().parent / "pilot_results.json"
DEV_INTERNAL_TEST_FRACTION = 0.2  # stratified split INSIDE dev; test.csv untouched
SEED = 20261106


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_dev() -> tuple[list[str], list[str]]:
    dev = FROZEN / "dev.csv"
    if not dev.exists():
        sys.exit("run training/prepare_promise.py first (dev split missing)")
    texts, labels = [], []
    with open(dev) as fh:
        for row in csv.DictReader(fh):
            texts.append(row["text"])
            labels.append(row["label"])
    return texts, labels


def split_dev(texts, labels):
    """Stratified train/validation split inside dev (fixed seed)."""
    import random
    by_label = {}
    for t, l in zip(texts, labels):
        by_label.setdefault(l, []).append(t)
    rng = random.Random(SEED)
    tr_x, tr_y, va_x, va_y = [], [], [], []
    for label, group in sorted(by_label.items()):
        group = group[:]
        rng.shuffle(group)
        n_val = max(1, round(len(group) * DEV_INTERNAL_TEST_FRACTION)) if len(group) > 2 else 0
        va_x.extend(group[:n_val]); va_y.extend([label] * n_val)
        tr_x.extend(group[n_val:]); tr_y.extend([label] * (len(group) - n_val))
    return tr_x, tr_y, va_x, va_y


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def record(result: dict) -> None:
    results = []
    if RESULTS.exists():
        results = json.loads(RESULTS.read_text()).get("runs", [])
    results.append(result)
    RESULTS.write_text(json.dumps({"runs": results}, indent=2) + "\n")
    print(f"recorded run in {RESULTS}")


def run_tfidf(tr_x, tr_y, va_x, va_y) -> dict:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import accuracy_score, f1_score, classification_report
    t0 = time.time()
    vec = TfidfVectorizer(ngram_range=(1, 2), min_df=1)
    clf = LogisticRegression(max_iter=2000, C=10.0)
    clf.fit(vec.fit_transform(tr_x), tr_y)
    train_s = time.time() - t0
    pred = clf.predict(vec.transform(va_x))
    labels = sorted(set(tr_y) | set(va_y))
    return {
        "backend": "tfidf+logreg",
        "train_seconds": round(train_s, 3),
        "peak_rss_mb": round(peak_rss_mb(), 1),
        "accuracy": round(accuracy_score(va_y, pred), 4),
        "macro_f1": round(f1_score(va_y, pred, average="macro", zero_division=0), 4),
        "per_class": classification_report(va_y, pred, labels=labels,
                                           output_dict=True, zero_division=0),
    }


def run_distilbert(tr_x, tr_y, va_x, va_y, epochs: int, batch: int, max_len: int, lr: float,
                   save_dir: str | None = None) -> dict:
    import torch
    from torch.utils.data import DataLoader, Dataset
    from transformers import (AutoModelForSequenceClassification,
                              AutoTokenizer)
    from sklearn.metrics import accuracy_score, f1_score, classification_report

    labels = sorted(set(tr_y) | set(va_y))
    lid = {l: i for i, l in enumerate(labels)}
    tok = AutoTokenizer.from_pretrained("distilbert-base-uncased")

    class DS(Dataset):
        def __init__(self, xs, ys):
            self.enc = tok(xs, truncation=True, max_length=max_len, padding="max_length")
            self.ys = [lid[y] for y in ys]
        def __len__(self): return len(self.ys)
        def __getitem__(self, i):
            item = {k: torch.tensor(v[i]) for k, v in self.enc.items()}
            item["labels"] = torch.tensor(self.ys[i])
            return item

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = AutoModelForSequenceClassification.from_pretrained(
        "distilbert-base-uncased", num_labels=len(labels)).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=lr)
    loader = DataLoader(DS(tr_x, tr_y), batch_size=batch, shuffle=True)
    t0 = time.time()
    model.train()
    for _ in range(epochs):
        for b in loader:
            opt.zero_grad()
            loss = model(**{k: v.to(device) for k, v in b.items()}).loss
            loss.backward()
            opt.step()
    train_s = time.time() - t0
    model.eval()
    pred = []
    with torch.no_grad():
        va_loader = DataLoader(DS(va_x, va_y), batch_size=batch)
        for b in va_loader:
            y = b.pop("labels")
            logits = model(**{k: v.to(device) for k, v in b.items()}).logits
            pred.extend(logits.argmax(-1).cpu().tolist())
    pred_labels = [labels[i] for i in pred]
    out = {
        "backend": "distilbert-base-uncased",
        "device": device,
        "epochs": epochs, "batch_size": batch, "max_len": max_len, "lr": lr,
        "train_seconds": round(train_s, 3),
        "peak_rss_mb": round(peak_rss_mb(), 1),
        "accuracy": round(accuracy_score(va_y, pred_labels), 4),
        "macro_f1": round(f1_score(va_y, pred_labels, average="macro", zero_division=0), 4),
        "per_class": classification_report(va_y, pred_labels, labels=labels,
                                           output_dict=True, zero_division=0),
    }
    if device == "cuda":
        out["peak_cuda_mb"] = round(torch.cuda.max_memory_allocated() / 1e6, 1)
    if save_dir:
        from pathlib import Path as _P
        d = _P(save_dir)
        d.mkdir(parents=True, exist_ok=True)
        model.save_pretrained(d)
        tok.save_pretrained(d)
        (d / "label_order.json").write_text(json.dumps(labels))
        out["saved_to"] = str(d)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["tfidf", "distilbert"], default="tfidf")
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--max-len", type=int, default=128)
    ap.add_argument("--lr", type=float, default=2e-5)
    ap.add_argument("--save-dir", default="",
                    help="if set, save model+tokenizer+label order here")
    args = ap.parse_args()

    texts, labels = load_dev()
    tr_x, tr_y, va_x, va_y = split_dev(texts, labels)
    print(f"dev rows={len(texts)} -> train={len(tr_x)} val={len(va_x)} "
          f"(classes={len(set(labels))})")
    result = run_tfidf(tr_x, tr_y, va_x, va_y) if args.backend == "tfidf" \
        else run_distilbert(tr_x, tr_y, va_x, va_y, args.epochs, args.batch, args.max_len, args.lr,
                        save_dir=args.save_dir or None)
    result.update({
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "dataset": "PROMISE NFR frozen dev split only (test untouched)",
        "dev_csv_sha256": sha256_of(FROZEN / "dev.csv"),
        "dev_internal_split": {"seed": SEED,
                               "validation_fraction": DEV_INTERNAL_TEST_FRACTION,
                               "stratified": True},
        "train_rows": len(tr_x), "val_rows": len(va_x),
        "label_distribution_train": dict(sorted(Counter(tr_y).items())),
        "note": "Classification subtask only; this is not the full critic "
                "(contextual gap detection supervision is separate work - "
                "see training/ANNOTATION_GUIDELINES.md).",
    })
    print(json.dumps({k: result[k] for k in
                      ("backend", "train_seconds", "accuracy", "macro_f1")}, indent=2))
    record(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
