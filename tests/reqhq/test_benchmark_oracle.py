"""Unit and deterministic Hypothesis property-based tests for reqhq.benchmark (Phase 3)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hypothesis import given, settings, strategies as st
from pydantic import ValidationError
import pytest

from reqhq.benchmark import (
    AnnotationItem,
    CrossDomainCase,
    CrossDomainSuite,
    DEFAULT_CROSS_DOMAIN_PATH,
    EXPECTED_DOMAINS,
    FrozenGoldDocument,
    FrozenOracleSimulator,
    GapCategory,
    GapClause,
    GapInjectionResult,
    GapInjector,
    GoldGapManifest,
    KappaReport,
    NOT_SPECIFIED_REPLY,
    OracleAnswer,
    OracleEvalMetrics,
    adjudicate_annotations,
    cohens_kappa,
    compute_kappa_from_files,
    load_cross_domain_suite,
    load_frozen_promise_manifest,
    load_frozen_pure_gold,
    load_frozen_pure_manifest,
    run_cross_domain_benchmark,
    run_gap_injection_benchmark,
    validate_gold_record_dict,
)
from reqhq.llm.providers.mock import MockProvider
from reqhq.llm.router import ProviderRouter
from reqhq.synthesis.models import TriageTrack

# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------

_SAMPLE_SHA256 = "a" * 64


def _make_valid_gold_dict(
    *,
    item_id: str = "pure-0001_-_sample",
    status: str = "FROZEN - human-verified (anchored, single annotator)",
    verified_by: Any = None,
    implied_categories_verified: Any = True,
    excluded: Any = None,
    source_sha256: Any = _SAMPLE_SHA256,
    requirements: Any = None,
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "prd_path": "data/raw/pure/extracted/sample.pdf",
        "implied_categories": ["security", "performance"],
        "gold_requirements": (
            requirements
            if requirements is not None
            else [
                {
                    "id": "G1",
                    "kind": "FR",
                    "text": "The system shall authenticate users with MFA.",
                    "source": "llm-suggestion+human-verify",
                },
                {
                    "id": "human-001",
                    "kind": "security",
                    "text": "The system shall rotate signing keys every 30 days.",
                    "source": "human-added:a",
                },
            ]
        ),
        "provenance": {
            "status": status,
            "verified_by": ["a"] if verified_by is None else verified_by,
            "source_document_sha256": source_sha256,
            "implied_categories_verified": implied_categories_verified,
            "excluded": excluded,
        },
    }


def _make_sample_clauses() -> list[GapClause]:
    return [
        GapClause(
            clause_id="C1",
            category=GapCategory.NFR,
            text="Checkout settlement API must respond within 180 milliseconds at p99 load.",
            keywords=["p99", "latency", "settlement"],
        ),
        GapClause(
            clause_id="C2",
            category=GapCategory.SECURITY,
            text="All cardholder tokens shall be encrypted with AES-256-GCM hardware keys.",
            keywords=["encryption", "AES-256-GCM", "cardholder"],
        ),
        GapClause(
            clause_id="C3",
            category=GapCategory.PROCESS,
            text="Treasury adjustments above fifty thousand dollars require dual compliance sign-off.",
            keywords=["treasury", "dual compliance"],
        ),
        GapClause(
            clause_id="C4",
            category=GapCategory.TRAINING,
            text="Operations analysts must pass annual sanctions certification workshops.",
            keywords=["sanctions", "certification"],
        ),
        GapClause(
            clause_id="C5",
            category=GapCategory.BOUNDARY,
            text="Offline mobile clients shall queue at most five hundred unsynced receipts.",
            keywords=["offline", "receipts"],
        ),
        GapClause(
            clause_id="C6",
            category=GapCategory.NFR,
            text="Nightly reconciliation batches shall complete before 04:00 UTC daily.",
            keywords=["nightly", "reconciliation"],
        ),
        GapClause(
            clause_id="C7",
            category=GapCategory.SECURITY,
            text="Failed administrator logins must trigger account lockout after five attempts.",
            keywords=["lockout", "login"],
        ),
        GapClause(
            clause_id="C8",
            category=GapCategory.PROCESS,
            text="Severity-one incidents require postmortem review within forty-eight hours.",
            keywords=["postmortem", "severity-one"],
        ),
        GapClause(
            clause_id="C9",
            category=GapCategory.TRAINING,
            text="New support engineers complete shadowing drills prior to production access.",
            keywords=["shadowing", "drills"],
        ),
        GapClause(
            clause_id="C10",
            category=GapCategory.BOUNDARY,
            text="Batch file uploads exceeding twenty megabytes are rejected with HTTP 413.",
            keywords=["megabytes", "HTTP 413"],
        ),
    ]


# ---------------------------------------------------------------------------
# 1. MANDATORY DETERMINISTIC HYPOTHESIS PROPERTY-BASED TESTS
# ---------------------------------------------------------------------------

_DISTINCT_WORD = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz",
    min_size=5,
    max_size=12,
)


@st.composite
def _clause_list_strategy(draw: st.DrawFn) -> list[GapClause]:
    count = draw(st.integers(min_value=1, max_value=30))
    categories = list(GapCategory)
    clauses: list[GapClause] = []
    for idx in range(count):
        token_a = draw(_DISTINCT_WORD)
        token_b = draw(_DISTINCT_WORD)
        cat = draw(st.sampled_from(categories))
        unique_tag = f"clauseunit{idx:03d}"
        text = f"Requirement {unique_tag} enforces {token_a} and {token_b} invariant."
        clauses.append(
            GapClause(
                clause_id=f"CL-{idx + 1}",
                category=cat,
                text=text,
                keywords=[unique_tag, f"kw{idx:03d}{token_a}"],
            )
        )
    return clauses


@settings(derandomize=True, max_examples=50)
@given(
    clauses=_clause_list_strategy(),
    seed=st.integers(min_value=0, max_value=100_000),
    drop_ratio=st.floats(min_value=0.20, max_value=0.40, allow_nan=False, allow_infinity=False),
)
def test_pbt_gap_injector_ratio_bounds_and_zero_leakage(
    clauses: list[GapClause],
    seed: int,
    drop_ratio: float,
) -> None:
    injector = GapInjector(seed=seed)
    result = injector.inject(
        spec_id="spec-pbt",
        title="Property Test Specification",
        clauses=clauses,
        drop_ratio=drop_ratio,
    )
    repeat = injector.inject(
        spec_id="spec-pbt",
        title="Property Test Specification",
        clauses=clauses,
        drop_ratio=drop_ratio,
    )

    assert isinstance(result, GapInjectionResult)
    assert result.leakage_verified is True
    assert result == repeat

    expected_drop = max(1, min(len(clauses), round(len(clauses) * drop_ratio)))
    assert len(result.manifest.stripped_clauses) == expected_drop
    assert len(result.manifest.stripped_clauses) + len(result.manifest.retained_clauses) == len(clauses)
    assert result.manifest.total_clauses == len(clauses)
    assert 0.20 <= result.manifest.drop_ratio <= 0.40

    # Strict zero verbatim leakage invariant
    for stripped in result.manifest.stripped_clauses:
        assert stripped.text not in result.degraded_prd
        assert stripped.text.lower() not in result.degraded_prd.lower()

    # All retained clauses remain present in degraded_prd
    for retained in result.manifest.retained_clauses:
        assert retained.text in result.degraded_prd


@settings(derandomize=True, max_examples=50)
@given(
    alphabet_size=st.integers(min_value=1, max_value=5),
    length=st.integers(min_value=1, max_value=50),
    data=st.data(),
)
def test_pbt_cohens_kappa_mathematical_properties(
    alphabet_size: int,
    length: int,
    data: st.DataObject,
) -> None:
    alphabet = ["software", "process", "training", "hybrid", "boundary"][:alphabet_size]
    label_strat = st.lists(st.sampled_from(alphabet), min_size=length, max_size=length)
    labels_a = data.draw(label_strat)
    labels_b = data.draw(label_strat)

    # Perfect agreement == 1.0 (including single-class constant agreement for alphabet_size == 1)
    assert cohens_kappa(labels_a, labels_a) == pytest.approx(1.0)
    assert cohens_kappa(labels_b, labels_b) == pytest.approx(1.0)

    # Symmetry: kappa(a, b) == kappa(b, a)
    k_ab = cohens_kappa(labels_a, labels_b)
    k_ba = cohens_kappa(labels_b, labels_a)
    assert k_ab == pytest.approx(k_ba, abs=1e-12)

    # Strictly bounded in [-1.0, 1.0]
    assert -1.0 <= k_ab <= 1.0


@settings(derandomize=True, max_examples=50)
@given(
    clauses=_clause_list_strategy(),
    seed=st.integers(min_value=0, max_value=10_000),
    drop_ratio=st.floats(min_value=0.20, max_value=0.40, allow_nan=False, allow_infinity=False),
    unrelated_tokens=st.lists(
        st.sampled_from(
            [
                "zxqplanet",
                "qvborbit",
                "wmpnebula",
                "jklcomet",
                "vbnquasar",
                "typpulsar",
            ]
        ),
        min_size=2,
        max_size=6,
    ),
)
def test_pbt_oracle_leak_proof_unrelated_questions(
    clauses: list[GapClause],
    seed: int,
    drop_ratio: float,
    unrelated_tokens: list[str],
) -> None:
    injected = GapInjector(seed=seed).inject(
        spec_id="spec-oracle-pbt",
        title="Oracle Leak Proof Spec",
        clauses=clauses,
        drop_ratio=drop_ratio,
    )
    oracle = FrozenOracleSimulator(injected.manifest)
    question = "What does the policy state about " + " ".join(unrelated_tokens) + "?"
    ans = oracle.answer_question(question)

    assert ans.answer == NOT_SPECIFIED_REPLY
    assert ans.found_in_ground_truth is False
    assert ans.matched_clause_ids == []
    assert ans.is_redundant is False
    for stripped in injected.manifest.stripped_clauses:
        assert stripped.text not in ans.answer

    metrics = oracle.score_session()
    assert metrics.gap_recall == 0.0
    assert metrics.precision == 0.0
    assert metrics.f1 == 0.0
    assert metrics.redundant_question_rate == 0.0


@settings(derandomize=True, max_examples=50)
@given(
    clauses=_clause_list_strategy(),
    seed=st.integers(min_value=0, max_value=10_000),
    drop_ratio=st.floats(min_value=0.20, max_value=0.40, allow_nan=False, allow_infinity=False),
    unrelated_count=st.integers(min_value=0, max_value=3),
    ask_repeats=st.booleans(),
)
def test_pbt_oracle_metrics_bounded_and_convergent(
    clauses: list[GapClause],
    seed: int,
    drop_ratio: float,
    unrelated_count: int,
    ask_repeats: bool,
) -> None:
    injected = GapInjector(seed=seed).inject(
        spec_id="spec-conv-pbt",
        title="Oracle Convergence Spec",
        clauses=clauses,
        drop_ratio=drop_ratio,
    )
    oracle = FrozenOracleSimulator(injected.manifest)

    for idx in range(unrelated_count):
        miss = oracle.answer_question(f"Unrelated zxqplanet qvborbit query {idx}?")
        assert miss.answer == NOT_SPECIFIED_REPLY
        assert miss.found_in_ground_truth is False

    for hidden in injected.manifest.stripped_clauses:
        resp = oracle.answer_question(f"Explain requirement {hidden.clause_id}")
        assert resp.found_in_ground_truth is True
        assert hidden.clause_id in resp.matched_clause_ids
        assert resp.is_redundant is False

    expected_convergence = unrelated_count + len(injected.manifest.stripped_clauses)
    if ask_repeats:
        first_hidden = injected.manifest.stripped_clauses[0]
        dup = oracle.answer_question(f"Repeat details for {first_hidden.clause_id}")
        assert dup.is_redundant is True

    metrics = oracle.score_session()
    assert metrics.gap_recall == pytest.approx(1.0)
    assert 0.0 <= metrics.precision <= 1.0
    assert 0.0 <= metrics.f1 <= 1.0
    assert 0.0 <= metrics.redundant_question_rate <= 1.0
    assert metrics.questions_to_convergence == expected_convergence
    assert metrics.questions_to_convergence <= metrics.total_questions
    assert metrics.matched_gaps == metrics.total_gaps


# ---------------------------------------------------------------------------
# 2. MODELS VALIDATION UNIT TESTS
# ---------------------------------------------------------------------------


def test_models_extra_forbid_and_validators() -> None:
    # Extra field rejected across strict Pydantic models
    with pytest.raises(ValidationError):
        GapClause(
            clause_id="C1",
            category=GapCategory.NFR,
            text="Valid clause text",
            unknown_field="nope",  # type: ignore[call-arg]
        )

    # Keyword deduplication & blank filtering
    clause = GapClause(
        clause_id="  C1  ",
        category=GapCategory.SECURITY,
        text="  Must encrypt data  ",
        keywords=["  AES ", "aes", "", "   ", "TLS"],
    )
    assert clause.clause_id == "C1"
    assert clause.text == "Must encrypt data"
    assert clause.keywords == ["AES", "TLS"]

    # Blank required string rejected
    with pytest.raises(ValidationError):
        GapClause(clause_id="   ", category=GapCategory.NFR, text="text")

    # GoldGapManifest partition mismatch and duplicate clause IDs rejected
    with pytest.raises(ValidationError):
        GoldGapManifest(
            spec_id="S1",
            seed=1,
            drop_ratio=0.30,
            total_clauses=5,
            stripped_clauses=[clause],
            retained_clauses=[],
        )

    with pytest.raises(ValidationError):
        GoldGapManifest(
            spec_id="S1",
            seed=1,
            drop_ratio=0.30,
            total_clauses=2,
            stripped_clauses=[clause],
            retained_clauses=[clause],
        )

    # FrozenGoldDocument rejects invalid SHA-256 (wrong length and non-hex 64-char string)
    with pytest.raises(ValidationError):
        FrozenGoldDocument(
            item_id="doc-1",
            split="dev",
            prd_path="doc.pdf",
            source_sha256="not-a-valid-sha256",
            status="FROZEN - human-verified",
            verified_by=["a"],
            implied_categories=[],
            requirements=[{"id": "G1", "kind": "FR", "text": "x", "source": "human-added:a"}],
        )

    with pytest.raises(ValidationError, match="64-character hex SHA-256"):
        FrozenGoldDocument(
            item_id="doc-1",
            split="dev",
            prd_path="doc.pdf",
            source_sha256="z" * 64,
            status="FROZEN - human-verified",
            verified_by=["a"],
            implied_categories=[],
            requirements=[{"id": "G1", "kind": "FR", "text": "x", "source": "human-added:a"}],
        )


def test_cross_domain_suite_validator_enforces_16_cases_and_balance() -> None:
    suite = load_cross_domain_suite()
    assert len(suite.cases) == 16

    # Wrong total count
    with pytest.raises(ValidationError, match="exactly 16 cases"):
        CrossDomainSuite(version="1.0.0", cases=suite.cases[:15])

    # Duplicate case_id
    dup_cases = list(suite.cases)
    dup_cases[-1] = dup_cases[0].model_copy()
    with pytest.raises(ValidationError, match="unique"):
        CrossDomainSuite(version="1.0.0", cases=dup_cases)

    # Imbalanced one_liner / multi_paragraph
    imbalanced_kind = [c.model_copy(update={"input_kind": "one_liner"}) for c in suite.cases]
    with pytest.raises(ValidationError, match="8 one_liner and 8 multi_paragraph"):
        CrossDomainSuite(version="1.0.0", cases=imbalanced_kind)

    # Imbalanced domain counts
    imbalanced_domain = list(suite.cases)
    imbalanced_domain[-1] = imbalanced_domain[-1].model_copy(
        update={"domain": "fintech_settlements"}
    )
    with pytest.raises(ValidationError, match="exactly 4 cases per domain"):
        CrossDomainSuite(version="1.0.0", cases=imbalanced_domain)

    # Blank gold_requirements entry rejected
    with pytest.raises(ValidationError):
        CrossDomainCase(
            case_id="X-1",
            domain="fintech_settlements",
            input_kind="one_liner",
            title="Test",
            prompt_text="Prompt",
            gold_requirements=["   "],
            expected_routes={"R1": "software"},
            adr_rubric_anchors=["Anchor"],
        )


# ---------------------------------------------------------------------------
# 3. GAP INJECTOR & ORACLE SIMULATOR UNIT TESTS
# ---------------------------------------------------------------------------


def test_gap_injector_error_paths_and_leakage_detection() -> None:
    clauses = _make_sample_clauses()
    injector = GapInjector(seed=42)

    with pytest.raises(ValueError, match="spec_id"):
        injector.inject("   ", "Title", clauses)

    with pytest.raises(ValueError, match="title"):
        injector.inject("S1", "   ", clauses)

    with pytest.raises(ValueError, match="drop_ratio"):
        injector.inject("S1", "Title", clauses, drop_ratio=0.19)

    with pytest.raises(ValueError, match="drop_ratio"):
        injector.inject("S1", "Title", clauses, drop_ratio=0.41)

    with pytest.raises(ValueError, match="drop_ratio"):
        injector.inject("S1", "Title", clauses, drop_ratio=float("nan"))

    with pytest.raises(ValueError, match="drop_ratio"):
        injector.inject("S1", "Title", clauses, drop_ratio=float("inf"))

    # Exact [0.20, 0.40] endpoints succeed cleanly with zero leakage
    low_res = injector.inject("S1", "Exact Low Bound", clauses, drop_ratio=0.20)
    high_res = injector.inject("S1", "Exact High Bound", clauses, drop_ratio=0.40)
    assert len(low_res.manifest.stripped_clauses) == 2
    assert len(high_res.manifest.stripped_clauses) == 4
    assert low_res.leakage_verified is True
    assert high_res.leakage_verified is True

    with pytest.raises(ValueError, match="at least one"):
        injector.inject("S1", "Title", [])

    dup_clauses = [clauses[0], clauses[0].model_copy()]
    with pytest.raises(ValueError, match="Duplicate clause_id"):
        injector.inject("S1", "Title", dup_clauses)

    # Single-clause injection strips the 1 clause and renders empty-retained fallback
    single_res = injector.inject("S1", "Single Clause", [clauses[0]], drop_ratio=0.30)
    assert len(single_res.manifest.stripped_clauses) == 1
    assert len(single_res.manifest.retained_clauses) == 0
    assert "_No clauses retained after gap injection._" in single_res.degraded_prd

    # Verbatim leakage via title triggers ValueError
    with pytest.raises(ValueError, match="Verbatim leakage detected"):
        injector.inject(
            "S1",
            clauses[0].text,
            [clauses[0]],
            drop_ratio=0.30,
        )

    # Normalized case-insensitive whitespace leakage triggers ValueError
    altered_title = "  ".join(clauses[0].text.upper().split())
    with pytest.raises(ValueError, match="Normalized verbatim leakage detected"):
        injector.inject(
            "S1",
            altered_title,
            [clauses[0]],
            drop_ratio=0.30,
        )


def test_oracle_simulator_matching_redundancy_and_metrics() -> None:
    clauses = [
        GapClause(
            clause_id="G1",
            category=GapCategory.NFR,
            text="Checkout API latency must remain below 150ms for global requests.",
            keywords=["latency", "150ms"],
        ),
        GapClause(
            clause_id="G2",
            category=GapCategory.SECURITY,
            text="Hardware security module rotates cryptographic vault secrets weekly.",
            keywords=[],
        ),
        GapClause(
            clause_id="G3",
            category=GapCategory.BOUNDARY,
            text="Quotas.",
            keywords=[],
        ),
        GapClause(
            clause_id="G4",
            category=GapCategory.PROCESS,
            text="The system shall be.",  # stopwords only -> only reachable by clause_id
            keywords=[],
        ),
    ]
    manifest = GoldGapManifest(
        spec_id="oracle-unit",
        seed=7,
        drop_ratio=0.30,
        total_clauses=4,
        stripped_clauses=clauses,
        retained_clauses=[],
    )
    oracle = FrozenOracleSimulator(manifest)

    with pytest.raises(ValueError, match="blank"):
        oracle.answer_question("   ")

    # 1. Keyword match reveals ONLY G1 (never leaks G2/G3/G4)
    a1 = oracle.answer_question("What is the required 150ms SLA?")
    assert isinstance(a1, OracleAnswer)
    assert a1.matched_clause_ids == ["G1"]
    assert a1.found_in_ground_truth is True
    assert a1.is_redundant is False
    assert "G2" not in a1.answer

    # 2. Repeat query of G1 is marked redundant
    a2 = oracle.answer_question("Tell me about latency targets again.")
    assert a2.matched_clause_ids == ["G1"]
    assert a2.is_redundant is True

    # 3. Content-token overlap (>= 2 tokens) matches G2
    a3 = oracle.answer_question("How often are cryptographic vault keys rotated?")
    assert a3.matched_clause_ids == ["G2"]
    assert a3.is_redundant is False

    # 4. Single content token clause G3 matches when token present
    a4 = oracle.answer_question("Are there any quotas enforced?")
    assert a4.matched_clause_ids == ["G3"]
    assert a4.is_redundant is False

    # 5. Unrelated question returns NOT_SPECIFIED_REPLY
    a5 = oracle.answer_question("What color is the office lobby?")
    assert a5.answer == NOT_SPECIFIED_REPLY
    assert a5.found_in_ground_truth is False
    assert a5.is_redundant is False

    # 6. Direct clause_id match reveals G4 and achieves convergence at question 6
    a6 = oracle.answer_question("What does G4 require?")
    assert a6.matched_clause_ids == ["G4"]
    assert a6.is_redundant is False
    assert oracle.revealed_clause_ids == frozenset({"G1", "G2", "G3", "G4"})
    assert len(oracle.history) == 6

    metrics = oracle.score_session()
    assert isinstance(metrics, OracleEvalMetrics)
    assert metrics.gap_recall == pytest.approx(1.0)
    assert metrics.precision == pytest.approx(4 / 6)
    expected_f1 = 2 * (4 / 6) * 1.0 / ((4 / 6) + 1.0)
    assert metrics.f1 == pytest.approx(expected_f1)
    assert metrics.questions_to_convergence == 6
    assert metrics.redundant_question_rate == pytest.approx(1 / 6)
    assert metrics.total_questions == 6
    assert metrics.matched_gaps == 4
    assert metrics.total_gaps == 4

    # Empty manifest edge case
    empty_manifest = GoldGapManifest(
        spec_id="empty",
        seed=1,
        drop_ratio=0.25,
        total_clauses=0,
        stripped_clauses=[],
        retained_clauses=[],
    )
    empty_metrics = FrozenOracleSimulator(empty_manifest).score_session()
    assert empty_metrics.gap_recall == 0.0
    assert empty_metrics.precision == 0.0
    assert empty_metrics.f1 == 0.0
    assert empty_metrics.questions_to_convergence == 0


# ---------------------------------------------------------------------------
# 4. 16-CASE CROSS-DOMAIN BENCHMARK UNIT TESTS
# ---------------------------------------------------------------------------


def test_real_cross_domain_suite_structure_and_runner() -> None:
    assert DEFAULT_CROSS_DOMAIN_PATH.exists()
    raw_bytes = DEFAULT_CROSS_DOMAIN_PATH.read_bytes()
    assert b"\r\n" not in raw_bytes, "cases.json must use LF line endings"
    suite = load_cross_domain_suite()
    assert suite.version == "1.0.0"
    assert len(suite.cases) == 16

    expected_triage_tracks = {track.value for track in TriageTrack}
    assert expected_triage_tracks == {"software", "process", "training", "hybrid"}

    # Verify strict 4 per domain, 2 one_liner + 2 multi_paragraph per domain, and all 4 tracks
    for domain in EXPECTED_DOMAINS:
        domain_cases = [c for c in suite.cases if c.domain == domain]
        assert len(domain_cases) == 4
        assert sum(1 for c in domain_cases if c.input_kind == "one_liner") == 2
        assert sum(1 for c in domain_cases if c.input_kind == "multi_paragraph") == 2
        tracks_seen = {t for c in domain_cases for t in c.expected_routes.values()}
        assert tracks_seen == expected_triage_tracks

    # Missing file error
    with pytest.raises(FileNotFoundError):
        load_cross_domain_suite(Path("data/benchmarks/cross_domain/does_not_exist.json"))

    # Run offline benchmark (router=None)
    report = run_cross_domain_benchmark(suite, router=None, seed=42)
    assert report["suite"] == "cross_domain"
    assert report["total_cases"] == 16
    assert report["total_routes"] == 64
    assert report["overall_route_accuracy"] >= 0.90
    assert set(report["domains"].keys()) == set(EXPECTED_DOMAINS)
    assert report["input_kinds"]["one_liner"]["case_count"] == 8
    assert report["input_kinds"]["multi_paragraph"]["case_count"] == 8
    # JSON serializable check
    serialized = json.dumps(report)
    assert len(serialized) > 500

    # Also test with an explicit ProviderRouter and unprefixed gold requirement fallback
    explicit_router = ProviderRouter(
        local_provider=MockProvider(default_response=""),
        strict_local=True,
        max_local_retries=0,
        enable_cache=False,
    )
    unprefixed_cases = list(suite.cases)
    unprefixed_cases[0] = unprefixed_cases[0].model_copy(
        update={
            "gold_requirements": [
                req.split(":", 1)[1].strip() for req in unprefixed_cases[0].gold_requirements
            ]
        }
    )
    unprefixed_suite = CrossDomainSuite(version="1.0.1", cases=unprefixed_cases)
    report_explicit = run_cross_domain_benchmark(unprefixed_suite, router=explicit_router, seed=7)
    assert report_explicit["total_cases"] == 16
    assert report_explicit["overall_route_accuracy"] >= 0.90

    # Run gap-injection benchmark across all 16 cases
    gap_report = run_gap_injection_benchmark(suite, seed=42, drop_ratio=0.30)
    assert gap_report["suite"] == "gap_injection"
    assert gap_report["total_cases"] == 16
    assert gap_report["all_leakage_verified"] is True
    assert gap_report["all_negative_controls_rejected"] is True
    assert gap_report["mean_gap_recall"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 5. ADJUDICATION & COHEN'S KAPPA UNIT TESTS
# ---------------------------------------------------------------------------


def test_cohens_kappa_known_values_and_errors() -> None:
    # Single-class constant perfect agreement -> 1.0
    assert cohens_kappa(["FR", "FR", "FR"], ["FR", "FR", "FR"]) == pytest.approx(1.0)

    # Known textbook example: N=10, 8 agreements, p_o=0.8, p_e=0.5*0.5 + 0.5*0.5 = 0.5 -> k=0.6
    a = ["yes"] * 5 + ["no"] * 5
    b = ["yes"] * 4 + ["no"] + ["yes"] + ["no"] * 4
    assert cohens_kappa(a, b) == pytest.approx(0.6)

    # Complete disagreement on balanced binary labels -> p_o=0, p_e=0.5 -> k=-1.0
    assert cohens_kappa(["A", "B"], ["B", "A"]) == pytest.approx(-1.0)

    # Exact 0.0 cases: chance-level agreement (p_o=0.5, p_e=0.5) & orthogonal labels (p_o=0, p_e=0)
    assert cohens_kappa(["A", "A", "B", "B"], ["A", "B", "A", "B"]) == pytest.approx(0.0)
    assert cohens_kappa(["A", "A"], ["B", "B"]) == pytest.approx(0.0)
    assert cohens_kappa(["A", "B", "C"], ["B", "C", "A"]) == cohens_kappa(
        ["B", "C", "A"], ["A", "B", "C"]
    )

    with pytest.raises(ValueError, match="equal length"):
        cohens_kappa(["A"], ["A", "B"])

    with pytest.raises(ValueError, match="non-empty"):
        cohens_kappa([], [])


def test_adjudicate_annotations_and_files_fail_closed(tmp_path: Path) -> None:
    rater_a = [
        AnnotationItem(item_id="I1", label="software"),
        AnnotationItem(item_id="I2", label="process"),
        AnnotationItem(item_id="I3", label="training"),
        AnnotationItem(item_id="I4", label="hybrid"),
    ]
    rater_b = [
        AnnotationItem(item_id="I1", label="software"),
        AnnotationItem(item_id="I2", label="process"),
        AnnotationItem(item_id="I3", label="hybrid"),
        AnnotationItem(item_id="I4", label="hybrid"),
    ]

    report = adjudicate_annotations(rater_a, rater_b)
    assert isinstance(report, KappaReport)
    assert report.shared_count == 4
    assert report.agreed_count == 3
    assert report.disagreed_ids == ["I3"]
    assert report.observed_agreement == pytest.approx(0.75)

    # Fail-closed refusals
    with pytest.raises(ValueError, match="must not be empty"):
        adjudicate_annotations([], rater_b)

    with pytest.raises(ValueError, match="skipped=True"):
        adjudicate_annotations(
            [AnnotationItem(item_id="I1", label="software", skipped=True)],
            [AnnotationItem(item_id="I1", label="software")],
        )

    with pytest.raises(ValueError, match="verified=False"):
        adjudicate_annotations(
            [AnnotationItem(item_id="I1", label="software")],
            [AnnotationItem(item_id="I1", label="software", verified=False)],
        )

    with pytest.raises(ValueError, match="duplicate item_id"):
        adjudicate_annotations(
            [
                AnnotationItem(item_id="I1", label="software"),
                AnnotationItem(item_id="I1", label="process"),
            ],
            [AnnotationItem(item_id="I1", label="software")],
        )

    with pytest.raises(ValueError, match="mismatched item_id"):
        adjudicate_annotations(
            [AnnotationItem(item_id="I1", label="software")],
            [AnnotationItem(item_id="I2", label="software")],
        )

    # File-based computation across list JSON, {"items": ...}, {"annotations": ...}, and JSONL
    fa_list = tmp_path / "a_list.json"
    fb_items = tmp_path / "b_items.json"
    fb_ann = tmp_path / "b_ann.json"
    fa_jsonl = tmp_path / "a.jsonl"

    fa_list.write_text(
        json.dumps([item.model_dump() for item in rater_a]),
        encoding="utf-8",
    )
    fb_items.write_text(
        json.dumps({"items": [item.model_dump() for item in rater_b]}),
        encoding="utf-8",
    )
    fb_ann.write_text(
        json.dumps({"annotations": [item.model_dump() for item in rater_b]}),
        encoding="utf-8",
    )
    fa_jsonl.write_text(
        "\n".join(json.dumps(item.model_dump()) for item in rater_a) + "\n",
        encoding="utf-8",
    )

    assert compute_kappa_from_files(fa_list, fb_items) == report
    assert compute_kappa_from_files(fa_jsonl, fb_ann) == report

    # File error cases
    with pytest.raises(FileNotFoundError):
        compute_kappa_from_files(tmp_path / "missing.json", fb_items)

    empty_file = tmp_path / "empty.json"
    empty_file.write_text("   ", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        compute_kappa_from_files(empty_file, fb_items)

    bad_obj = tmp_path / "bad_obj.json"
    bad_obj.write_text(json.dumps({"other": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="items"):
        compute_kappa_from_files(bad_obj, fb_items)

    bad_scalar = tmp_path / "bad_scalar.json"
    bad_scalar.write_text("42", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported JSON structure"):
        compute_kappa_from_files(bad_scalar, fb_items)


# ---------------------------------------------------------------------------
# 6. PURE / PROMISE FAIL-CLOSED BRIDGE UNIT TESTS
# ---------------------------------------------------------------------------


def test_real_frozen_pure_and_promise_readonly_load() -> None:
    frozen_root = Path("data/frozen")

    # Default split="dev", strict_fail_closed=False -> returns the 3 human-verified FROZEN docs
    docs = load_frozen_pure_gold(frozen_root)
    doc_ids = {d.item_id for d in docs}
    assert doc_ids == {
        "pure-0000_-_cctns",
        "pure-0000_-_gamma_j",
        "pure-1999_-_dii",
    }
    for doc in docs:
        assert doc.split == "dev"
        assert doc.status.startswith("FROZEN - human-verified")
        assert len(doc.verified_by) >= 1
        assert len(doc.requirements) >= 1
        for req in doc.requirements:
            assert req["source"] == "llm-suggestion+human-verify" or req["source"].startswith(
                "human-added:"
            )

    # Strict fail-closed on dev/ rejects REVIEW-INCOMPLETE and SKELETON files
    with pytest.raises(ValueError, match="Fail-closed refusal"):
        load_frozen_pure_gold(frozen_root, split="dev", strict_fail_closed=True)

    # Test split currently contains only SKELETON docs -> returns [] without leaking unverified data
    test_docs = load_frozen_pure_gold(frozen_root, split="test", strict_fail_closed=False)
    assert test_docs == []

    # PROMISE NFR manifest loads with SHA-256 verification passing
    promise_manifest = load_frozen_promise_manifest(frozen_root, verify_hashes=True)
    assert promise_manifest["dataset"] == "PROMISE NFR"
    assert promise_manifest["stats"]["rows_dev"] == 437
    assert promise_manifest["stats"]["rows_test"] == 185

    # PURE manifest loads cleanly
    pure_manifest = load_frozen_pure_manifest(frozen_root)
    assert pure_manifest["stats"]["documents"] == 79
    assert pure_manifest["stats"]["dev_documents"] == 61


def test_validate_gold_record_dict_fail_closed_refusals(tmp_path: Path) -> None:
    # Valid baseline passes
    valid = validate_gold_record_dict(_make_valid_gold_dict())
    assert valid.item_id == "pure-0001_-_sample"
    assert len(valid.requirements) == 2

    # Non-dict root
    with pytest.raises(ValueError, match="JSON object"):
        validate_gold_record_dict(["not", "a", "dict"])  # type: ignore[arg-type]

    # Missing item_id / prd_path / provenance
    bad = _make_valid_gold_dict()
    bad["item_id"] = ""
    with pytest.raises(ValueError, match="item_id"):
        validate_gold_record_dict(bad)

    bad = _make_valid_gold_dict()
    bad["prd_path"] = ""
    with pytest.raises(ValueError, match="prd_path"):
        validate_gold_record_dict(bad)

    bad = _make_valid_gold_dict()
    bad["provenance"] = None
    with pytest.raises(ValueError, match="provenance"):
        validate_gold_record_dict(bad)

    # Non-FROZEN status
    with pytest.raises(ValueError, match="provenance.status"):
        validate_gold_record_dict(_make_valid_gold_dict(status="REVIEW-INCOMPLETE (anchored)"))

    # Missing/empty/blank verified_by
    with pytest.raises(ValueError, match="verified_by"):
        validate_gold_record_dict(_make_valid_gold_dict(verified_by=[]))

    with pytest.raises(ValueError, match="blank/non-string"):
        validate_gold_record_dict(_make_valid_gold_dict(verified_by=["a", "  "]))

    # implied_categories_verified not True
    with pytest.raises(ValueError, match="implied_categories_verified"):
        validate_gold_record_dict(_make_valid_gold_dict(implied_categories_verified=False))

    # Skipped items present in provenance.excluded
    with pytest.raises(ValueError, match="skipped"):
        validate_gold_record_dict(_make_valid_gold_dict(excluded={"skipped": ["G19"]}))

    # Missing source_document_sha256
    with pytest.raises(ValueError, match="source_document_sha256"):
        validate_gold_record_dict(_make_valid_gold_dict(source_sha256=""))

    # Empty or invalid requirements
    with pytest.raises(ValueError, match="non-empty list"):
        validate_gold_record_dict(_make_valid_gold_dict(requirements=[]))

    with pytest.raises(ValueError, match="must be an object"):
        validate_gold_record_dict(_make_valid_gold_dict(requirements=["bad"]))

    with pytest.raises(ValueError, match="missing id/kind/text"):
        validate_gold_record_dict(
            _make_valid_gold_dict(
                requirements=[{"id": "", "kind": "FR", "text": "x", "source": "human-added:a"}]
            )
        )

    # Raw unverified llm-suggestion rejected
    with pytest.raises(ValueError, match="unverified source"):
        validate_gold_record_dict(
            _make_valid_gold_dict(
                requirements=[
                    {"id": "G1", "kind": "FR", "text": "Unverified", "source": "llm-suggestion"}
                ]
            )
        )

    # Non-string or empty human-added source rejected
    with pytest.raises(ValueError, match="unverified source"):
        validate_gold_record_dict(
            _make_valid_gold_dict(
                requirements=[{"id": "G1", "kind": "FR", "text": "Bad", "source": None}]
            )
        )

    with pytest.raises(ValueError, match="unverified source"):
        validate_gold_record_dict(
            _make_valid_gold_dict(
                requirements=[{"id": "G1", "kind": "FR", "text": "Bad", "source": "human-added:"}]
            )
        )

    # Loader error paths
    with pytest.raises(ValueError, match="Unsupported split"):
        load_frozen_pure_gold(tmp_path, split="train")  # type: ignore[arg-type]

    with pytest.raises(FileNotFoundError):
        load_frozen_pure_gold(tmp_path, split="dev")

    with pytest.raises(FileNotFoundError):
        load_frozen_promise_manifest(tmp_path)

    with pytest.raises(FileNotFoundError):
        load_frozen_pure_manifest(tmp_path)

    # Synthetic PROMISE / PURE manifest error branches
    promise_dir = tmp_path / "promise_nfr"
    promise_dir.mkdir(parents=True)
    (promise_dir / "FREEZE.json").write_text(json.dumps({"dataset": "PROMISE"}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing required key"):
        load_frozen_promise_manifest(tmp_path)

    (promise_dir / "FREEZE.json").write_text(
        json.dumps(
            {
                "dataset": "PROMISE",
                "files": {"dev": {}, "test": {"sha256": _SAMPLE_SHA256}},
                "stats": {},
                "split_rule": {},
                "discipline": "frozen",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing sha256 metadata"):
        load_frozen_promise_manifest(tmp_path)

    (promise_dir / "FREEZE.json").write_text(
        json.dumps(
            {
                "dataset": "PROMISE",
                "files": {
                    "dev": {"sha256": _SAMPLE_SHA256},
                    "test": {"sha256": _SAMPLE_SHA256},
                },
                "stats": {},
                "split_rule": {},
                "discipline": "frozen",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(FileNotFoundError, match="split file missing"):
        load_frozen_promise_manifest(tmp_path)

    (promise_dir / "dev.csv").write_text("col\nval\n", encoding="utf-8")
    (promise_dir / "test.csv").write_text("col\nval\n", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        load_frozen_promise_manifest(tmp_path, verify_hashes=True)

    # verify_hashes=False skips hash check cleanly
    manifest_no_hash = load_frozen_promise_manifest(tmp_path, verify_hashes=False)
    assert manifest_no_hash["dataset"] == "PROMISE"

    pure_dir = tmp_path / "pure"
    pure_dir.mkdir(parents=True)
    (pure_dir / "FREEZE.json").write_text(json.dumps({"dataset": "PURE"}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing required key"):
        load_frozen_pure_manifest(tmp_path)
