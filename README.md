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

Walking skeleton (Stage 1 of the project plan): the full PRD-to-spec loop
works end to end. Critics are currently one LLM with narrow role prompts
(baseline B2 in the proposal); fine-tuned SLM critics (hypothesis H2),
frozen-split evaluation (H1-H3) and the ground-truth study are planned
next. Without a `GEMINI_API_KEY` the app runs in a deterministic demo mode
used for development and tests; demo-mode output is not model-quality and
is never used for evaluation.

**Hosted demo:** https://agentic-specification-refinement.onrender.com
runs the app in demo mode only (no API key in hosted secrets, by design).
It is a free instance: it sleeps after ~15 minutes idle and takes about a
minute to wake. Demo output shows the flow, not refinement quality.

## Known quality gaps (carried, not solved)

These are evaluation targets for Stages 2-4, not claims of quality:

- PRD statements are currently typed by the orchestrator as FRs even when
  they are really quality statements (e.g. "The checkout must be fast."
  or "We store user profile data" show up as FR-n traced to the PRD).
  FR/NFR typing guidelines and measurement land in Stage 2.
- A `vague` critic finding turns the critic's suggested fix into a final
  NFR directly, traced to that critic, without asking the user. That
  follows the proposal's escalation rule, but the user never explicitly
  approves those lines; H1/H3 measurement must count them separately
  from user-answered lines.
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
docs/datasets.md        dataset qualification: licenses, label gap, open checks
docs/compute.md         training feasibility: verified free compute options
tests/                  offline unit tests (MockLLM)
```

Escalation rule (from the submitted proposal): the human is asked only
when a critic reports missing business context (`missing_context`) or two
critics disagree (`conflict`). Vague wording the critic can fix itself
(`vague`) becomes an NFR directly, traced to that critic.

## Pre-registered hypotheses (unchanged from the proposal)

- H1: the full pipeline (no human input) has higher NFR recall than a
  single LLM, precision dropping no more than 0.03.
- H2: SLM critics come within 0.05 NFR F1 of LLM critics, at much lower
  token cost and latency.
- H3: targeted questioning reaches at least 95% of the completeness of
  ask-everything review, with at least 50% fewer questions.

No evaluation results are claimed yet; this repo currently contains the
implementation the evaluation will run on.

## Deploy

Any host that can run `streamlit run app.py` works. The app reads `GEMINI_API_KEY` from the environment or from `.streamlit/secrets.toml`; without it, it runs in the labelled demo mode.

- Generic container/VM (e.g. Render): build `pip install -r requirements.txt`, start `streamlit run app.py --server.port=$PORT --server.address=0.0.0.0`.
- Streamlit Community Cloud: main file path `app.py`, set `GEMINI_API_KEY` in the app's Secrets.
