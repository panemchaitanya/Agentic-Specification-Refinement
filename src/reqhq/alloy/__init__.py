"""Stage 4b Two-Layer Literate Alloy 6 Emitter & Headless Neuro-Symbolic Verification Gate."""

from __future__ import annotations

from reqhq.alloy.counterexample import translate_counterexample
from reqhq.alloy.emitter import (
    ALLOY6_TEMPORAL_KEYWORDS,
    AlloySpecEmitter,
    lint_core_domain_version_agnostic,
)
from reqhq.alloy.extractor import (
    build_req_to_alloy_refs,
    extract_alloy_blocks,
    replace_alloy_block,
    write_alloy_files,
)
from reqhq.alloy.mock_analyzer import MockAlloyAnalyzer
from reqhq.alloy.models import (
    AlloyCompatMode,
    AlloyRunResult,
    AlloyRunStatus,
    AlloyVerificationOutcome,
    ConflictQuestion,
    CounterexamplePrompt,
    ExtractedAlloyBlock,
)
from reqhq.alloy.repair_loop import AlloyVerificationController
from reqhq.alloy.runner import AlloyAnalyzer, HeadlessAlloyRunner, parse_alloy_output
from reqhq.alloy.unsat_mapper import map_unsat_to_conflicts
from reqhq.alloy.vacuity import inject_vacuity_witnesses

__all__ = [
    "ALLOY6_TEMPORAL_KEYWORDS",
    "AlloyAnalyzer",
    "AlloyCompatMode",
    "AlloyRunResult",
    "AlloyRunStatus",
    "AlloySpecEmitter",
    "AlloyVerificationController",
    "AlloyVerificationOutcome",
    "ConflictQuestion",
    "CounterexamplePrompt",
    "ExtractedAlloyBlock",
    "HeadlessAlloyRunner",
    "MockAlloyAnalyzer",
    "build_req_to_alloy_refs",
    "extract_alloy_blocks",
    "inject_vacuity_witnesses",
    "lint_core_domain_version_agnostic",
    "map_unsat_to_conflicts",
    "parse_alloy_output",
    "replace_alloy_block",
    "translate_counterexample",
    "write_alloy_files",
]
