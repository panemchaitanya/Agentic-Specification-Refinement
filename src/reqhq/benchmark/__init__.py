"""Phase 3 Gap-Injection Oracle, Cross-Domain Benchmark, Adjudication & Frozen PURE/PROMISE Bridge."""

from __future__ import annotations

from reqhq.benchmark.adjudication import (
    adjudicate_annotations,
    cohens_kappa,
    compute_kappa_from_files,
)
from reqhq.benchmark.cross_domain import (
    DEFAULT_CROSS_DOMAIN_PATH,
    load_cross_domain_suite,
    run_cross_domain_benchmark,
    run_gap_injection_benchmark,
)
from reqhq.benchmark.gap_injector import GapInjector
from reqhq.benchmark.models import (
    AnnotationItem,
    CrossDomainCase,
    CrossDomainSuite,
    DomainName,
    EXPECTED_DOMAINS,
    FrozenGoldDocument,
    GapCategory,
    GapClause,
    GapInjectionResult,
    GoldGapManifest,
    InputKind,
    KappaReport,
    OracleAnswer,
    OracleEvalMetrics,
    RouteTrack,
)
from reqhq.benchmark.oracle_sim import FrozenOracleSimulator, NOT_SPECIFIED_REPLY
from reqhq.benchmark.pure_bridge import (
    load_frozen_promise_manifest,
    load_frozen_pure_gold,
    load_frozen_pure_manifest,
    validate_gold_record_dict,
)

__all__ = [
    "AnnotationItem",
    "CrossDomainCase",
    "CrossDomainSuite",
    "DEFAULT_CROSS_DOMAIN_PATH",
    "DomainName",
    "EXPECTED_DOMAINS",
    "FrozenGoldDocument",
    "FrozenOracleSimulator",
    "GapCategory",
    "GapClause",
    "GapInjectionResult",
    "GapInjector",
    "GoldGapManifest",
    "InputKind",
    "KappaReport",
    "NOT_SPECIFIED_REPLY",
    "OracleAnswer",
    "OracleEvalMetrics",
    "RouteTrack",
    "adjudicate_annotations",
    "cohens_kappa",
    "compute_kappa_from_files",
    "load_cross_domain_suite",
    "load_frozen_promise_manifest",
    "load_frozen_pure_gold",
    "load_frozen_pure_manifest",
    "run_cross_domain_benchmark",
    "run_gap_injection_benchmark",
    "validate_gold_record_dict",
]
