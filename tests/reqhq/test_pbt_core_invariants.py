"""Deterministic Hypothesis property-based tests for core ReqHQ v2 invariants.

Covers:
1. PIIScrubber round-trip reversibility & outbound zero-leakage (Phase 1).
2. HierarchicalChunker zero-truncation & lossless reconstruction invariant (Phase 1).
3. QuotaLedger 90%-ceiling monotonicity invariant (Phase 1).
4. PBT_Spec -> codegen -> ast.parse -> contract_lock tamper-evidence (Phase 3).
"""

from __future__ import annotations

import ast
import hashlib
import keyword
from pathlib import Path
import tempfile

from hypothesis import given, settings, strategies as st
import pytest

from reqhq.ingestion.chunker import HierarchicalChunker
from reqhq.ingestion.normalizer import NormalizedDocument
from reqhq.llm.pii_scrubber import PIIScrubber
from reqhq.llm.quota_ledger import ProviderQuotaLimits, QuotaLedger
from reqhq.pbt import (
    ContractTamperError,
    GuardrailEnforcement,
    PBTContractDocument,
    PBTPropertyContract,
    PropertyKind,
    dump_pbt_spec_yaml,
    export_pbt_bundle,
    load_pbt_spec_yaml,
    verify_contract_lock,
)
from reqhq.schemas.pbt import (
    HypothesisStrategySpec,
    PBTSpec,
    PropertyInvariant,
    PydanticGuardrail,
)

# Safe plain-text alphabet that avoids accidentally colliding with placeholder tokens `<...>`
_SAFE_WORD = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "),
    min_size=1,
    max_size=24,
)

_EMAIL_STRAT = st.builds(
    lambda user, dom, tld: f"{user}@{dom}.{tld}",
    user=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=3, max_size=10),
    dom=st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=3, max_size=10),
    tld=st.sampled_from(["com", "org", "net", "io"]),
)

_SSN_STRAT = st.builds(
    lambda a, b, c: f"{a:03d}-{b:02d}-{c:04d}",
    a=st.integers(min_value=100, max_value=899),
    b=st.integers(min_value=10, max_value=99),
    c=st.integers(min_value=1000, max_value=9999),
)

_IPV4_STRAT = st.builds(
    lambda a, b, c, d: f"{a}.{b}.{c}.{d}",
    a=st.integers(min_value=10, max_value=223),
    b=st.integers(min_value=0, max_value=255),
    c=st.integers(min_value=0, max_value=255),
    d=st.integers(min_value=1, max_value=254),
)

_API_KEY_STRAT = st.one_of(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=20, max_size=28).map(
        lambda s: f"sk-proj-{s}"
    ),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=20, max_size=28).map(
        lambda s: f"gsk_{s}"
    ),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=24, max_size=30).map(
        lambda s: f"AIza{s}"
    ),
)

_SECTION_BODY = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,;:-_\n"),
    min_size=5,
    max_size=220,
)


class TestPIIScrubberPBTInvariants:
    """Hypothesis PBT verifying PIIScrubber outbound redaction and exact round-trip restoration."""

    @settings(derandomize=True, max_examples=50)
    @given(
        prefix=_SAFE_WORD,
        email=_EMAIL_STRAT,
        middle=_SAFE_WORD,
        ssn=_SSN_STRAT,
        ip=_IPV4_STRAT,
        api_key=_API_KEY_STRAT,
        suffix=_SAFE_WORD,
    )
    def test_pii_scrubber_round_trip_and_zero_outbound_leak(
        self,
        prefix: str,
        email: str,
        middle: str,
        ssn: str,
        ip: str,
        api_key: str,
        suffix: str,
    ) -> None:
        scrubber = PIIScrubber()
        original = (
            f"{prefix} contact {email} {middle} ssn {ssn} host {ip} token {api_key} {suffix}"
        )
        scrubbed, mapping = scrubber.scrub(original)

        # Outbound zero-leakage invariant
        for secret in (email, ssn, ip, api_key):
            assert secret not in scrubbed
        assert len(mapping) >= 4

        # Exact string round-trip reversibility
        assert scrubber.restore(scrubbed, mapping) == original

        # Recursive container restoration invariant
        nested = {"text": scrubbed, "items": [scrubbed, (scrubbed, 123)]}
        restored_nested = scrubber.restore(nested, mapping)
        assert restored_nested == {"text": original, "items": [original, (original, 123)]}


class TestHierarchicalChunkerPBTInvariants:
    """Hypothesis PBT verifying HierarchicalChunker 0% truncation across arbitrary Markdown docs."""

    @settings(derandomize=True, max_examples=50)
    @given(
        sections=st.lists(
            st.tuples(
                st.integers(min_value=1, max_value=4),
                st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ", min_size=2, max_size=30).filter(lambda s: bool(s.strip())),
                _SECTION_BODY,
            ),
            min_size=0,
            max_size=8,
        ),
        max_chunk_chars=st.integers(min_value=10, max_value=400),
        raw_overlap=st.integers(min_value=0, max_value=32),
    )
    def test_chunker_zero_truncation_and_exact_reassembly(
        self,
        sections: list[tuple[int, str, str]],
        max_chunk_chars: int,
        raw_overlap: int,
    ) -> None:
        parts = [f"{'#' * level} {title.strip()}\n\n{body}\n" for level, title, body in sections]
        markdown = "\n".join(parts)
        doc = NormalizedDocument(
            source_path=Path("synthetic_pbt.md"),
            source_sha256=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            parser_name="plain_text_markdown",
            media_type="text/markdown",
            markdown=markdown,
            char_count=len(markdown),
            page_count=1,
            anchors=[],
        )

        # Zero-overlap exact lossless reconstruction (including empty document boundary)
        chunker = HierarchicalChunker(max_chunk_chars=max_chunk_chars, overlap_chars=0)
        result = chunker.chunk(doc)

        assert result.truncation_ratio == 0.0
        assert result.covered_chars == result.total_chars == len(markdown)
        assert "".join(c.text for c in result.chunks) == markdown
        for chunk in result.chunks:
            assert chunk.char_count == len(chunk.text) <= max_chunk_chars
            assert chunk.source_sha256 == doc.source_sha256

        # Overlapping mode also preserves 0% truncation and strict chunk size bound
        overlap = min(raw_overlap, max_chunk_chars - 1)
        overlap_res = HierarchicalChunker(
            max_chunk_chars=max_chunk_chars,
            overlap_chars=overlap,
        ).chunk(doc)
        assert overlap_res.truncation_ratio == 0.0
        assert overlap_res.covered_chars == len(markdown)
        for chunk in overlap_res.chunks:
            assert chunk.char_count <= max_chunk_chars


class TestQuotaLedgerPBTInvariants:
    """Hypothesis PBT verifying QuotaLedger ceiling monotonicity within an active window."""

    @settings(derandomize=True, max_examples=50, deadline=None)
    @given(
        rpm=st.integers(min_value=1, max_value=40),
        tpm=st.integers(min_value=50, max_value=20_000),
        ceiling_ratio=st.floats(
            min_value=0.10,
            max_value=1.00,
            allow_nan=False,
            allow_infinity=False,
        ),
        call_tokens=st.lists(st.integers(min_value=0, max_value=800), min_size=1, max_size=25),
    )
    def test_quota_ledger_90_percent_ceiling_monotonicity(
        self,
        rpm: int,
        tpm: int,
        ceiling_ratio: float,
        call_tokens: list[int],
    ) -> None:
        now = 1_700_000_000.0
        rpm_cap = round(rpm * ceiling_ratio, 6)
        tpm_cap = round(tpm * ceiling_ratio, 6)

        with tempfile.TemporaryDirectory(prefix="reqhq_pbt_quota_") as tmp:
            ledger = QuotaLedger(
                Path(tmp) / "quota.db",
                ceiling_ratio=ceiling_ratio,
                limits={
                    "gemini": ProviderQuotaLimits(
                        rpm=rpm,
                        rpd=max(rpm * 10, 100),
                        tpm=tpm,
                        tpd=max(tpm * 10, 10_000),
                        default_cooldown_seconds=60.0,
                    )
                },
                clock=lambda: now,
            )

            blocked_for_zero_est = False
            recorded_reqs = 0
            recorded_toks = 0

            for tok in call_tokens:
                allowed_before, _ = ledger.can_consume("gemini", est_tokens=tok)
                expected_allowed = (recorded_reqs + 1) <= rpm_cap and (recorded_toks + tok) <= tpm_cap
                assert allowed_before is expected_allowed

                # Zero-estimate check must be monotonically non-increasing once blocked
                allowed_min, _ = ledger.can_consume("gemini", est_tokens=0)
                if blocked_for_zero_est:
                    assert allowed_min is False
                elif not allowed_min:
                    blocked_for_zero_est = True

                if allowed_before:
                    ledger.record_usage("gemini", prompt_tokens=tok, completion_tokens=0)
                    recorded_reqs += 1
                    recorded_toks += tok
                    assert recorded_reqs <= rpm_cap
                    assert recorded_toks <= tpm_cap


_IDENT_STRAT = st.builds(
    lambda head, tail: f"{head}{tail}",
    head=st.sampled_from("abcdefghjkmnpqrstuvwxyz"),
    tail=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=2, max_size=10),
).filter(lambda s: s.isidentifier() and not keyword.iskeyword(s))


class TestPBTCodegenAndContractLockInvariants:
    """Hypothesis PBT verifying PBT_Spec -> YAML -> codegen -> ast.parse -> verify_contract_lock."""

    @settings(derandomize=True, max_examples=50, deadline=None)
    @given(
        module_name=_IDENT_STRAT,
        param_name=_IDENT_STRAT,
        prop_kind=st.sampled_from(list(PropertyKind)),
        enforcement=st.sampled_from(list(GuardrailEnforcement)),
        seed=st.integers(min_value=1, max_value=9999),
        prop_max_examples=st.integers(min_value=200, max_value=1000),
        tamper_byte_index=st.integers(min_value=0, max_value=5000),
    )
    def test_pbt_spec_codegen_ast_parse_and_tamper_evident_lock(
        self,
        module_name: str,
        param_name: str,
        prop_kind: PropertyKind,
        enforcement: GuardrailEnforcement,
        seed: int,
        prop_max_examples: int,
        tamper_byte_index: int,
    ) -> None:
        source_sha = hashlib.sha256(f"{module_name}:{seed}".encode("utf-8")).hexdigest()
        req_id = "REQ-001"
        prop_id = "PBT-001"

        strat = HypothesisStrategySpec(
            parameter_name=param_name,
            strategy_expr="st.integers(min_value=0, max_value=100)",
            description="Non-negative bounded integer input",
        )
        inv = PropertyInvariant(
            invariant_id=prop_id,
            req_id=req_id,
            title=f"Invariant for {module_name}",
            precondition=f"{param_name} >= 0",
            assertion_expr=f"{param_name} >= 0",
            strategies=[strat],
            max_examples=prop_max_examples,
        )
        guardrail = PydanticGuardrail(
            guardrail_id="GR-001",
            model_name=f"{module_name.title().replace('_', '')}Input",
            field_name=param_name,
            constraint_type="range",
            rule=">= 0",
            error_message="must be non-negative",
        )
        pbt_spec = PBTSpec(
            pbt_spec_id=f"PBT-SPEC-{seed}",
            target_module=module_name,
            req_ids=[req_id],
            invariants=[inv],
            guardrails=[guardrail],
            alloy_model_ref="core_domain.als::Inv_REQ_001",
            seed=seed,
        )
        prop_contract = PBTPropertyContract(
            property_id=prop_id,
            req_id=req_id,
            adr_id="ADR-R001",
            source_sha256=source_sha,
            alloy_ref="core_domain.als::Inv_REQ_001",
            target_symbol=f"handle_{module_name}",
            kind=prop_kind,
            title=f"Check {module_name}",
            precondition=f"{param_name} >= 0",
            assertion_expr=f"{param_name} >= 0",
            input_strategies=[strat],
            guardrail_enforcement=enforcement,
            max_examples=prop_max_examples,
        )
        doc = PBTContractDocument(
            spec=pbt_spec,
            session_id=f"sess-{seed}",
            source_sha256=source_sha,
            properties=[prop_contract],
            seed=seed,
        )

        # 1. Pure-Python YAML round-trip invariant
        yaml_text = dump_pbt_spec_yaml(doc)
        reloaded = load_pbt_spec_yaml(yaml_text)
        assert reloaded == doc

        # 2. Codegen -> ast.parse -> contract_lock verification -> 1-byte tamper rejection
        with tempfile.TemporaryDirectory(prefix="reqhq_pbt_lock_") as tmp:
            out_dir = Path(tmp) / "bundle"
            bundle = export_pbt_bundle(doc, out_dir)
            assert bundle.pbt_spec_yaml.is_file()
            assert bundle.lock_file.is_file()
            assert len(bundle.guardrail_files) >= 1
            assert len(bundle.test_files) >= 1

            for py_file in [*bundle.guardrail_files, *bundle.test_files]:
                ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))

            verified = verify_contract_lock(out_dir)
            assert "PBT_Spec.yaml" in verified

            # Mutate a single byte in the locked PBT test file and verify fail-closed rejection
            locked_test = bundle.test_files[0]
            raw = bytearray(locked_test.read_bytes())
            idx = tamper_byte_index % len(raw)
            raw[idx] ^= 0x01
            locked_test.write_bytes(bytes(raw))

            with pytest.raises(ContractTamperError):
                verify_contract_lock(out_dir)
