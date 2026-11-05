"""Download the project's public datasets and record their hashes.

Reproducible acquisition for the ReqHQ Agentic Specification Refinement
project (Stage 0 of PROJECT_PLAN.md). Downloads the pinned Zenodo files
into data/raw/ and writes data/raw/RAW_MANIFEST.json with sha256 hashes
and fetch timestamps. Re-running is safe: existing files with matching
hashes are kept, mismatches are re-downloaded and reported.

Raw downloads are NOT committed to git (see data/raw/.gitignore); the
committed artefacts are this script, the manifest and the frozen splits
produced by prepare_*.py.

Sources (all verified reachable and licensed for re-use, see
docs/datasets.md):
  - PROMISE NFR: https://zenodo.org/records/268542 (DOI 10.5281/zenodo.268542)
  - PURE:        https://zenodo.org/records/7118517 (DOI 10.5281/zenodo.7118517)
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"

# Pinned source files: (relative output path, download URL, description).
FILES = [
    ("promise_nfr/nfr.tar",
     "https://zenodo.org/api/records/268542/files/nfr.tar/content",
     "PROMISE NFR dataset (Cleland-Huang et al. 2007), Zenodo record 268542"),
    ("pure/requirements.zip",
     "https://zenodo.org/api/records/7118517/files/requirements.zip/content",
     "PURE requirements documents (79 PDFs), Zenodo record 7118517"),
    ("pure/requirements-xml.zip",
     "https://zenodo.org/api/records/7118517/files/requirements-xml.zip/content",
     "PURE XML versions (19 documents), Zenodo record 7118517"),
    ("pure/req_document.xsd",
     "https://zenodo.org/api/records/7118517/files/req_document.xsd/content",
     "PURE XML schema, Zenodo record 7118517"),
]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    manifest_path = RAW / "RAW_MANIFEST.json"
    old = {}
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text()).get("files", {})
    entries = {}
    ok = True
    for rel, url, desc in FILES:
        dest = RAW / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and rel in old and sha256_of(dest) == old[rel]["sha256"]:
            print(f"keep   {rel} (hash matches manifest)")
            entries[rel] = old[rel]
            continue
        print(f"fetch  {rel} <- {url}")
        try:
            with urllib.request.urlopen(url, timeout=600) as resp:
                dest.write_bytes(resp.read())
        except Exception as exc:  # noqa: BLE001 - report and fail the run
            print(f"ERROR downloading {url}: {exc}", file=sys.stderr)
            ok = False
            continue
        entries[rel] = {
            "sha256": sha256_of(dest),
            "bytes": dest.stat().st_size,
            "url": url,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "description": desc,
        }
        print(f"       sha256={entries[rel]['sha256']}  "
              f"bytes={entries[rel]['bytes']}")
    manifest = {
        "created_by": "training/fetch_datasets.py",
        "note": "Raw public datasets. Not committed; re-fetch with this script.",
        "files": entries,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
