"""Unit & deterministic Hypothesis PBT tests for SRS heading promotion, section triage & semantic chunking."""

from __future__ import annotations

from pathlib import Path

from hypothesis import given, settings, strategies as st
import pytest

from reqhq.ingestion import (
    PDFTextCleaner,
    SectionRole,
    SemanticChunkConfig,
    SemanticSRSChunker,
    TFIDFConceptVectorizer,
    classify_section_role,
    cluster_requirements_into_batches,
    cosine_similarity,
    deduplicate_requirements,
    promote_srs_headings,
)
from reqhq.ingestion.semantic_chunker import _is_standalone_title_heading, _split_into_paragraphs
from reqhq.schemas.requirements import (
    RequirementKind,
    RequirementNode,
    RequirementStatus,
    TraceLink,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PURE_DEV_DIR = REPO_ROOT / "data" / "frozen" / "pure" / "text" / "dev"
_DUMMY_SHA = "a" * 64


def _make_node(
    req_id: str,
    title: str,
    statement: str,
    *,
    kind: RequirementKind = RequirementKind.FUNCTIONAL,
    anchor: str = "sec:1",
    tags: list[str] | None = None,
) -> RequirementNode:
    return RequirementNode(
        req_id=req_id,
        title=title,
        statement=statement,
        kind=kind,
        priority="P1",
        status=RequirementStatus.DRAFT,
        trace_links=[
            TraceLink(
                source_sha256=_DUMMY_SHA,
                source_uri="file://srs.txt",
                anchor=anchor,
                excerpt=statement[:80],
            )
        ],
        tags=tags or [],
    )


class TestHeadingPromotionAndSectionTriage:
    """Unit tests for IEEE-830 heading promotion and deterministic section-role triage."""

    def test_promote_srs_headings_preserves_normative_sentences_and_promotes_hierarchy(self) -> None:
        assert promote_srs_headings("") == ""
        assert _is_standalone_title_heading("", prev_blank=True, next_blank=True) is False
        assert _is_standalone_title_heading("Actor:", prev_blank=True, next_blank=False) is False
        assert _is_standalone_title_heading("The system shall login", prev_blank=True, next_blank=True) is False
        assert _is_standalone_title_heading("One Two Three Four Five Six Seven", prev_blank=True, next_blank=True) is False
        assert _is_standalone_title_heading("of and the", prev_blank=True, next_blank=True) is False
        assert _is_standalone_title_heading("Access Module", prev_blank=True, next_blank=False) is True

        raw = (
            "# Existing Heading\n\n"
            "1. Introduction\n"
            "1.1 Purpose\n"
            "This project defines the telemetry portal.\n\n"
            "3.2.1 Audio Recording\n"
            "4. The System must allow users to authenticate with MFA.\n\n"
            "A1 Registration\n"
            "Chapter 2: Security\n\n"
            "Access Module\n"
            "The system shall enforce RBAC permissions.\n"
        )
        promoted = promote_srs_headings(raw)
        assert "# 1. Introduction" in promoted
        assert "## 1.1 Purpose" in promoted
        assert "### 3.2.1 Audio Recording" in promoted
        assert "## A1 Registration" in promoted
        assert "# Chapter 2: Security" in promoted
        assert "## Access Module" in promoted
        assert "\n4. The System must allow users to authenticate with MFA." in promoted

    def test_classify_section_role_covers_all_4_roles_and_normative_safety(self) -> None:
        role_g, n_g = classify_section_role("1.3 Definitions, Acronyms, and Abbreviations", "| HTML | Web markup |")
        assert role_g == SectionRole.GLOSSARY_ONLY and n_g == 0

        role_b, n_b = classify_section_role("1.4 References", "IEEE 830-1998 standard reference.")
        assert role_b == SectionRole.BACKGROUND_REFERENCE and n_b == 0

        role_c, n_c = classify_section_role("2.1 Assumptions and Constraints", "Runs on Linux servers.")
        assert role_c == SectionRole.CONSTRAINTS_ASSUMPTIONS and n_c == 0

        role_r, n_r = classify_section_role("3.1 Functional Requirements", "The system shall log logins.")
        assert role_r == SectionRole.REQUIREMENT_DENSE and n_r == 1

        # Safety shield: a section titled "Purpose" containing normative modals is NEVER skipped!
        role_safe, n_safe = classify_section_role(
            "1.1 Purpose", "The system shall authenticate users and must log all audit events."
        )
        assert role_safe == SectionRole.REQUIREMENT_DENSE and n_safe == 2

        # Single normative modal in an untitled section routes as REQUIREMENT_DENSE
        role_one, n_one = classify_section_role("Miscellaneous Notes", "The portal must validate tokens.")
        assert role_one == SectionRole.REQUIREMENT_DENSE and n_one == 1

        # Generic non-normative section falls back to CONSTRAINTS_ASSUMPTIONS
        role_misc, n_misc = classify_section_role("Miscellaneous Notes", "General narrative notes only.")
        assert role_misc == SectionRole.CONSTRAINTS_ASSUMPTIONS and n_misc == 0


class TestTFIDFAndSemanticChunking:
    """Unit tests for TF-IDF + RE concept similarity, valley splitting, clustering, and deduplication."""

    def test_tfidf_concept_synonyms_and_cosine_edge_cases(self) -> None:
        vec = TFIDFConceptVectorizer()
        assert vec.vectorize([]) == []
        vecs = vec.vectorize([
            "User login authentication password RBAC",
            "Operator MFA credential encryption security",
            "Database backup failover uptime recovery",
            "   ",
        ])
        assert len(vecs) == 4
        assert vecs[3] == {}
        assert cosine_similarity({}, vecs[0]) == 0.0
        assert cosine_similarity(vecs[0], vecs[1]) > cosine_similarity(vecs[0], vecs[2])
        # Asymmetric vector length branch
        assert cosine_similarity({"a": 1.0, "b": 0.0}, {"a": 1.0}) == pytest.approx(1.0)
        assert _split_into_paragraphs("A\n\nB") == ["A", "B"]

    def test_semantic_chunker_splits_valleys_packs_siblings_and_skips_glossary_background(self) -> None:
        chunker = SemanticSRSChunker(SemanticChunkConfig(target_chunk_chars=600, min_chunk_chars=150))
        empty_plan = chunker.plan_document("", doc_id="empty")
        assert empty_plan.total_chunks == 0
        assert empty_plan.llm_char_reduction_pct == 0.0

        # Build a multi-section doc with glossary, references, sibling req sections,
        # a huge single paragraph (>600 chars), and a multi-topic large section (>600 chars)
        huge_para = (
            "The system shall verify cryptographic signatures on every telemetry packet before ingestion. " * 10
        ).strip()
        long_sec_paras = "\n\n".join(
            [
                "The system shall authenticate operators using MFA and enforce strict RBAC password policies across all endpoints.",
                "The authentication module must lock accounts after five consecutive failed login attempts and alert security admins.",
                "The database backup engine shall replicate snapshots across three regional availability zones with automatic failover.",
                "Disaster recovery drills must verify zero data loss and restore full service availability within sixty seconds.",
                "Audit trails shall record immutable timestamps for every compliance review and export signed CSV reports.",
            ]
        )
        doc = (
            "Opening preamble paragraph before any heading.\n\n"
            "1.1 Purpose\n"
            "This document describes the product vision.\n\n"
            "1.3 Definitions, Acronyms, and Abbreviations\n"
            "| MFA | Multi-Factor Authentication |\n"
            "| RBAC | Role-Based Access Control |\n\n"
            "1.4 References\n"
            "IEEE 830 standard specification.\n\n"
            "2.1 Assumptions\n"
            "Operators have modern web browsers.\n\n"
            "3.1 Short Feature A\n"
            "The system shall support export to CSV.\n\n"
            "3.2 Short Feature B\n"
            "The system shall support export to JSON.\n\n"
            "3.3 Large Multi-Topic Security and Recovery\n"
            f"{long_sec_paras}\n\n"
            "3.4 Giant Single Paragraph\n"
            f"{huge_para}\n"
        )
        plan = chunker.plan_document(doc, doc_id="trial-doc")
        assert plan.total_sections >= 7
        assert plan.skipped_chunks >= 2  # Glossary + Background/References skipped!
        assert plan.llm_routed_chunks >= 3
        assert plan.llm_char_reduction_pct > 0.0
        assert all(c.char_count <= 600 for c in plan.chunks)
        assert SectionRole.GLOSSARY_ONLY.value in plan.role_counts
        assert SectionRole.BACKGROUND_REFERENCE.value in plan.role_counts
        assert SectionRole.REQUIREMENT_DENSE.value in plan.role_counts

        # Exercise skip flags turned off + giant unbroken token without ". " cut point
        no_skip_chunker = SemanticSRSChunker(
            SemanticChunkConfig(
                target_chunk_chars=400,
                min_chunk_chars=100,
                skip_glossary_llm=False,
                skip_background_llm=False,
            )
        )
        unbroken_plan = no_skip_chunker.plan_document(
            "1.3 Glossary\n" + ("word " * 110).strip() + "\n\n1.4 References\nRef text.",
            doc_id="no-skip",
        )
        assert unbroken_plan.skipped_chunks == 0
        assert unbroken_plan.llm_routed_chunks >= 2
        # Verify empty-paragraph guard when section body is only whitespace (covered via internal helper)
        from reqhq.ingestion.semantic_chunker import TriagedSection

        blank_sec = TriagedSection(
            section_index=0,
            heading_path=["Empty"],
            title="Empty",
            level=1,
            role=SectionRole.REQUIREMENT_DENSE,
            route_to_llm=True,
            char_count=0,
            normative_sentence_count=0,
            text="",
        )
        assert no_skip_chunker._split_section_by_semantic_valleys(blank_sec) == []

    def test_cluster_requirements_into_batches_co_locates_related_topics(self) -> None:
        with pytest.raises(ValueError, match="batch_size must be >= 1"):
            cluster_requirements_into_batches([], batch_size=0)
        assert cluster_requirements_into_batches([], batch_size=5) == []

        nodes = [
            _make_node("REQ-01", "MFA Login", "The system shall enforce MFA login authentication and RBAC.", kind=RequirementKind.SECURITY),
            _make_node("REQ-02", "Chart Theme", "The GUI interface shall support high-contrast WCAG screen themes."),
            _make_node("REQ-03", "Latency SLA", "The API shall achieve p95 latency under 200ms at high throughput.", kind=RequirementKind.NON_FUNCTIONAL),
            _make_node("REQ-04", "Password Lockout", "The system shall lock user credentials after failed password login attempts.", kind=RequirementKind.SECURITY),
            _make_node("REQ-05", "Screen Navigation", "The GUI navigation menu shall comply with WCAG accessibility guidelines."),
            _make_node("REQ-06", "Throughput Target", "The service shall maintain 5000 RPS throughput under peak latency SLA.", kind=RequirementKind.NON_FUNCTIONAL),
        ]
        # Small list <= batch_size returns single batch
        assert len(cluster_requirements_into_batches(nodes[:3], batch_size=5)) == 1

        batches = cluster_requirements_into_batches(nodes, batch_size=2)
        assert len(batches) == 3
        # Security pair (REQ-01 & REQ-04) must land in the SAME critic batch!
        first_batch_ids = {n.req_id for n in batches[0]}
        assert first_batch_ids == {"REQ-01", "REQ-04"}

    def test_deduplicate_requirements_merges_near_duplicates_and_preserves_tracelinks(self) -> None:
        with pytest.raises(ValueError, match="similarity_threshold"):
            deduplicate_requirements([], similarity_threshold=0.2)
        single = [_make_node("REQ-01", "MFA", "The system shall enforce MFA login.")]
        assert deduplicate_requirements(single) == (single, 0)

        nodes = [
            _make_node(
                "REQ-01",
                "Enforce MFA Authentication",
                "The system shall enforce MFA authentication for all operator sessions.",
                anchor="sec:1.1",
                tags=["overview"],
            ),
            _make_node(
                "REQ-02",
                "Enforce MFA Authentication",
                "The system shall enforce MFA authentication for all operator sessions.",
                anchor="sec:3.4",
                tags=["detailed", "overview"],
            ),
            _make_node(
                "REQ-03",
                "Enforce MFA Authentication",
                "The system shall enforce MFA authentication for all operator sessions.",
                anchor="sec:3.4",  # duplicate anchor skipped cleanly
                tags=["detailed"],
            ),
            _make_node(
                "REQ-04",
                "Backup Failover",
                "The database shall run automated daily backups across regional zones.",
                anchor="sec:4.2",
                tags=["reliability"],
            ),
        ]
        deduped, merged = deduplicate_requirements(nodes, similarity_threshold=0.85)
        assert merged == 2
        assert len(deduped) == 2
        mfa_node = deduped[0]
        assert {tl.anchor for tl in mfa_node.trace_links} == {"sec:1.1", "sec:3.4"}
        assert mfa_node.tags == ["overview", "detailed"]

    def test_real_pure_dev_space_fractions_and_cctns_semantic_plan(self) -> None:
        cleaner = PDFTextCleaner()
        chunker = SemanticSRSChunker()

        sf_clean = cleaner.clean_file(PURE_DEV_DIR / "2001 - space fractions.pdf.txt")
        sf_plan = chunker.plan_document(sf_clean.cleaned_text, doc_id=sf_clean.doc_id)
        assert SectionRole.GLOSSARY_ONLY.value in sf_plan.role_counts
        assert sf_plan.skipped_chunks >= 2
        assert sf_plan.llm_char_reduction_pct >= 20.0

        cc_clean = cleaner.clean_file(PURE_DEV_DIR / "0000 - cctns.pdf.txt")
        cc_plan = chunker.plan_document(cc_clean.cleaned_text, doc_id=cc_clean.doc_id)
        assert cc_plan.llm_routed_chunks >= 8
        assert all(c.char_count <= 3400 for c in cc_plan.chunks)


class TestSemanticChunkerPBT:
    """Deterministic Hypothesis property-based tests for semantic chunking & batch clustering."""

    @settings(derandomize=True, max_examples=50, deadline=None)
    @given(
        st.lists(
            st.text(
                alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters=" ."),
                min_size=8,
                max_size=60,
            ),
            min_size=1,
            max_size=12,
        ),
        st.integers(min_value=1, max_value=5),
    )
    def test_pbt_cluster_batches_is_exact_lossless_partition(
        self,
        statements: list[str],
        batch_size: int,
    ) -> None:
        nodes = [
            _make_node(f"REQ-{i:03d}", f"Title {i}", s.strip() or f"Requirement clause {i}", anchor=f"a:{i}")
            for i, s in enumerate(statements, start=1)
        ]
        batches = cluster_requirements_into_batches(nodes, batch_size=batch_size)
        assert all(1 <= len(b) <= batch_size for b in batches)
        flattened_ids = [n.req_id for b in batches for n in b]
        assert sorted(flattened_ids) == sorted(n.req_id for n in nodes)

        deduped, merged = deduplicate_requirements(nodes, similarity_threshold=0.90)
        assert len(deduped) + merged == len(nodes)
        preserved_anchors = {tl.anchor for n in deduped for tl in n.trace_links}
        assert preserved_anchors == {f"a:{i}" for i in range(1, len(nodes) + 1)}
