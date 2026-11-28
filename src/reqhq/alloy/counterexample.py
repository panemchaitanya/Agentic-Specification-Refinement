"""Translate Alloy counterexample traces into concrete user-facing scenario prompts."""

from __future__ import annotations

from collections.abc import Mapping
import re

from reqhq.alloy.models import (
    AlloyRunResult,
    CounterexamplePrompt,
    ExtractedAlloyBlock,
)


def _find_assertion_req_ids(
    assertion_name: str,
    blocks: Mapping[str, ExtractedAlloyBlock],
) -> list[str]:
    pattern = re.compile(rf"^\s*(?:assert|check)\s+{re.escape(assertion_name)}\b")
    req_ids: list[str] = []
    seen: set[str] = set()

    for module_name in sorted(blocks):
        block = blocks[module_name]
        matched_lines = [
            line_no
            for line_no, line in enumerate(block.code.splitlines(), start=1)
            if pattern.match(line)
        ]
        for rid in block.req_ids_for_lines(matched_lines):
            if rid not in seen:
                seen.add(rid)
                req_ids.append(rid)

    return req_ids


def translate_counterexample(
    result: AlloyRunResult,
    assertion_name: str,
    blocks: Mapping[str, ExtractedAlloyBlock],
) -> CounterexamplePrompt:
    """Translate an Alloy counterexample trace into a step-by-step CounterexamplePrompt."""
    cleaned_assertion = assertion_name.strip() or "UnnamedAssertion"
    req_ids = _find_assertion_req_ids(cleaned_assertion, blocks)

    scenario_steps: list[str] = []
    for idx, entry in enumerate(result.counterexample_trace, start=1):
        step_num = entry.get("step", idx - 1)
        state_name = str(entry.get("state", f"State{step_num}"))
        details = [
            f"{k}={v}"
            for k, v in entry.items()
            if k not in ("step", "state") and v is not None and str(v) != ""
        ]
        if details:
            scenario_steps.append(f"Step {idx} ({state_name}): {', '.join(details)}")
        else:
            scenario_steps.append(f"Step {idx} ({state_name}): state transition observed")

    if not scenario_steps:
        fallback_line = (result.stdout or result.stderr or "").strip().splitlines()
        summary = fallback_line[0] if fallback_line else f"Assertion {cleaned_assertion} violated."
        scenario_steps.append(f"Step 1 (Counterexample): {summary}")

    req_context = (
        f" (traced to {', '.join(req_ids)})" if req_ids else ""
    )
    question = (
        f"Alloy found a counterexample violating `{cleaned_assertion}`{req_context} "
        f"in {len(scenario_steps)} step(s). Does this scenario represent a missing guardrail "
        "that must be prohibited, or an intended exception that should narrow the assertion?"
    )

    return CounterexamplePrompt(
        assertion_name=cleaned_assertion,
        req_ids=req_ids,
        scenario_steps=scenario_steps,
        question=question,
    )


__all__ = ["translate_counterexample"]
