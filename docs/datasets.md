# Dataset qualification notes (Stage 0 / Stage 2)

Status: sources verified reachable and licenses checked (6 Nov 2026).
Nothing downloaded, split, or frozen yet. Split-by-project and the test-set
freeze (file hashes + date) must happen before any experiment or prompt
tuning touches evaluation data.

## PROMISE NFR - critic training/eval labels

- Mirror: https://zenodo.org/records/268542 (DOI 10.5281/zenodo.268542),
  Cleland-Huang, Mazrouee, Liguo, Port (2007).
- License: Creative Commons Attribution 4.0 (CC-BY 4.0) - re-use allowed
  with credit. Verified on the Zenodo page.
- Also at tera-PROMISE:
  https://openscience.us/repo/requirements/requirements-other/nfr.html
- Content: sentence-level labels, F (functional) plus NFR types
  (availability, legal, look-and-feel, maintainability, operational,
  performance, scalability, security, usability, fault tolerance,
  portability).
- LABEL GAP (from the proposal review): these labels train an NFR
  *classifier component*, not a full critic. The proposed critics need
  contextual supervision this dataset does not contain: given PRD context
  plus a draft requirement set, identify a *missing or vague* NFR, point
  to the responsible PRD sentence, and give a reason. Producing those
  labels (hand annotation, or LLM-assisted labeling with hand
  verification) is budgeted work. The Architecture critic has no obvious
  public label source at all.
- Open checks: deduplication quality and whether a by-project split maps
  cleanly onto the project's frozen splits - unknown until inspected.

## PURE - PRD-style source documents

- https://zenodo.org/records/7118517 - 79 public requirements documents,
  34,268 sentences (PDFs; the record advertises 19 in XML but the zip contains 18 - discrepancy recorded by the freeze).
- License: the Zenodo record metadata says CC-BY 4.0, but the license
  file header inside the artifact asserts CC-BY-SA 3.0. The stricter
  in-artifact terms govern our use: CC-BY-SA 3.0, attribution recorded
  in data/frozen/. Curator's own caveat stands: documents were gathered
  from web sources and the curator is not aware of license or IP claims
  on the requirements; copyright risk was minimized by using non-recent
  data and sampling. Treat provenance per document as "public web
  document", credit the dataset, and drop any document the curator
  removes.

## KEPs / Rust RFCs / PEPs - PRD-to-spec pair candidates

- https://github.com/kubernetes/enhancements,
  https://github.com/rust-lang/rfcs, https://github.com/python/peps
- Candidate pairs only, not gold: proposal-to-final-spec pairing quality,
  and oracle-answer leakage risk, are open research questions. The oracle
  log (which spec lines each answer drew on) is the leakage guard.
