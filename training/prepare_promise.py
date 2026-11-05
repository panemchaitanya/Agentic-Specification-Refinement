"""Prepare and freeze the PROMISE NFR dataset (Stage 0).

Parses data/raw/promise_nfr/nfr.tar -> nfr/nfr.arff, deduplicates,
maps the original labels onto the project's NFR taxonomy
(src/specpipe/schemas.py NFR_CATEGORIES) and writes a frozen
BY-PROJECT dev/test split:

  data/frozen/promise_nfr/dev.csv
  data/frozen/promise_nfr/test.csv
  data/frozen/promise_nfr/FREEZE.json   (hashes, date, split rule, stats)

Freeze discipline (PROJECT_PLAN.md Stage 0): after this freeze, the test
split is not touched by any experiment, prompt tuning or pilot until the
final Stage 4 evaluation. Pilots (train_nfr_classifier.py) use dev only.

Original labels: F functional, A availability, L legal, LF look-and-feel,
MN maintainability, O operational, PE performance, SC scalability,
SE security, US usability, FT fault tolerance, PO portability.

Taxonomy mapping (proposal completeness-penalty categories):
  SE -> security; PE -> performance; SC -> scalability;
  A, FT -> reliability; US -> usability; L, LF, MN, O, PO -> other;
  F -> FR (functional, not an NFR).
The original label is kept in every row; the mapping is a view, not a
rewrite, so the mapping decision stays auditable.

License note: the Zenodo record metadata says CC-BY 4.0, but the
nfr.arff header itself asserts CC-BY-SA 3.0. We record both and treat
the stricter (CC-BY-SA 3.0, share-alike) as governing any derived files
we distribute; attribution is in FREEZE.json and docs/datasets.md.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
import tarfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW_TAR = ROOT / "data" / "raw" / "promise_nfr" / "nfr.tar"
OUT = ROOT / "data" / "frozen" / "promise_nfr"

LABELS = {"F", "A", "L", "LF", "MN", "O", "PE", "SC", "SE", "US", "FT", "PO"}
TAXONOMY = {
    "SE": "security", "PE": "performance", "SC": "scalability",
    "A": "reliability", "FT": "reliability", "US": "usability",
    "L": "other", "LF": "other", "MN": "other", "O": "other", "PO": "other",
    "F": "FR",
}
# Deterministic test-project selection: projects sorted by sha256 of their
# id, taken until >= 25% of rows are in test. Fixed rule, recorded below,
# so the freeze is reproducible without a random seed.
TEST_FRACTION_TARGET = 0.25


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_arff(text: str) -> list[dict]:
    """Parse the nfr.arff data section: ProjectID,'text',class per row."""
    rows, errors = [], []
    in_data = False
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not in_data:
            if line.upper() == "@DATA":
                in_data = True
            continue
        if not line or line.startswith("%"):
            continue
        # Split off ProjectID and class; the text is the quoted middle.
        first = line.find(",")
        last = line.rstrip(",").rfind(",")
        if first <= 0 or last <= first:
            errors.append((lineno, line[:80]))
            continue
        pid_s = line[:first].strip()
        label = line[last + 1:].strip().rstrip(",")
        text_mid = line[first + 1:last].strip()
        if len(text_mid) >= 2 and text_mid[0] in "'\"" and text_mid[-1] == text_mid[0]:
            text_mid = text_mid[1:-1].replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")
        if label not in LABELS or not pid_s.isdigit():
            errors.append((lineno, line[:80]))
            continue
        rows.append({"project_id": int(pid_s),
                     "text": " ".join(text_mid.split()),
                     "label": label})
    return rows, errors


def main() -> int:
    if not RAW_TAR.exists():
        print("run training/fetch_datasets.py first", file=sys.stderr)
        return 1
    with tarfile.open(RAW_TAR) as tf:
        member = tf.extractfile("nfr/nfr.arff")
        arff = member.read().decode("utf-8", errors="replace")
    rows, errors = parse_arff(arff)
    if errors:
        print(f"WARNING: {len(errors)} unparseable rows (first 5): {errors[:5]}")

    # Deduplicate on (project_id, normalized text): same sentence repeated
    # inside one project would leak between identical rows; cross-project
    # duplicates are kept but flagged so split leakage is measurable.
    seen, deduped, dupes = set(), [], 0
    for r in rows:
        key = (r["project_id"], r["text"].lower())
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        deduped.append(r)
    text_to_projects = defaultdict(set)
    for r in deduped:
        text_to_projects[r["text"].lower()].add(r["project_id"])
    cross_project_dupes = sum(1 for ps in text_to_projects.values() if len(ps) > 1)

    # Deterministic by-project split.
    projects = sorted({r["project_id"] for r in deduped})
    ranked = sorted(projects, key=lambda p: hashlib.sha256(f"promise-nfr-{p}".encode()).hexdigest())
    total = len(deduped)
    counts = Counter(r["project_id"] for r in deduped)
    test_projects, test_rows = [], 0
    for p in ranked:
        if test_rows >= TEST_FRACTION_TARGET * total and len(test_projects) >= 3:
            break
        test_projects.append(p)
        test_rows += counts[p]
    test_set = set(test_projects)

    OUT.mkdir(parents=True, exist_ok=True)
    fields = ["project_id", "text", "label", "category"]
    written = {}
    for name, pred in (("dev", lambda p: p not in test_set),
                       ("test", lambda p: p in test_set)):
        path = OUT / f"{name}.csv"
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            for r in deduped:
                if pred(r["project_id"]):
                    w.writerow({**r, "category": TAXONOMY[r["label"]]})
        written[name] = path

    dist = {name: Counter(r["label"] for r in csv.DictReader(open(p)))
            for name, p in written.items()}
    freeze = {
        "dataset": "PROMISE NFR",
        "source": {
            "zenodo_record": "https://zenodo.org/records/268542",
            "doi": "10.5281/zenodo.268542",
            "tera_promise": "https://openscience.us/repo/requirements/requirements-other/nfr.html",
            "license_record_metadata": "CC-BY-4.0",
            "license_file_header": "CC-BY-SA-3.0 (stricter; governs derived files)",
            "attribution": "Cleland-Huang, Mazrouee, Liguo, Port (2007), PROMISE NFR dataset",
        },
        "raw_tar_sha256": sha256_of(RAW_TAR),
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "frozen_by": "training/prepare_promise.py",
        "split_rule": {
            "unit": "project (ProjectID attribute, 15 projects)",
            "rule": "projects sorted by sha256('promise-nfr-<id>'), taken until "
                    f">= {TEST_FRACTION_TARGET:.0%} of rows and >= 3 projects are in test",
            "test_projects": sorted(test_set),
            "dev_projects": sorted(p for p in projects if p not in test_set),
        },
        "stats": {
            "rows_parsed": len(rows),
            "parse_errors": len(errors),
            "within_project_duplicates_removed": dupes,
            "cross_project_duplicate_texts": cross_project_dupes,
            "rows_dev": sum(1 for r in deduped if r["project_id"] not in test_set),
            "rows_test": test_rows,
            "label_distribution_dev": dict(sorted(dist["dev"].items())),
            "label_distribution_test": dict(sorted(dist["test"].items())),
            "taxonomy_mapping": TAXONOMY,
            "coverage_caveat": {
                "classes_missing_from_test": sorted(LABELS - set(dist["test"])),
                "classes_missing_from_dev": sorted(LABELS - set(dist["dev"])),
                "note": "Deterministic project split; rare classes (FT/MN/PO "
                        "have <= 13 rows total) may miss a split. Final "
                        "evaluation must report per-class support.",
            },
        },
        "files": {name: {"sha256": sha256_of(p), "bytes": p.stat().st_size}
                  for name, p in written.items()},
        "discipline": "test.csv is frozen: no experiments, prompt tuning or "
                      "pilots until the final Stage 4 evaluation.",
    }
    (OUT / "FREEZE.json").write_text(json.dumps(freeze, indent=2) + "\n")
    print(json.dumps({k: freeze["stats"][k] for k in
                      ("rows_parsed", "within_project_duplicates_removed",
                       "cross_project_duplicate_texts", "rows_dev", "rows_test")}, indent=2))
    print("test projects:", sorted(test_set))
    print("dev label dist:", dict(sorted(dist["dev"].items())))
    print("test label dist:", dict(sorted(dist["test"].items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
