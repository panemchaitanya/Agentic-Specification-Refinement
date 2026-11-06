# Evaluation protocol (Stage 2/4)

Status: protocol FIXED before any evaluation data exists (6 Nov 2026).
The harness (`eval/`) runs and is smoke-tested on synthetic inputs; no
frozen split, no gold extraction and no real-model runs exist yet, so this
repo claims no H1-H3 results. Changing any rule below after seeing results
would break the registered-report claim; changes go through the whole team
and are recorded here with a date.

Hypotheses are verbatim from the submitted proposal (section 3,
Pre-registered Hypotheses):

- **H1.** The full pipeline (without human input) has higher NFR recall
  than a single LLM given the same PRD, with precision dropping by no more
  than 0.03.
- **H2.** SLM critics come within 0.05 NFR F1 of LLM critics in the same
  roles, at much lower token cost and latency.
- **H3.** Targeted questioning reaches at least 95% of the completeness of
  asking the human to review everything, using at least 50% fewer
  questions.

## 1. Baselines (proposal section 4, operationalized)

| Arm | Code | Definition |
| --- | ---- | ---------- |
| B1 single LLM | `eval/baselines.py::b1_single_llm` | One prompt turns the PRD into FRs + NFRs in one shot. No critics, no questions. Output is untraced by design (`trace_kind="baseline_b1"`); only kind/text are scored. |
| B2 orchestrator + LLM critics | `b2_orchestrator_critics` | Draft FRs plus the critic set; EVERY finding's suggestion is applied as an NFR. No gap check, no human loop. |
| B3 pipeline, no human | `b3_pipeline_no_human` | The real pipeline (`analyze` composition + `finalize`) with no answers. Escalated findings stay `OPEN:` placeholders. |
| B3T targeted + oracle | `b3_targeted_with_oracle` | The H3 targeted arm: pipeline questions answered by the oracle. |
| B4 ask-everything | `b4_ask_everything` | EVERY finding (including `vague`) becomes a question with no MAX_QUESTIONS cap; the oracle answers all. |

Ablations ("each critic removed in turn") pass a `critic_names` subset to
the B2/B3/B3T/B4 builders, e.g. `("architecture", "performance")` drops
the security critic.

Baseline-semantics stability: B2's "apply every suggestion" behavior is
implemented inside `eval/baselines.py` and does NOT change when the
product pipeline evolves - comparisons stay pinned to the recorded code
version. B3/B3T deliberately track the real pipeline (`finalize`), so
their semantics move with the coordinator's core changes (e.g. the
planned critic-suggestion approval step); every run log records the git
code version, and runs across a core change are never pooled into one
comparison. Any core change that alters escalation or composition must be
flagged to the evaluation lane before the next run batch.

Why B2 and B3 differ: B2 applies every critic suggestion blindly, so its
NFR count is maximal and its precision is the interesting number. B3 keeps
the pipeline's escalation discipline, so unresolved context gaps stay
visible as OPEN items instead of being silently filled. That difference
is exactly the design choice H1/H3 test.

## 2. Matching rubric (fixed; 20% of matches hand-audited)

A generated requirement and a gold requirement **match** when ALL hold:

- **R1 same intent.** Both require the same system behavior or property;
  rephrasing, reordering and tone changes are fine.
- **R2 same kind/class.** FR matches FR; an NFR matches only its own
  category (security/performance/scalability/reliability/usability/other).
- **R3 no material numeric/scope change.** A matched pair must not differ
  in a stated quantity, threshold or scope (e.g. "2s p95" vs "5s p95" is
  NOT a match). An unmatched numeric difference makes the generated line
  a near-miss and is recorded, not silently ignored.
- **R4 OPEN lines never match.** `OPEN:` placeholders carry no content;
  they are excluded from the predicted set for PRF and reported via
  `open_rate`.

Matching is one-to-one (`eval/scoring.py::score_item`): only the first
pair for a gold id counts; extra preds matched to the same gold count as
unmatched and the item is flagged `many_to_one` for audit. 20% of match
verdicts per run are re-checked by a second person, sampled
deterministically by `eval/audit.py::sample_matches_for_audit` (seeded,
at least one match per item); inter-rater agreement on the audit sample
is reported with the results.

## 3. Metrics

From `src/specpipe/metrics.py` (stable, coordinator-owned):

- Precision/recall/F1 for FRs and NFRs **separately**, per item, from
  rubric verdicts.
- Completeness: share of PRD-implied NFR categories (security,
  performance, scalability, reliability, usability, other) the spec
  covers; OPEN lines do not count (`eval/scoring.py::spec_completeness`).
- Human questions per PRD (`questions_asked` in the run log).
- Every comparison repeated 3 times; paired bootstrap 95% CIs on per-item
  score differences (`bootstrap_ci`).

## 4. Hypothesis readouts (fixed rules, in `eval/scoring.py`)

Each rule below says what traces verbatim to the proposal and what is a
transparent pre-data operationalization. Nothing here tightens or weakens
a hypothesis silently; every operational choice is named and was fixed
before any evaluation data exists.

- **H1** = B3 vs B1, per-item paired NFR scores. Supported iff the paired
  bootstrap 95% CI for mean recall(B3) - recall(B1) lies above 0 AND mean
  precision(B3) - precision(B1) >= -0.03.
  - Traces to proposal: the comparison arms, the recall direction, the
    0.03 precision-drop bound (verbatim), and bootstrap CIs with paired
    tests (section 4).
  - Operationalization: the proposal says "higher recall"; we require the
    95% CI to exclude 0 rather than only a positive mean difference -
    this is the pre-registered significance bar implied by "report
    bootstrap confidence intervals", and it is stricter, never looser.
    The raw mean differences are reported alongside either way.
- **H2** = SLM vs LLM critics in identical roles, per-item paired NFR F1.
  Supported iff mean F1(SLM) - F1(LLM) >= -0.05. Token cost and latency
  are reported as means and ratios; the proposal says "much lower"
  without a number, so no pass/fail is attached to them.
  - Traces to proposal: arms, F1 gap 0.05 (verbatim), cost/latency
    reporting.
  - Operationalization: the pass/fail uses the mean difference (CI
    reported); "much lower" stays descriptive because the proposal sets
    no threshold.
- **H3** = B3T vs B4, per-item paired. Supported iff mean
  completeness(B3T)/completeness(B4) >= 0.95 AND mean
  1 - questions(B3T)/questions(B4) >= 0.50. (0/0 completeness is scored
  1.0; 0 questions on both arms is a 1.0 reduction.)
  - Traces to proposal: 95% completeness and 50% fewer questions
    (verbatim).
  - Operationalization: we average per-item ratios rather than taking
    ratios of aggregate sums; the per-item pairing matches the proposal's
    paired-test design. The 0/0 conventions are arbitrary but fixed here
    pre-data.

Negative and null results are reported as measured; if H1 fails the
critic ablations identify the responsible component (proposal section 5).

## 5. Oracle protocol and leakage guard

The simulated human (`eval/oracle.py::ReferenceOracle`) answers ONLY from
the item's reference spec, one highest-overlap line per question, and
returns "" when nothing in the reference is relevant. Every answer is
logged with the reference line ids it drew on and a verbatim-overlap
ratio. This deterministic oracle is **extractive**, so it is a worst-case
upper bound for phrasing leakage into the spec (a real or LLM-paraphrased
oracle would leak less); `oracle_audit` in the run log reports the
verbatim-copy share per run, a sample is hand-audited, and the limitation
is stated with H3 results. The classmate study (proposal, if time allows)
is the real-human check.

## 6. Data discipline

- Split by project; the test set is frozen (file hashes + date recorded
  by the datasets lane) BEFORE any experiment or prompt tuning. All
  prompt and design iteration happens on the dev split only.
- Gold files and match-verdict files follow the formats in
  `eval/gold.py`; gold carries extractor provenance including Cohen's
  kappa from the two independent extractors.
- Contamination guard (proposal): documents dated after the model's
  training cutoff are preferred and reported as a separate subset.
- Bounded-scope rule (6 Nov 2026, D5 candidate): the LLM pre-labeler saw
  at most the first 120,000 chars of each document
  (`training/llm_prelabel.py` MAX_DOC_CHARS; 18 of 61 dev documents
  exceed this). Human review pages exactly that same range
  (`training/verify_suggestions.py`), and every verdict records
  `review_scope` (chars presented vs LLM scope). Therefore any dev-split
  scoring run MUST bound the pipeline input to the same 120,000-char
  prefix per document: never score full-document pipeline output against
  prefix-scoped gold. Such runs are labeled "bounded dev evaluation
  (120k-char prefix scope)". For the 43 documents at or under 120k chars
  the bounded scope IS the full document. The frozen full-document
  protocol above is unchanged and is the only protocol valid for the
  final TEST evaluation; the bound is a dev-stage correctness repair,
  not a protocol change.

## 7. Run logs and reproducibility

`eval/harness.py::run_experiment` writes append-only JSONL to
`eval/runs/`: a header line (baseline, dataset/freeze id, repeats, seed,
critic subset, code version, timestamp) and one line per (item, repeat)
with requirement counts, NFR categories, OPEN rate, questions asked/
answered, completeness (when implied categories exist), oracle audit, and
LLM cost/latency.

Cost note: `GeminiLLM` exposes each response's `usageMetadata` via
`.last_usage`, and `LoggingLLM` records per-call token counts when present
(char volumes and latency always). H2's token-cost claim additionally
needs SLM-side measurements from the training lane.

## 8. Trace fidelity (quality metric, not a hypothesis)

Trace coverage ("every line names a source") is already guaranteed by the
pipeline; whether the named source is REAL is not checked anywhere.
`eval/validity.py` measures it per run: `fr_trace_verbatim_rate` (share of
PRD-traced FRs whose trace_ref is a strictly verbatim PRD quote,
whitespace-normalized, case-sensitive) and `finding_trace_verbatim_rate`
(same for critic findings' PRD pointers). Both land in every run log.
Reported alongside H1-H3; a low rate means trace claims overstate
grounding. Whether the pipeline should validate/repair/drop hallucinated
traces itself is a coordinator core decision, flagged here pre-data.

## 9. Deviation register (implementation vs proposal, recorded openly)

- **D1 - MAX_QUESTIONS=5 cap.** The proposal's rule is "a question goes to
  the human only when a critic reports missing business context or two
  critics disagree" - with no cap. The pipeline's gap check caps targeted
  questions at 5. Effect on H3: the targeted arm may ask fewer questions
  than the proposal rule implies (helping the 50%-reduction side) while
  dropping escalated findings (hurting completeness). Handling: every run
  log records `n_escalated` alongside `questions_asked`, so dropped
  escalations are visible; H3 results report both, and if any item has
  n_escalated > questions_asked the item is flagged in the results table.
  DECISION (coordinator, 6 Nov 2026, pre-data): the cap STAYS as a
  product decision; the evaluation arms run with it and H3 results
  disclose it explicitly.

- **D2 - Conflict detection is self-declared, not cross-critic.** The
  proposal's escalation example ("an encryption requirement that breaks a
  latency target") implies the gap-and-conflict check COMPARES findings
  across critics. The implementation has each critic review in isolation
  (a critic sees only the PRD and draft FRs, never other critics'
  findings, verified against src/specpipe/critics.py), so kind=conflict
  only appears when one critic speculates about another's requirement.
  Effect on H3: a whole escalation trigger may under-fire in real runs,
  understating questions on the targeted arm. Run logs record per-kind
  finding counts (`finding_kinds`), so conflict rates are measurable
  per run; if conflicts stay near zero on the dev split, that is evidence
  for the coordinator to implement cross-critic comparison in the gap
  check (core change, coordinator-owned) before the frozen run.
  DECISION (coordinator, 6 Nov 2026, pre-data): implemented
  `gapcheck.detect_cross_critic_conflicts` - a bounded deterministic
  pass: a finding pair from different critics is flagged conflict only
  when the suggestions carry opposing directive keywords (fixed table)
  AND share a target (same PRD sentence or common content word). Low
  recall by design: it catches explicit tension and cannot prove none
  exists, so under-firing is reduced, not eliminated; conflict rates stay
  measurable via `finding_kinds` in every run log.

- **D3 - Trace fidelity is measured eval-side, not repaired pipeline-side.**
  PRD-sentence citations from a real model can be hallucinated (not
  verbatim). `eval/validity.py` measures verbatim rates
  (`fr_trace_verbatim_rate`, `finding_trace_verbatim_rate`,
  whitespace-normalized, case-sensitive) in every run log. DECISION
  (coordinator, 6 Nov 2026, pre-data): the pipeline does NOT validate,
  repair or drop unverified traces for now - silent repair would change
  outputs differently across arms and bias H1-H3. If dev-split verbatim
  rates come out low, a pipeline-side *visible* flag (mark, never
  silently drop) is the candidate follow-up, recorded here in advance.

- **D4 - Mid-submission gold: anchored human verification over LLM
  suggestions, not the proposal's two-blind-extractor design.**
  The proposal's ground-truth protocol is two independent human
  extractions + adjudication + kappa. For the 6 Nov mid-submission the
  team verifies LLM-suggested labels instead (accept/reject/edit/add per
  suggestion, fixed guidelines in training/ANNOTATION_GUIDELINES.md),
  because pre-labels already exist for all 61 dev documents and anchored
  review is ~12 min/doc vs hours per blind extraction. DECISION
  (coordinator, 6 Nov 2026, pre-data): (a) suggestion files carry status
  "LLM-SUGGESTED - unverified" and are rejected fail-closed by
  eval/gold.py; (b) only verdict files from named human annotators
  (training/verify_suggestions.py) promote a document to FROZEN gold via
  training/merge_verdicts.py; (c) a length-stratified shared subsample of
  8 dev documents (2 LLM-truncated) is verified independently by both
  annotators; pairwise Cohen's kappa on accept/reject decisions is
  computed and recorded in gold provenance, and shared-doc conflicts
  resolve by majority vote (ties keep the item out); (d) a second free
  model (different model family version) re-extracts the shared 8 as a
  cross-check pre-filter and LLM-vs-LLM agreement prior; (e) coverage and
  the single- vs double-verified split are reported verbatim with any
  evaluation numbers produced under this protocol. Deviation from the
  proposal is declared here, measured, and upgradeable to the full
  two-blind-extractor design before the 30 Nov final.

## 10. Evidence labels (use these exact words in reports)

- **smoke** - deterministic MockLLM runs (`run_smoke.py`): plumbing only,
  never cited as results.
- **pilot** - real-model runs on synthetic or dev-split data to measure
  feasibility (cost, latency, failure modes): informs decisions, is not
  hypothesis evidence.
- **evaluation** - runs on the frozen test split under this protocol: the
  only evidence cited for H1-H3.
- **oracle evidence** - from the automated ReferenceOracle: repeatable,
  but a simulated and extractive upper bound on human cooperation and on
  phrasing leakage; always labeled "simulated human (oracle)".
- **human evidence** - from actual people (extractors, match auditors,
  the optional classmate study): the only source for kappa, match-audit
  agreement, and any real-user claim.

## 11. Current state (6 Nov 2026)

- Harness, baselines B1/B2/B3/B3T/B4, oracle, scoring, gold/verdict
  loaders: implemented and unit-tested (30/30 with the metric tests).
- Smoke run on the two repo sample PRDs with the deterministic MockLLM:
  `eval/runs/*-sample-prds-*.jsonl`. This proves plumbing only; MockLLM
  output is rule-based demo text and the references are synthetic. It is
  NOT evaluation evidence.
- `eval/run_pilot.py` - bounded free-route feasibility pilot (synthetic
  PRDs only, 40-call hard budget, aborts clean on LLM error, labeled
  pilot). Ready to run the moment the route/scale is approved.
- Blocked on: frozen dev/test splits and gold extraction (datasets lane),
  an LLM budget/route/scale decision for real generation runs, and SLM
  critics for H2 (training lane).
