# Dataset qualification notes (Stage 0 / Stage 2)

Status: downloaded, inspected, and FROZEN (6 Nov 2026). Raw files live in
`data/raw/` (not committed; re-fetch with `training/fetch_datasets.py`,
hashes in `data/raw/RAW_MANIFEST.json`). Frozen splits and manifests live
in `data/frozen/`. Split-by-project and the test-set freeze are DONE
before any experiment or prompt tuning touches evaluation data.

## PROMISE NFR - critic training/eval labels

- Mirror: https://zenodo.org/records/268542 (DOI 10.5281/zenodo.268542),
  Cleland-Huang, Mazrouee, Liguo, Port (2007).
  Also at tera-PROMISE:
  https://openscience.us/repo/requirements/requirements-other/nfr.html
- License: the Zenodo record metadata says CC-BY 4.0, but the `nfr.arff`
  file header itself asserts CC-BY-SA 3.0 (share-alike). We treat the
  stricter CC-BY-SA 3.0 as governing any derived files we distribute,
  keep attribution, and note that redistribution of our derived split
  files inherits the share-alike term. Use inside this private course
  repo is fine under either.
- Content verified: 625 sentence-level rows, labels F plus NFR types
  (A, L, LF, MN, O, PE, SC, SE, US, FT, PO). The archive also ships
  NFR-to-FR trace files for 10 projects (`P(1-10)_NFR_FR_trace/`), a
  possible future trace-supervision source.
- RESOLVED: **by-project split is possible** - the arff carries a
  `ProjectID` attribute (15 projects). Frozen split:
  `data/frozen/promise_nfr/{dev.csv,test.csv,FREEZE.json}` - dev 437 rows
  (11 projects), test 185 rows (projects 1, 3, 4, 7), deterministic
  hash-ranked rule recorded in FREEZE.json.
- RESOLVED: **deduplication** - 3 within-project exact duplicates removed;
  0 cross-project duplicate texts, so the by-project split has no exact-
  text leakage. Caveat: rare classes FT/MN/PO are absent from the test
  split (<= 13 rows each overall); per-class support is recorded and must
  be reported in the final evaluation.
- LABEL GAP (unchanged, real work): these labels train an NFR
  *classifier component*, not a full critic. The critics need contextual
  supervision this dataset does not contain: given PRD context plus a
  draft requirement set, identify a *missing or vague* NFR, point to the
  responsible PRD sentence, and give a reason. The label format and
  protocol are fixed in `training/ANNOTATION_GUIDELINES.md`; the
  Architecture critic has no public label source at all (hand annotation
  only).

## PURE - PRD-style source documents

- https://zenodo.org/records/7118517 (DOI 10.5281/zenodo.7118517) -
  79 public requirements documents, 34,268 sentences.
- License: CC-BY 4.0, with the curator's own caveat: documents were
  gathered from web sources and the curator is not aware of license or
  IP claims on the requirements. Treat provenance per document as
  "public web document", credit the dataset, and drop any document the
  curator removes.
- Frozen: `data/frozen/pure/FREEZE.json` - per-document sha256, formats
  (pdf/doc/htm/html/rtf), deterministic by-document split: 61 dev /
  18 test documents. Discrepancy found: the record advertises 19 XML
  versions; the zip contains 18. XML-to-document name matching is
  normalised-containment best effort (naming differs between archives);
  details in FREEZE.json.
- Text access: `training/extract_pure_text.py` converts all 79 documents
  to text under `data/frozen/pure/text/<split>/` (79 ok / 0 failed,
  per-document hashes in EXTRACTION_REPORT.json).

## LLM pre-labeling (owner-approved 6 Nov 2026)

`training/llm_prelabel.py` pre-labels the gold skeletons with the free
Gemini route (owner approved the free-model labeling decision, ~12:07
IST). Output lands in `data/frozen/gold_llm_suggestions/<split>/` with
provenance.status "LLM-SUGGESTED - unverified, not gold": eval/gold.py's
whitelist rejects these, and only a human verification + adjudication
pass (annotate_critic.py, two-extractor protocol, kappa recorded) can
promote any of it to FROZEN gold. First pass: 61/61 dev docs, 1601
suggestions, 18 docs truncated at 120k chars (flagged per file), logs in
eval/runs/PRELABEL-dev-*. Labels are free-tier quality by declaration and
upgradeable before 30 Nov.

## KEPs / Rust RFCs / PEPs - PRD-to-spec pair candidates

- https://github.com/kubernetes/enhancements,
  https://github.com/rust-lang/rfcs, https://github.com/python/peps
- Builder: `training/build_pairs.py` (blobless git clone, no API keys).
  Pair = document at its adding commit (proposal side, rename-FOLLOWED
  so repo restructures cannot leak post-acceptance content into the
  proposal) vs the same document at HEAD (spec side).
- Leakage guard verified by inspection: an unfollowed build dated
  pre-2010 PEPs at the Sept 2023 `peps/` mass-move commit, i.e. the
  "proposal" side silently contained final text; `--follow` resolves to
  true origins (e.g. PEP 279 -> Feb 2002).
- Status: 30-pair pilot sample in `data/frozen/pairs/pilot_sample.json`
  with a hand-validation checklist. Candidate pairs only, not gold:
  pairing quality and oracle-answer leakage stay open research questions
  until the sample is hand-validated; full freeze (by-proposal split,
  `data/frozen/pairs/{dev,test}/<item>.prd.txt/.spec.txt` matching
  `eval/gold.py` `prd_path`) happens after validation.
