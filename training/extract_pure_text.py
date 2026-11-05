"""Extract plain text from PURE documents for labelling and evaluation.

PURE ships pdf/doc/htm/html/rtf. Extraction coverage is measured and
recorded honestly per document - a document whose text cannot be
extracted is marked failed, never silently dropped or hand-filled.

Output:
  data/frozen/pure/text/<split>/<document>.txt   (extracted text)
  data/frozen/pure/text/EXTRACTION_REPORT.json   (per-document status)

Uses local tools only (pdftotext, antiword/catdoc, unrtf, stdlib HTML
tag stripping). No network, no LLM calls.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW_ZIP = ROOT / "data" / "raw" / "pure" / "requirements.zip"
FREEZE = ROOT / "data" / "frozen" / "pure" / "FREEZE.json"
OUT = ROOT / "data" / "frozen" / "pure" / "text"


def extract_pdf(data: bytes, tmp: Path) -> str:
    tmp.write_bytes(data)
    return subprocess.run(["pdftotext", "-layout", str(tmp), "-"],
                          check=True, capture_output=True, text=True).stdout


def extract_doc(data: bytes, tmp: Path) -> str:
    tmp.write_bytes(data)
    for tool in (["antiword", str(tmp)], ["catdoc", str(tmp)]):
        try:
            return subprocess.run(tool, check=True, capture_output=True,
                                  text=True, errors="replace").stdout
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    raise RuntimeError("no working .doc extractor")


def extract_rtf(data: bytes, tmp: Path) -> str:
    tmp.write_bytes(data)
    return subprocess.run(["unrtf", "--text", str(tmp)],
                          check=True, capture_output=True, text=True).stdout


def extract_html(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return html.unescape(re.sub(r"\s+", " ", text))


def main() -> int:
    if not RAW_ZIP.exists() or not FREEZE.exists():
        print("run fetch_datasets.py and prepare_pure.py first", file=sys.stderr)
        return 1
    freeze = json.loads(FREEZE.read_text())
    tmp = Path("/tmp/pure_extract_tmp")
    report = []
    with zipfile.ZipFile(RAW_ZIP) as z:
        names = {Path(n).name: n for n in z.namelist() if not n.endswith("/")}
        for doc in freeze["documents"]:
            name, fmt, split = doc["document"], doc["format"], doc["split"]
            entry = {"document": name, "split": split, "format": fmt}
            try:
                data = z.read(names[name])
                if fmt == "pdf":
                    text = extract_pdf(data, tmp.with_suffix(".pdf"))
                elif fmt == "doc":
                    text = extract_doc(data, tmp.with_suffix(".doc"))
                elif fmt == "rtf":
                    text = extract_rtf(data, tmp.with_suffix(".rtf"))
                else:
                    text = extract_html(data)
                text = text.strip()
                if len(text) < 500:
                    raise RuntimeError(f"suspiciously short extraction ({len(text)} chars)")
                dest = OUT / split / f"{name}.txt"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text)
                entry.update(status="ok", chars=len(text),
                             text_sha256=hashlib.sha256(text.encode()).hexdigest())
            except Exception as exc:  # noqa: BLE001 - record, never drop silently
                entry.update(status="failed", error=str(exc)[:200])
            report.append(entry)
    OUT.mkdir(parents=True, exist_ok=True)
    summary = {
        "created_by": "training/extract_pure_text.py",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ok": sum(1 for e in report if e["status"] == "ok"),
        "failed": sum(1 for e in report if e["status"] == "failed"),
        "note": "Failed documents are excluded from labelling/eval until "
                "extraction is fixed; they are never hand-filled.",
        "documents": report,
    }
    (OUT / "EXTRACTION_REPORT.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"ok={summary['ok']} failed={summary['failed']}")
    for e in report:
        if e["status"] == "failed":
            print("FAILED:", e["document"], "-", e["error"][:80])
    return 0


if __name__ == "__main__":
    sys.exit(main())
