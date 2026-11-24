"""Strict Pydantic v2 schemas for Python Hypothesis + Pydantic guardrail PBT specs."""

from __future__ import annotations

import keyword
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _require_identifier(value: str, field_label: str) -> str:
    cleaned = value.strip()
    if not cleaned.isidentifier() or keyword.iskeyword(cleaned):
        raise ValueError(
            f"{field_label} must be a valid non-keyword Python identifier, got {value!r}"
        )
    return cleaned


class HypothesisStrategySpec(BaseModel):
    """Specification of a single Hypothesis strategy parameter."""

    model_config = ConfigDict(extra="forbid")

    parameter_name: str = Field(..., min_length=1)
    strategy_expr: str = Field(..., min_length=1)
    description: str = ""
    constraints: list[str] = Field(default_factory=list)

    @field_validator("parameter_name")
    @classmethod
    def _validate_param_name(cls, value: str) -> str:
        return _require_identifier(value, "parameter_name")

    @field_validator("strategy_expr")
    @classmethod
    def _validate_strategy_expr(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("strategy_expr must not be blank")
        return cleaned


class PropertyInvariant(BaseModel):
    """A single property-based test invariant bound to a requirement ID."""

    model_config = ConfigDict(extra="forbid")

    invariant_id: str = Field(..., min_length=1)
    req_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    precondition: str = ""
    assertion_expr: str = Field(..., min_length=1)
    strategies: list[HypothesisStrategySpec] = Field(default_factory=list)
    max_examples: int = Field(default=100, ge=1, le=10000)

    @field_validator("invariant_id", "req_id", "title", "assertion_expr")
    @classmethod
    def _validate_non_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be blank")
        return cleaned

    @model_validator(mode="after")
    def _validate_unique_strategy_params(self) -> PropertyInvariant:
        seen: set[str] = set()
        for strat in self.strategies:
            if strat.parameter_name in seen:
                raise ValueError(
                    f"Duplicate strategy parameter_name {strat.parameter_name!r} in invariant {self.invariant_id!r}"
                )
            seen.add(strat.parameter_name)
        return self


class PydanticGuardrail(BaseModel):
    """A runtime Pydantic v2 guardrail constraint derived from requirements."""

    model_config = ConfigDict(extra="forbid")

    guardrail_id: str = Field(..., min_length=1)
    model_name: str = Field(..., min_length=1)
    field_name: str = Field(..., min_length=1)
    constraint_type: Literal[
        "type",
        "range",
        "regex",
        "enum",
        "custom_validator",
        "extra_forbid",
    ]
    rule: str = Field(..., min_length=1)
    error_message: str = ""

    @field_validator("guardrail_id", "rule")
    @classmethod
    def _validate_guardrail_non_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be blank")
        return cleaned

    @field_validator("model_name")
    @classmethod
    def _validate_model_name(cls, value: str) -> str:
        return _require_identifier(value, "model_name")

    @field_validator("field_name")
    @classmethod
    def _validate_field_name(cls, value: str) -> str:
        return _require_identifier(value, "field_name")


class PBTSpec(BaseModel):
    """Executable specification combining Hypothesis invariants and Pydantic guardrails."""

    model_config = ConfigDict(extra="forbid")

    pbt_spec_id: str = Field(..., min_length=1)
    target_module: str = Field(..., min_length=1)
    req_ids: list[str] = Field(..., min_length=1)
    invariants: list[PropertyInvariant] = Field(default_factory=list)
    guardrails: list[PydanticGuardrail] = Field(default_factory=list)
    alloy_model_ref: str | None = None
    seed: int | None = None

    @field_validator("pbt_spec_id", "target_module")
    @classmethod
    def _validate_spec_non_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be blank")
        return cleaned

    @field_validator("req_ids")
    @classmethod
    def _validate_spec_req_ids(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in values:
            stripped = item.strip()
            if not stripped:
                raise ValueError("req_ids must not contain blank entries")
            cleaned.append(stripped)
        return cleaned

    @model_validator(mode="after")
    def _validate_spec_integrity(self) -> PBTSpec:
        seen_invariants: set[str] = set()
        req_set = set(self.req_ids)
        for inv in self.invariants:
            if inv.invariant_id in seen_invariants:
                raise ValueError(f"Duplicate invariant_id: {inv.invariant_id!r}")
            seen_invariants.add(inv.invariant_id)
            if inv.req_id not in req_set:
                raise ValueError(
                    f"Invariant {inv.invariant_id!r} references req_id {inv.req_id!r} not in PBTSpec.req_ids"
                )

        seen_guardrails: set[str] = set()
        for guardrail in self.guardrails:
            if guardrail.guardrail_id in seen_guardrails:
                raise ValueError(f"Duplicate guardrail_id: {guardrail.guardrail_id!r}")
            seen_guardrails.add(guardrail.guardrail_id)
        return self

    def render_hypothesis_stub(self) -> str:
        """Render a Python Hypothesis test module scaffold for this PBTSpec."""
        lines = [
            f'"""Auto-generated Hypothesis scaffold for {self.pbt_spec_id} ({self.target_module})."""',
            "from hypothesis import given, settings, strategies as st",
            "",
        ]
        for inv in self.invariants:
            safe_name = (
                "".join(c if c.isalnum() else "_" for c in inv.invariant_id.lower()).strip("_")
                or "invariant"
            )
            params = ", ".join(s.parameter_name for s in inv.strategies)
            strat_args = ", ".join(f"{s.parameter_name}={s.strategy_expr}" for s in inv.strategies)
            lines.append(f"@settings(max_examples={inv.max_examples})")
            if strat_args:
                lines.append(f"@given({strat_args})")
            lines.append(f"def test_{safe_name}({params}) -> None:")
            lines.append(f'    """[{inv.req_id}] {inv.title}"""')
            if inv.precondition:
                lines.append(f"    # Precondition: {inv.precondition}")
            lines.append(f"    assert {inv.assertion_expr}")
            lines.append("")
        return "\n".join(lines)


__all__ = [
    "HypothesisStrategySpec",
    "PBTSpec",
    "PropertyInvariant",
    "PydanticGuardrail",
]
