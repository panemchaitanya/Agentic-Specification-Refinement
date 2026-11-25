"""Parse shrunk Hypothesis counterexamples from pytest output into structured repair reports."""

from __future__ import annotations

import re

from reqhq.pbt.codegen import _pascal_ident, _safe_ident
from reqhq.pbt.models import (
    CounterexampleClassification,
    CounterexampleRepairReport,
    PBTContractDocument,
    PBTPropertyContract,
    PropertyKind,
)

_SECTION_HEADER_RE = re.compile(r"^_{3,}\s+([A-Za-z0-9_.\[\]-]+)\s+_{3,}$")
_FALSIFYING_START_RE = re.compile(
    r"^\s*(?:E\s+)?(?:Falsifying example|Failing test case):\s*(.*)$"
)
_PYTEST_E_PREFIX_RE = re.compile(r"^E\s+")
_FALSIFYING_CALL_NAME_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_ASSERT_TAG_RE = re.compile(r"\[([A-Za-z0-9_-]+)\s*\|\s*([A-Za-z0-9_-]+)\]")
_ERROR_LINE_RE = re.compile(r"^E\s+([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Warning|Unsatisfiable|FailedHealthCheck|InvalidArgument).*)$")

_STRATEGY_MISMATCH_MARKERS = (
    "unsatisfiable",
    "failedhealthcheck",
    "invalidargument",
    "filtertoomuch",
    "healthcheck",
    "strategy",
    "cannot satisfy",
    "unable to satisfy assumptions",
    "unexpected keyword argument",
    "missing 1 required positional argument",
)
_SPEC_AMBIGUITY_MARKERS = (
    "ambiguous",
    "ambiguity",
    "contradict",
    "underspecified",
    "not specified",
    "assumption",
    "spec_ambiguity",
    "conflict",
    "unclear boundary",
)


def _index_properties_by_test_name(
    doc: PBTContractDocument | None,
) -> dict[str, PBTPropertyContract]:
    if doc is None:
        return {}
    mapping: dict[str, PBTPropertyContract] = {}
    for prop in doc.properties:
        if prop.kind == PropertyKind.STATEFUL_TRANSITION:
            cls_name = f"Test{_pascal_ident(prop.property_id, fallback='State')}StateMachine"
            mapping[cls_name] = prop
            mapping[f"{cls_name}.runTest"] = prop
        else:
            mapping[f"test_{_safe_ident(prop.property_id, prefix='prop')}"] = prop
    return mapping


def _classify_counterexample(
    assertion_error: str,
    falsifying_example: str,
) -> CounterexampleClassification:
    combined = f"{assertion_error}\n{falsifying_example}".lower()
    if any(marker in combined for marker in _STRATEGY_MISMATCH_MARKERS):
        return CounterexampleClassification.STRATEGY_MISMATCH
    if any(marker in combined for marker in _SPEC_AMBIGUITY_MARKERS):
        return CounterexampleClassification.SPEC_AMBIGUITY
    return CounterexampleClassification.CODE_DEFECT


def _build_suggested_action(
    classification: CounterexampleClassification,
    test_name: str,
    property_id: str | None,
    req_id: str | None,
) -> str:
    ref_parts = [p for p in (property_id, req_id) if p]
    ref_label = f" ({' / '.join(ref_parts)})" if ref_parts else ""

    if classification == CounterexampleClassification.STRATEGY_MISMATCH:
        return (
            f"Refine Hypothesis input strategies or domain preconditions for {test_name}{ref_label} "
            "so generated inputs stay within the valid contract domain."
        )
    if classification == CounterexampleClassification.SPEC_AMBIGUITY:
        return (
            f"Escalate {test_name}{ref_label} to specification refinement to resolve ambiguous "
            "or contradictory requirement boundaries before updating the locked contract."
        )
    return (
        f"Repair the target implementation tested by {test_name}{ref_label} to satisfy the "
        "shrunk counterexample without weakening the locked PBT assertion."
    )


def _extract_error_in_block(lines: list[str], start_idx: int, end_idx: int) -> str:
    # Prefer explicit `E       <Exception>: ...` lines within the failure section.
    candidates: list[str] = []
    for raw in lines[start_idx:end_idx]:
        stripped = raw.strip()
        if _FALSIFYING_START_RE.match(raw):
            break
        match = _ERROR_LINE_RE.match(stripped)
        if match:
            candidates.append(match.group(1).strip())
        elif "AssertionError:" in stripped or "ContractViolationError:" in stripped:
            candidates.append(_PYTEST_E_PREFIX_RE.sub("", stripped).strip())
    if candidates:
        return candidates[-1]
    return "AssertionError: Hypothesis falsified property contract"


def parse_hypothesis_counterexamples(
    pytest_output: str,
    doc: PBTContractDocument | None = None,
) -> list[CounterexampleRepairReport]:
    """Parse shrunk Hypothesis `Falsifying example:` outputs from pytest logs."""
    if not pytest_output.strip():
        return []

    lines = pytest_output.splitlines()
    prop_index = _index_properties_by_test_name(doc)
    prop_by_id = (
        {prop.property_id: prop for prop in doc.properties}
        if doc is not None
        else {}
    )

    # Identify pytest failure section spans so each counterexample binds to its section's error.
    sections: list[tuple[int, int, str]] = []
    header_positions: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        hdr = _SECTION_HEADER_RE.match(line.strip())
        if hdr:
            header_positions.append((idx, hdr.group(1)))

    if header_positions:
        for pos_idx, (start_line, name) in enumerate(header_positions):
            end_line = (
                header_positions[pos_idx + 1][0]
                if pos_idx + 1 < len(header_positions)
                else len(lines)
            )
            sections.append((start_line, end_line, name))
    else:
        sections.append((0, len(lines), ""))

    reports: list[CounterexampleRepairReport] = []

    for sec_start, sec_end, sec_name in sections:
        idx = sec_start
        while idx < sec_end:
            match = _FALSIFYING_START_RE.match(lines[idx])
            if not match:
                idx += 1
                continue

            example_lines: list[str] = []
            inline_tail = match.group(1).strip()
            if inline_tail:
                example_lines.append(inline_tail)
            idx += 1

            while idx < sec_end:
                nxt = lines[idx]
                nxt_stripped = nxt.strip()
                if not nxt_stripped:
                    break
                cleaned_nxt = _PYTEST_E_PREFIX_RE.sub("", nxt_stripped).strip()
                if (
                    cleaned_nxt.startswith("You can reproduce")
                    or cleaned_nxt.startswith("Explanation:")
                    or cleaned_nxt.startswith("===")
                    or cleaned_nxt.startswith("___")
                    or cleaned_nxt.startswith("FAILED ")
                    or _FALSIFYING_START_RE.match(nxt)
                ):
                    break
                example_lines.append(cleaned_nxt)
                idx += 1

            falsifying_example = "\n".join(example_lines).strip() or "Falsifying example (unparsed)"

            inferred_name = sec_name
            if not inferred_name and example_lines:
                call_match = _FALSIFYING_CALL_NAME_RE.match(example_lines[0])
                if call_match:
                    inferred_name = call_match.group(1)
            test_name = inferred_name or "unknown_hypothesis_test"

            assertion_error = _extract_error_in_block(lines, sec_start, sec_end)

            property_id: str | None = None
            req_id: str | None = None

            tag_match = _ASSERT_TAG_RE.search(assertion_error)
            if tag_match:
                property_id = tag_match.group(1)
                req_id = tag_match.group(2)

            matched_prop = prop_index.get(test_name)
            if matched_prop is None and "." in test_name:
                matched_prop = prop_index.get(test_name.split(".", 1)[0])
            if matched_prop is None and property_id is not None:
                matched_prop = prop_by_id.get(property_id)

            if matched_prop is not None:
                property_id = property_id or matched_prop.property_id
                req_id = req_id or matched_prop.req_id

            classification = _classify_counterexample(assertion_error, falsifying_example)
            suggested_action = _build_suggested_action(
                classification,
                test_name,
                property_id,
                req_id,
            )

            reports.append(
                CounterexampleRepairReport(
                    test_name=test_name,
                    property_id=property_id,
                    req_id=req_id,
                    falsifying_example=falsifying_example,
                    assertion_error=assertion_error,
                    classification=classification,
                    suggested_action=suggested_action,
                )
            )

    return reports


__all__ = ["parse_hypothesis_counterexamples"]
