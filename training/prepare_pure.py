"""Prepare and freeze the PURE document inventory (Stage 0).

PURE (https://zenodo.org/records/7118517, CC-BY 4.0 with the curator's
provenance caveat) supplies the PRD-style requirements documents used for
ground-truth extraction and frozen evaluation.

The PDFs themselves are NOT committed (size, per-document provenance).
What this script freezes is the inventory and the BY-DOCUMENT split: every
document is one "project" for splitting. Output:

  data/frozen/pure/FREEZE.json  per-document sha256, bytes, format,
                                dev/test assignment, split rule, date

The evaluation lane re-extracts documents locally via
training/fetch_datasets.py + this script and verifies the hashes here.
Documents dated after the evaluation model's training cutoff are flagged
when a year is visible in the filename (proposal contamination control);
the eval lane decides the model cutoff and filters accordingly.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "pure"
OUT = ROOT / "data" / "frozen" / "pure"
TEST_FRACTION_TARGET = 0.25


def norm(name: str) -> str:
    """Normalise stems for fuzzy XML/document matching (naming varies)."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    doc_zip = RAW / "requirements.zip"
    xml_zip = RAW / "requirements-xml.zip"
    for p in (doc_zip, xml_zip):
        if not p.exists():
            print("run training/fetch_datasets.py first", file=sys.stderr)
            return 1

    extract = RAW / "extracted"
    extract.mkdir(parents=True, exist_ok=True)
    docs = []
    with zipfile.ZipFile(doc_zip) as z:
        for name in z.namelist():
            if name.endswith("/") or "__MACOSX" in name:
                continue
            dest = extract / Path(name).name
            dest.write_bytes(z.read(name))
            docs.append(dest)
    xml_docs = []
    xml_stems = set()
    with zipfile.ZipFile(xml_zip) as z:
        for name in z.namelist():
            if name.endswith("/") or "__MACOSX" in name:
                continue
            dest = extract / "xml" / Path(name).name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(name))
            xml_docs.append(dest.name)
            xml_stems.add(Path(name).stem)

    inventory = []
    for d in sorted(docs, key=lambda p: p.name):
        year = re.match(r"(\d{4})", d.name)
        inventory.append({
            "document": d.name,
            "format": d.suffix.lstrip(".").lower(),
            "year_prefix": int(year.group(1)) if year else None,
            "bytes": d.stat().st_size,
            "sha256": sha256_of(d),
            "has_xml_version": d.stem in xml_stems or any(
                norm(d.stem) in norm(x) or norm(x) in norm(d.stem)
                for x in xml_stems),
        })

    # Deterministic by-document split, same hash-rank rule as PROMISE.
    ranked = sorted(inventory,
                    key=lambda r: hashlib.sha256(f"pure-{r['document']}".encode()).hexdigest())
    total_bytes = sum(r["bytes"] for r in inventory)
    test_docs, test_bytes = [], 0
    for r in ranked:
        if test_bytes >= TEST_FRACTION_TARGET * total_bytes and len(test_docs) >= 10:
            break
        test_docs.append(r["document"])
        test_bytes += r["bytes"]
    test_set = set(test_docs)
    for r in inventory:
        r["split"] = "test" if r["document"] in test_set else "dev"

    OUT.mkdir(parents=True, exist_ok=True)
    freeze = {
        "dataset": "PURE (Public Requirements dataset)",
        "source": {
            "zenodo_record": "https://zenodo.org/records/7118517",
            "doi": "10.5281/zenodo.7118517",
            "license": "CC-BY-4.0",
            "provenance_caveat": "Curator gathered documents from public web "
                                 "sources and is not aware of license/IP claims; "
                                 "credit the dataset and drop any document the "
                                 "curator removes.",
        },
        "raw_sha256": {p.name: sha256_of(p) for p in (doc_zip, xml_zip)},
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "frozen_by": "training/prepare_pure.py",
        "split_rule": {
            "unit": "document (each document is one project)",
            "rule": "documents sorted by sha256('pure-<name>'), taken until "
                    f">= {TEST_FRACTION_TARGET:.0%} of bytes and >= 10 documents "
                    "are in test",
        },
        "stats": {
            "documents": len(inventory),
            "with_xml_version": sum(1 for r in inventory if r["has_xml_version"]),
            "xml_files_present": len(xml_stems),
            "xml_note": "Record description advertises 19 XML documents; the "
                        "zip contains 18. Matching to PDF/doc stems is "
                        "best-effort (normalised containment) because naming "
                        "conventions differ between the two archives.",
            "formats": sorted({r["format"] for r in inventory}),
            "dev_documents": len(inventory) - len(test_set),
            "test_documents": len(test_set),
            "test_documents_list": sorted(test_set),
        },
        "documents": inventory,
        "discipline": "test documents are frozen: no experiments, prompt "
                      "tuning, or extraction-guideline iteration on them until "
                      "the final Stage 4 evaluation.",
    }
    (OUT / "FREEZE.json").write_text(json.dumps(freeze, indent=2) + "\n")
    print(json.dumps(freeze["stats"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
