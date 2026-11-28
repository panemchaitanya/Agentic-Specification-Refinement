"""Stage 3 Elicitation, 5 Local Critics & Delta Convergence Engine."""

from __future__ import annotations

from reqhq.elicitation.convergence import (
    cluster_and_rank_questions,
    deduplicate_findings,
    is_converged,
)
from reqhq.elicitation.critics import (
    BaseCritic,
    CriticPanel,
    FunctionalCompletenessCritic,
    LogicalConsistencyCritic,
    MAX_CRITIC_PROMPT_CHARS,
    NonFunctionalQualityCritic,
    ProcessOperationalCritic,
    TrainingAdoptionCritic,
)
from reqhq.elicitation.decomposer import (
    MAX_DECOMPOSE_PROMPT_CHARS,
    RequirementDecomposer,
)
from reqhq.elicitation.delta import DeltaDependencyTracker
from reqhq.elicitation.engine import ElicitationEngine
from reqhq.elicitation.models import (
    AssumptionRecord,
    ConvergenceGateError,
    CriticDisagreement,
    CriticDomain,
    CriticFinding,
    ElicitationQuestion,
    ElicitationState,
    FindingSeverity,
    SEVERITY_RANK,
    UserAnswer,
)

__all__ = [
    "AssumptionRecord",
    "BaseCritic",
    "ConvergenceGateError",
    "CriticDisagreement",
    "CriticDomain",
    "CriticFinding",
    "CriticPanel",
    "DeltaDependencyTracker",
    "ElicitationEngine",
    "ElicitationQuestion",
    "ElicitationState",
    "FindingSeverity",
    "FunctionalCompletenessCritic",
    "LogicalConsistencyCritic",
    "MAX_CRITIC_PROMPT_CHARS",
    "MAX_DECOMPOSE_PROMPT_CHARS",
    "NonFunctionalQualityCritic",
    "ProcessOperationalCritic",
    "RequirementDecomposer",
    "SEVERITY_RANK",
    "TrainingAdoptionCritic",
    "UserAnswer",
    "cluster_and_rank_questions",
    "deduplicate_findings",
    "is_converged",
]
