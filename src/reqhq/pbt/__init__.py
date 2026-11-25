"""Public API for Phase 3 PBT contract emission, guardrail codegen, locking, and counterexample feedback."""

from __future__ import annotations

from reqhq.pbt.codegen import compile_pbt_guardrails, export_pbt_bundle
from reqhq.pbt.contract_lock import (
    ContractTamperError,
    verify_contract_lock,
    write_contract_lock,
)
from reqhq.pbt.counterexample_feedback import parse_hypothesis_counterexamples
from reqhq.pbt.emitter import PBTSpecEmitter, dump_pbt_spec_yaml, load_pbt_spec_yaml
from reqhq.pbt.models import (
    CounterexampleClassification,
    CounterexampleRepairReport,
    GeneratedGuardrailBundle,
    GuardrailEnforcement,
    PBTContractDocument,
    PBTPropertyContract,
    PropertyKind,
)

__all__ = [
    "ContractTamperError",
    "CounterexampleClassification",
    "CounterexampleRepairReport",
    "GeneratedGuardrailBundle",
    "GuardrailEnforcement",
    "PBTContractDocument",
    "PBTPropertyContract",
    "PBTSpecEmitter",
    "PropertyKind",
    "compile_pbt_guardrails",
    "dump_pbt_spec_yaml",
    "export_pbt_bundle",
    "load_pbt_spec_yaml",
    "parse_hypothesis_counterexamples",
    "verify_contract_lock",
    "write_contract_lock",
]
