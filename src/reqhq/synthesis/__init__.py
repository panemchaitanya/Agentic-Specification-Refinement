"""Stage 4a & 5a Socio-Technical Triage, Routing ADRs, Design, and Process/Training synthesis."""

from __future__ import annotations

from reqhq.synthesis.bundle import build_traceability_matrix, write_synthesis_bundle
from reqhq.synthesis.design_emitter import emit_design_md
from reqhq.synthesis.models import (
    AssumptionLike,
    EmittedADRFile,
    SynthesisBundleResult,
    TraceabilityMatrix,
    TraceabilityRow,
    TriageItem,
    TriagePlan,
    TriageTrack,
)
from reqhq.synthesis.process_training_emitter import emit_process_and_training_md
from reqhq.synthesis.routing_adr_emitter import (
    build_assumption_adrs,
    build_routing_adrs,
    dump_yaml_frontmatter,
    parse_emitted_adr,
    parse_yaml_frontmatter,
    validate_assumption_adr_markdown,
    validate_routing_adr_markdown,
)
from reqhq.synthesis.triage import SocioTechnicalTriager

__all__ = [
    "AssumptionLike",
    "EmittedADRFile",
    "SocioTechnicalTriager",
    "SynthesisBundleResult",
    "TraceabilityMatrix",
    "TraceabilityRow",
    "TriageItem",
    "TriagePlan",
    "TriageTrack",
    "build_assumption_adrs",
    "build_routing_adrs",
    "build_traceability_matrix",
    "dump_yaml_frontmatter",
    "emit_design_md",
    "emit_process_and_training_md",
    "parse_emitted_adr",
    "parse_yaml_frontmatter",
    "validate_assumption_adr_markdown",
    "validate_routing_adr_markdown",
    "write_synthesis_bundle",
]
