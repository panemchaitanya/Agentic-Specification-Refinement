"""16-case cross-domain socio-technical benchmark loader and runner."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

from reqhq.benchmark.gap_injector import GapInjector
from reqhq.benchmark.models import (
    CrossDomainCase,
    CrossDomainSuite,
    EXPECTED_DOMAINS,
    ExtendedCrossDomainSuite,
    GapCategory,
    GapClause,
)
from reqhq.benchmark.oracle_sim import FrozenOracleSimulator
from reqhq.llm.providers.mock import MockProvider
from reqhq.llm.router import ProviderRouter
from reqhq.schemas.requirements import (
    RequirementGraph,
    RequirementKind,
    RequirementNode,
    RequirementStatus,
    TraceLink,
)
from reqhq.synthesis.triage import SocioTechnicalTriager

DEFAULT_CROSS_DOMAIN_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "benchmarks"
    / "cross_domain"
    / "cases.json"
)

EXTENDED_CROSS_DOMAIN_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "benchmarks"
    / "cross_domain"
    / "cases_extended.json"
)

_REQ_PREFIX_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.+)$")
_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")


def load_cross_domain_suite(path: Path | None = None) -> CrossDomainSuite:
    """Load and strictly validate the 16-case cross-domain benchmark suite."""
    target = path if path is not None else DEFAULT_CROSS_DOMAIN_PATH
    if not target.exists():
        raise FileNotFoundError(f"Cross-domain benchmark suite not found: {target}")
    raw = json.loads(target.read_text(encoding="utf-8"))
    return CrossDomainSuite.model_validate(raw)


def load_extended_cross_domain_suite(path: Path | None = None) -> ExtendedCrossDomainSuite:
    """Load and strictly validate the 48-case (8-domain) extended cross-domain suite."""
    target = path if path is not None else EXTENDED_CROSS_DOMAIN_PATH
    if not target.exists():
        raise FileNotFoundError(f"Extended cross-domain benchmark suite not found: {target}")
    raw = json.loads(target.read_text(encoding="utf-8"))
    return ExtendedCrossDomainSuite.model_validate(raw)


def _extract_req_id_and_statement(text: str, fallback_id: str) -> tuple[str, str]:
    match = _REQ_PREFIX_RE.match(text)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return fallback_id, text.strip()


def _case_to_requirement_graph(case: CrossDomainCase) -> RequirementGraph:
    """Build a valid RequirementGraph from a CrossDomainCase's gold requirements."""
    digest = hashlib.sha256(case.prompt_text.encode("utf-8")).hexdigest()
    expected_keys = list(case.expected_routes.keys())
    nodes: list[RequirementNode] = []

    for idx, raw_req in enumerate(case.gold_requirements):
        fallback_id = expected_keys[idx] if idx < len(expected_keys) else f"R{idx + 1}"
        req_id, statement = _extract_req_id_and_statement(raw_req, fallback_id)
        nodes.append(
            RequirementNode(
                req_id=req_id,
                title=f"{case.case_id} {req_id}",
                statement=statement,
                kind=RequirementKind.FUNCTIONAL,
                status=RequirementStatus.PROPOSED,
                priority="P1",
                trace_links=[
                    TraceLink(
                        source_sha256=digest,
                        source_uri=f"benchmark://cross_domain/{case.case_id}",
                        anchor=req_id,
                        excerpt=statement[:160],
                    )
                ],
                tags=[case.domain, case.input_kind],
            )
        )

    return RequirementGraph(
        graph_id=f"bench-{case.case_id.lower()}",
        nodes=nodes,
        edges=[],
    )


def _build_offline_router() -> ProviderRouter:
    return ProviderRouter(
        local_provider=MockProvider(default_response=""),
        strict_local=True,
        max_local_retries=0,
        enable_cache=False,
    )


def run_cross_domain_benchmark(
    suite: CrossDomainSuite,
    *,
    router: ProviderRouter | None = None,
    seed: int = 42,
) -> dict[str, Any]:
    """Run socio-technical triage across all 16 cross-domain cases and score route accuracy."""
    effective_router = router if router is not None else _build_offline_router()
    triager = SocioTechnicalTriager(effective_router)

    domain_stats: dict[str, dict[str, Any]] = {
        domain: {
            "case_count": 0,
            "matched_routes": 0,
            "total_routes": 0,
            "route_accuracy": 0.0,
        }
        for domain in EXPECTED_DOMAINS
    }
    input_kind_stats: dict[str, dict[str, Any]] = {
        "one_liner": {"case_count": 0, "matched_routes": 0, "total_routes": 0, "route_accuracy": 0.0},
        "multi_paragraph": {
            "case_count": 0,
            "matched_routes": 0,
            "total_routes": 0,
            "route_accuracy": 0.0,
        },
    }
    expected_track_dist: dict[str, int] = {
        "software": 0,
        "process": 0,
        "training": 0,
        "hybrid": 0,
    }
    predicted_track_dist: dict[str, int] = {
        "software": 0,
        "process": 0,
        "training": 0,
        "hybrid": 0,
    }

    case_reports: list[dict[str, Any]] = []
    total_matched = 0
    total_routes = 0

    for case in suite.cases:
        graph = _case_to_requirement_graph(case)
        plan = triager.triage(graph, session_id=f"bench-{case.case_id}-{seed}")

        predicted_routes: dict[str, str] = {
            item.req_id: item.track.value for item in plan.items
        }
        matched = 0
        case_total = len(case.expected_routes)
        for req_id, expected_track in case.expected_routes.items():
            expected_track_dist[expected_track] += 1
            pred = predicted_routes.get(req_id)
            if pred in predicted_track_dist:
                predicted_track_dist[pred] += 1
            if pred == expected_track:
                matched += 1

        accuracy = (matched / case_total) if case_total > 0 else 0.0
        total_matched += matched
        total_routes += case_total

        d_stat = domain_stats[case.domain]
        d_stat["case_count"] += 1
        d_stat["matched_routes"] += matched
        d_stat["total_routes"] += case_total

        k_stat = input_kind_stats[case.input_kind]
        k_stat["case_count"] += 1
        k_stat["matched_routes"] += matched
        k_stat["total_routes"] += case_total

        case_reports.append(
            {
                "case_id": case.case_id,
                "domain": case.domain,
                "input_kind": case.input_kind,
                "title": case.title,
                "expected_routes": dict(case.expected_routes),
                "predicted_routes": predicted_routes,
                "matched_routes": matched,
                "total_routes": case_total,
                "route_accuracy": round(accuracy, 4),
                "adr_rubric_anchor_count": len(case.adr_rubric_anchors),
            }
        )

    for stats in (*domain_stats.values(), *input_kind_stats.values()):
        stats["route_accuracy"] = round(
            (stats["matched_routes"] / stats["total_routes"]) if stats["total_routes"] > 0 else 0.0,
            4,
        )

    overall_accuracy = (total_matched / total_routes) if total_routes > 0 else 0.0
    return {
        "suite": "cross_domain",
        "suite_version": suite.version,
        "seed": seed,
        "total_cases": len(suite.cases),
        "matched_routes": total_matched,
        "total_routes": total_routes,
        "overall_route_accuracy": round(overall_accuracy, 4),
        "domains": domain_stats,
        "input_kinds": input_kind_stats,
        "track_distribution": {
            "expected": expected_track_dist,
            "predicted": predicted_track_dist,
        },
        "cases": case_reports,
    }


def _track_to_gap_category(track: str) -> GapCategory:
    mapping = {
        "software": GapCategory.NFR,
        "process": GapCategory.PROCESS,
        "training": GapCategory.TRAINING,
        "hybrid": GapCategory.SECURITY,
    }
    return mapping.get(track, GapCategory.BOUNDARY)


def _extract_keywords(text: str, limit: int = 3) -> list[str]:
    tokens = [tok.lower() for tok in _TOKEN_RE.findall(text) if len(tok) >= 6]
    unique: list[str] = []
    for tok in tokens:
        if tok not in unique:
            unique.append(tok)
        if len(unique) >= limit:
            break
    return unique


def run_gap_injection_benchmark(
    suite: CrossDomainSuite | None = None,
    *,
    seed: int = 42,
    drop_ratio: float = 0.30,
) -> dict[str, Any]:
    """Run deterministic gap injection + leak-proof oracle simulation across benchmark cases."""
    active_suite = suite if suite is not None else load_cross_domain_suite()
    injector = GapInjector(seed=seed)

    case_results: list[dict[str, Any]] = []
    recalls: list[float] = []
    precisions: list[float] = []
    f1s: list[float] = []

    for case in active_suite.cases:
        expected_keys = list(case.expected_routes.keys())
        clauses: list[GapClause] = []
        for idx, raw_req in enumerate(case.gold_requirements):
            fallback_id = expected_keys[idx] if idx < len(expected_keys) else f"R{idx + 1}"
            req_id, statement = _extract_req_id_and_statement(raw_req, fallback_id)
            track = case.expected_routes.get(req_id, "software")
            clauses.append(
                GapClause(
                    clause_id=f"{case.case_id}-{req_id}",
                    category=_track_to_gap_category(track),
                    text=statement,
                    keywords=_extract_keywords(statement),
                )
            )

        injected = injector.inject(
            spec_id=case.case_id,
            title=case.title,
            clauses=clauses,
            drop_ratio=drop_ratio,
        )
        oracle = FrozenOracleSimulator(injected.manifest)

        # Probe each hidden clause with a targeted query plus one unrelated negative control.
        for hidden in injected.manifest.stripped_clauses:
            oracle.answer_question(f"What is specified for {hidden.clause_id}?")
        negative_control = oracle.answer_question(
            "What is the botanical watering cadence for ornamental orchid greenhouses?"
        )

        metrics = oracle.score_session()
        recalls.append(metrics.gap_recall)
        precisions.append(metrics.precision)
        f1s.append(metrics.f1)

        case_results.append(
            {
                "case_id": case.case_id,
                "domain": case.domain,
                "total_clauses": injected.manifest.total_clauses,
                "stripped_count": len(injected.manifest.stripped_clauses),
                "retained_count": len(injected.manifest.retained_clauses),
                "leakage_verified": injected.leakage_verified,
                "negative_control_rejected": not negative_control.found_in_ground_truth,
                "metrics": metrics.model_dump(mode="json"),
            }
        )

    count = len(case_results)
    return {
        "suite": "gap_injection",
        "suite_version": active_suite.version,
        "seed": seed,
        "drop_ratio": drop_ratio,
        "total_cases": count,
        "all_leakage_verified": all(r["leakage_verified"] for r in case_results),
        "all_negative_controls_rejected": all(
            r["negative_control_rejected"] for r in case_results
        ),
        "mean_gap_recall": round(sum(recalls) / count, 4) if count else 0.0,
        "mean_precision": round(sum(precisions) / count, 4) if count else 0.0,
        "mean_f1": round(sum(f1s) / count, 4) if count else 0.0,
        "cases": case_results,
    }


__all__ = [
    "DEFAULT_CROSS_DOMAIN_PATH",
    "EXTENDED_CROSS_DOMAIN_PATH",
    "load_cross_domain_suite",
    "load_extended_cross_domain_suite",
    "run_cross_domain_benchmark",
    "run_gap_injection_benchmark",
]
