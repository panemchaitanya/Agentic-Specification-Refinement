# Agentic Specification Refinement (Team ReqHQ)

Autonomous Software Engineering (CS6.403, IIIT-H, Monsoon 2026) course project.

**Team:** Panem Chaitanya Pavan Kumar (2025204023), Anjali Yadav (2025204015), Anshul Agrawal (2025901015)

Turns a rough product description (PRD) into a complete, trace-linked
specification. An orchestrator drafts functional requirements; three
specialist critics (Security, Architecture, Performance) flag missing or
vague non-functional requirements; a gap-and-conflict check asks the user
only the questions it cannot settle; the final spec shows where every line
came from: a PRD sentence, a critic, or a human answer.

## Status

Mid-submission snapshot (6 Nov 2026). What exists and is measured today:

- **Pipeline (Stage 1):** the full PRD-to-spec loop works end to end.
  Critics are currently one LLM with narrow role prompts (baseline B2 in
  the proposal). Without a `GEMINI_API_KEY` the app runs in a
  deterministic demo mode used for development and tests; demo-mode
  output is not model-quality and is never used for evaluation.
- **UI:** guided web wizard (upload PRD -> extraction -> critic review ->
  spec download) served by the Streamlit app, plus the hosted demo below.
- **Datasets (Stage 2):** PROMISE NFR and PURE are frozen under
  `data/frozen/` with manifest, provenance and license documentation
  (`docs/datasets.md`). Dev/test discipline: test stays untouched until
  the Stage 4 evaluation.
- **Ground truth (in progress):** 61 dev documents carry LLM-suggested
  requirement labels (free-tier model, status "LLM-SUGGESTED -
  unverified, not gold"; the evaluation loader rejects them fail-closed).
  A human verification pass is underway with fixed guidelines
  (`training/ANNOTATION_GUIDELINES.md`, `training/verify_suggestions.py`,
  `docs/verification_assignment.md`); verified documents are promoted to
  FROZEN gold by `training/merge_verdicts.py` with inter-annotator
  agreement (Cohen's kappa) on a shared subsample. Until then, no gold
  claims are made.
- **Training pilot (Stage 3, feasibility only):** NFR classification
  subtask on the frozen PROMISE dev split, dev-internal stratified holdout
  (`training/train_nfr_classifier.py`, `training/pilot_results.json`).
  Measured so far: TF-IDF + logistic regression accuracy 0.744 / macro-F1
  0.679; DistilBERT CPU pilot accuracy 0.779 / macro-F1 0.504; DistilBERT
  GPU sweep (Colab T4, 3 configs): best config 6 epochs / batch 16 /
  lr 5e-5 -> accuracy 0.826 / macro-F1 0.782 in 21 s of training,
  peak CUDA 1.5 GB (weakest config 0.523 / 0.235 - lr/batch sensitive,
  recorded honestly). These are pilot-label numbers: they measure
  feasibility and the honest floor a trained model must beat, not
  hypothesis evidence for H1-H3.
- **Evaluation harness:** frozen-split runner with pre-registered H1-H3
  (`docs/evaluation.md`, decisions D1-D4), gold fail-closed loading
  (`eval/gold.py`), and run logs under `eval/runs/`. No frozen evaluation
  result is claimed yet.

**Hosted demo:** https://agentic-specification-refinement.onrender.com
runs the app in demo mode only (no API key in hosted secrets, by design).
It is a free instance: it sleeps after ~15 minutes idle and takes about a
minute to wake. Demo output shows the flow, not refinement quality.

## Known quality gaps (carried, not solved)

These are evaluation targets for Stages 2-4, not claims of quality:

- FR/NFR typing of PRD statements is a keyword heuristic
  (`orchestrator.classify_statement`): explicit quality wording ("fast",
  "secure", ...) becomes an NFR category, everything else stays FR. The
  heuristic is unvalidated - the Stage 2 extraction guidelines and
  matching rubric own the real FR/NFR boundary.
- A `vague` critic finding still turns the critic's suggested fix into a
  final NFR without asking the user (the proposal's escalation rule).
  These lines are now marked in output: `user_confirmed: false` in the
  downloaded spec and counted as `user_unconfirmed` in the trace report.
  Marking is visibility, not approval; H1/H3 measurement must count
  these separately from user-answered lines.
- "0 untraced lines" means trace coverage only (every line names a
  source). It says nothing about whether the lines are correct,
  complete, or well-typed - that is what the frozen-split evaluation
  (H1-H3) is for.

## Run

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=...        # optional; omit for demo mode
export GEMINI_MODEL=gemini-3.1-flash-lite   # optional override
streamlit run app.py
```

### Using the app

The app is a three-step wizard:

1. **Describe** - paste a PRD (or load a bundled sample) and analyse it.
2. **Review & answer** - inspect the draft FRs with their PRD traces and
   the critic findings (each finding shows the PRD sentence that caused
   it and why it matters). Only unresolved gaps become questions;
   unanswered questions enter the spec as open items.
3. **Specification** - the final trace-linked spec, grouped into FRs and
   NFR categories, downloadable as JSON or Markdown.

Without a GEMINI_API_KEY the app runs the labelled demo mode used for
development and tests.

## Test

```bash
python -m pytest tests/
```

## Architecture

```
app.py                  Streamlit UI: Input -> Questions -> Result
src/specpipe/
  schemas.py            FR/NFR, Finding, Question, Spec contracts + trace kinds
  llm.py                Gemini REST client + deterministic MockLLM
  orchestrator.py       drafts FRs, each traced to a verbatim PRD sentence
  critics.py            security / architecture / performance reviewers
  gapcheck.py           dedupe, escalation rule, targeted questions (cap 5)
  pipeline.py           analyze() + finalize(): deterministic trace-linked composition
data/sample_prds/       demo inputs
eval/                   evaluation harness: baselines B1-B4, oracle, scoring, run logs
docs/evaluation.md      fixed evaluation protocol (readouts, rubric, data discipline)
docs/datasets.md        dataset qualification: licenses, label gap, open checks
docs/compute.md         training feasibility: verified free compute options
tests/                  offline unit tests (MockLLM)
```

Escalation rule (from the submitted proposal): the human is asked only
when a critic reports missing business context (`missing_context`) or two
critics disagree (`conflict`). Vague wording the critic can fix itself
(`vague`) becomes an NFR directly, traced to that critic.

## Pre-registered hypotheses (verbatim from the submitted proposal)

- H1. The full pipeline (without human input) has higher NFR recall than
  a single LLM given the same PRD, with precision dropping by no more
  than 0.03.
- H2. SLM critics come within 0.05 NFR F1 of LLM critics in the same
  roles, at much lower token cost and latency.
- H3. Targeted questioning reaches at least 95% of the completeness of
  asking the human to review everything, using at least 50% fewer
  questions.

No evaluation results are claimed yet. The evaluation harness exists
and is smoke-tested on synthetic inputs only (plumbing, not evidence);
frozen splits, gold extraction and real runs are pending.

## Deploy

Any host that can run `streamlit run app.py` works. The app reads `GEMINI_API_KEY` from the environment or from `.streamlit/secrets.toml`; without it, it runs in the labelled demo mode.

- Generic container/VM (e.g. Render): build `pip install -r requirements.txt`, start `streamlit run app.py --server.port=$PORT --server.address=0.0.0.0`.
- Hosted instance notes: the service deploys MANUALLY (auto-deploy is off)
  so the demo stays stable during testing windows. Every deploy restarts
  the service and resets all active sessions (in-progress analyses are
  lost); the free instance also sleeps after ~15 minutes idle and takes
  about a minute to wake. Both are known hosting limitations, not app
  bugs.
- Streamlit Community Cloud: main file path `app.py`, set `GEMINI_API_KEY` in the app's Secrets.
