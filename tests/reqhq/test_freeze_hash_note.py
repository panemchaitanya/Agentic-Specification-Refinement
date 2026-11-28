"""Pin the CRLF/LF mapping documented in docs/final_report/FREEZE_HASH_NOTE.md."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

CASES = [
    (
        "data/frozen/pure/FREEZE.json",
        "44c8e018a73b3fe93be5c77f8dfbd936d73edb5c8540fa02e0b8d220b4001a43",
    ),
    (
        "data/frozen/promise_nfr/FREEZE.json",
        "0f774dc8452817f2e73226afa0bb589930e55cc258238d29d685f8f564f2bff4",
    ),
]


def _lf(raw: bytes) -> bytes:
    return raw.replace(b"\r\n", b"\n")


@pytest.mark.parametrize(("rel", "report_sha"), CASES)
def test_report_hash_is_the_crlf_form_of_the_committed_manifest(rel: str, report_sha: str) -> None:
    raw = _lf((ROOT / rel).read_bytes())
    assert hashlib.sha256(raw.replace(b"\n", b"\r\n")).hexdigest() == report_sha


@pytest.mark.parametrize(("rel", "report_sha"), CASES)
def test_note_lists_both_hashes(rel: str, report_sha: str) -> None:
    note = (ROOT / "docs/final_report/FREEZE_HASH_NOTE.md").read_text(encoding="utf-8")
    lf_sha = hashlib.sha256(_lf((ROOT / rel).read_bytes())).hexdigest()
    assert report_sha in note
    assert lf_sha in note
