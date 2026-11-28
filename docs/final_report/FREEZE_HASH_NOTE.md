# FREEZE.json hash note

Section 10 of `live_release/COMPREHENSIVE_EXPERIMENT_REPORT.md` quotes SHA-256 values
for the two frozen-dataset manifests. They do not match the bytes committed in git.
The cause is line endings: the report hashed Windows (CRLF) checkouts, while git stores
the files with LF endings. The report's values are kept unchanged; this note maps them.

| File | Report value (CRLF checkout) | Committed bytes (LF) |
| --- | --- | --- |
| `data/frozen/pure/FREEZE.json` | `44c8e018a73b3fe93be5c77f8dfbd936d73edb5c8540fa02e0b8d220b4001a43` | `b5e17abb8d4b3402d5bbfa173bb622d46a5c4dbd8adc7a3ba40f730a8cd19ddb` |
| `data/frozen/promise_nfr/FREEZE.json` | `0f774dc8452817f2e73226afa0bb589930e55cc258238d29d685f8f564f2bff4` | `3ebb0218995db192125042250a4fabfe6f12673315c7cbc7174fb04597401b2a` |

Check: converting the committed LF bytes to CRLF reproduces each report value exactly
(full 64-hex equality, checked 28 Nov 2026 on a Linux checkout).
`tests/reqhq/test_freeze_hash_note.py` asserts this, so the mapping cannot drift.

This explains the mismatch only. It does not validate the experiment run itself.
