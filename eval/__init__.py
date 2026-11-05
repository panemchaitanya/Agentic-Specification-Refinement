"""Evaluation lane (Stage 2/4): harness, baselines B1-B4, oracle, scoring.

This package is evaluation infrastructure only. It never edits src/specpipe;
it composes the pipeline's public functions and consumes match verdicts
produced under the written rubric in docs/evaluation.md.
"""
import sys
from pathlib import Path

# Same import pattern as tests/: repo src/ holds the specpipe package.
_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
