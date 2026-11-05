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
verdicts per run are re-checked by a second person; inter-rater agreement
on the audit sample is reported with the results.

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

- **H1** = B3 vs B1, per-item paired NFR scores. Supported iff the paired
  bootstrap 95% CI for mean recall(B3) - recall(B1) lies above 0 AND mean
  precision(B3) - precision(B1) >= -0.03.
- **H2** = SLM vs LLM critics in identical roles, per-item paired NFR F1.
  Supported iff mean F1(SLM) - F1(LLM) >= -0.05. Token cost and latency
  are reported as means and ratios; the proposal says "much lower"
  without a number, so no pass/fail is attached to them.
- **H3** = B3T vs B4, per-item paired. Supported iff mean
  completeness(B3T)/completeness(B4) >= 0.95 AND mean
  1 - questions(B3T)/questions(B4) >= 0.50. (0/0 completeness is scored
  1.0; 0 questions on both arms is a 1.0 reduction.)

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

## 8. Current state (6 Nov 2026)

- Harness, baselines B1/B2/B3/B3T/B4, oracle, scoring, gold/verdict
  loaders: implemented and unit-tested (30/30 with the metric tests).
- Smoke run on the two repo sample PRDs with the deterministic MockLLM:
  `eval/runs/*-sample-prds-*.jsonl`. This proves plumbing only; MockLLM
  output is rule-based demo text and the references are synthetic. It is
  NOT evaluation evidence.
- Blocked on: frozen dev/test splits and gold extraction (datasets lane),
  an LLM budget/route decision for real generation runs, and SLM critics
  for H2 (training lane).
