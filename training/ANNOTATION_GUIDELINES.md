# Annotation guidelines: ground truth and critic supervision

Team ReqHQ - Agentic Specification Refinement. These guidelines define the
two labelling jobs the evaluation depends on, the exact file formats, and
the quality protocol (two independent labellers, adjudication, Cohen's
kappa). They exist before any labelling starts, because disagreements are
expected and must be resolvable without re-labelling everything.

## Job 1: FR/NFR extraction from reference specs (evaluation gold)

The binding labelling contract is `docs/extraction-guidelines.md`
(evaluation lane): definitions, the six-category taxonomy, and the seven
boundary rules. Apply it verbatim - do not re-invent local variants.

This lane's responsibility in Job 1 is the data side: frozen items to
extract from (PURE test/dev documents, validated pairs) and assembling
the gold files from the two independent extractions + adjudication in
`eval/gold.py` format, with `provenance.extractor_agreement_kappa`
recorded per item and `freeze_id` pointing at the freeze manifest.

## Job 2: contextual critic supervision (SLM training labels)

PROMISE NFR labels *sentences*; the critics need *contextual* labels that
no public dataset provides (docs/datasets.md label gap). One JSONL record
per labelled finding, aligned to `src/specpipe/schemas.py` `Finding`:

```json
{
  "item_id": "pure-dev-0000-cctns",
  "prd_sentence": "The system shall be used by field officers.",
  "draft_frs": ["..."],
  "finding": {
    "critic": "security",
    "category": "security",
    "kind": "missing_context",
    "issue": "No statement of who may access citizen records",
    "suggestion": "Access to citizen records is restricted to authorised officers via role-based access control.",
    "reason": "PII is present and the PRD names multiple user roles"
  }
}
```

### Labelling rules

1. `critic` in {security, architecture, performance}; report ONLY that
   critic's angle, exactly like the runtime critics.
2. `kind` follows the proposal escalation rule: `missing_context` (needs
   business input only the PRD author has), `vague` (present but
   unquantified), `conflict` (clashes with another finding).
3. `prd_sentence` must be a verbatim quote from the item's PRD text.
4. A finding needs all of issue, suggestion, reason; no empty fields.
5. If the draft FRs already cover the angle adequately, emit NO finding.
   Negative examples (no finding) are as valuable as positive ones.

### Sources and honest limits

- Security and performance: partial seeding possible from PROMISE NFR
  sentences in context (a PRD sentence plus a labelled SE/PE requirement
  suggests a gap when the draft lacks it). Still needs hand verification.
- Architecture: NO public label source exists. Hand annotation only.
- LLM-assisted pre-labelling may draft candidates, but EVERY label is
  hand-verified before use; unverified model output is not training data.
- Same two-labeller + adjudication + kappa protocol as Job 1.

## What is NOT done yet (kept honest)

- No human extraction or critic labels exist yet. The file formats,
  scripts and frozen splits are ready; the two-extractor pass is team
  work scheduled with Anjali and Anshul (project plan work split).
- Model-generated text is never recorded as human gold or as verified
  critic supervision.
