"""Phase 3 Antigravity Handoff Bundler, AST Mutation Engine, and Condition A/B/C Harness."""

from __future__ import annotations

from reqhq.harness.ast_mutator import ASTMutantEngine, MutantCandidate, generate_mutants
from reqhq.harness.experiment import (
    AgentRunResult,
    CodingAgent,
    ExperimentHarness,
    MockCodingAgent,
    append_trial_result_csv,
    read_trial_results_csv,
)
from reqhq.harness.handoff import build_handoff_bundle
from reqhq.harness.models import (
    RESULTS_CSV_FIELDNAMES,
    ExperimentCondition,
    HandoffBundleManifest,
    MutantOutcome,
    MutantRecord,
    MutationReport,
    TrialResultRow,
    compute_mutation_score,
)
from reqhq.harness.mutation import MutationRunner, MutmutRunner, select_mutation_runner

__all__ = [
    "ASTMutantEngine",
    "AgentRunResult",
    "CodingAgent",
    "ExperimentCondition",
    "ExperimentHarness",
    "HandoffBundleManifest",
    "MockCodingAgent",
    "MutantCandidate",
    "MutantOutcome",
    "MutantRecord",
    "MutationReport",
    "MutationRunner",
    "MutmutRunner",
    "RESULTS_CSV_FIELDNAMES",
    "TrialResultRow",
    "append_trial_result_csv",
    "build_handoff_bundle",
    "compute_mutation_score",
    "generate_mutants",
    "read_trial_results_csv",
    "select_mutation_runner",
]
