# 0002. Isolate v2 Engine Under `src/reqhq`

- **Date:** 2026-11-24
- **Status:** Accepted
- **PR / Branch:** `adr/0002-isolate-v2-engine-under-src-reqhq`
- **Supersedes:** None

## Context

The repository currently hosts the Team ReqHQ mid-submission research artifact (commit `0514afa` on branch `Chaitanya`, alongside teammate work on `origin/Anjali`):

- `src/specpipe/` implements the Stage 1 PRD-to-specification pipeline and Streamlit demo (`app.py`).
- `data/frozen/`, `eval/`, and `training/` preserve frozen PROMISE NFR / PURE dataset splits, bounded evaluation manifests, and annotation/training scripts required for course evaluation reproducibility (H1–H3).
- `tests/test_*.py` contains **87 passing offline unit tests** verified on Python 3.15.0.

Rewriting `src/specpipe/` in place to build ReqHQ v2 would risk invalidating frozen evaluation baselines, breaking the hosted Streamlit demo, and creating merge conflicts with `Chaitanya` and `origin/Anjali`.

## Decision

We isolate the entire ReqHQ v2 Phase 1 architecture in a dedicated namespace package and test tree while keeping the legacy research codebase strictly immutable:

1. **Disjoint Package Root:** All v2 production modules live exclusively under `src/reqhq/` (`adr/`, `schemas/`, `store/`, `ingestion/`, `llm/`, `config.py`, `cli.py`).
2. **Disjoint Test Tree:** All v2 unit and property-based tests live exclusively under `tests/reqhq/` (`test_adr_manager.py`, `test_schemas_and_store.py`, `test_hardware_profiler.py`, `test_ingestion_and_chunker.py`, `test_router_and_quota.py`, `test_cli.py`).
3. **Zero Legacy Mutation:** `src/specpipe/`, `data/frozen/`, `app.py`, `eval/`, `training/`, `requirements.txt`, and existing `tests/test_*.py` are **never modified**. Base branch `Chaitanya` (`0514afa`) and remote branch `origin/Anjali` remain untouched; v2 integration targets `feat/v2-phase1-core`.
4. **Coexistent Packaging:** A PEP 621 `pyproject.toml` declares `package-dir = {"" = "src"}`, exposes console entry point `reqhq = "reqhq.cli:main"`, configures `[tool.pytest.ini_options] pythonpath = ["src"]`, and keeps `specpipe` dependencies in an optional `[legacy]` extra so both `pytest --ignore=tests/reqhq` (87 legacy tests) and full `pytest` run cleanly with zero cross-suite interference.

## Consequences

- **Positive:** 100% backward compatibility with the mid-submission baseline (`87 passed`) and zero risk to frozen evaluation splits or teammate branches.
- **Positive:** Clean architectural boundaries enable strict Pydantic v2 (`extra="forbid"`), event-sourced CLI workflows, and local-first LLM routing without carrying Streamlit or v1 heuristic debt.
- **Trade-off:** Two packages (`specpipe` and `reqhq`) temporarily coexist under `src/`, requiring contributors to keep imports strictly one-way (neither package imports the other during Phase 1).
- **Follow-up:** Any future migration of v1 evaluation benchmarks onto `reqhq` will require a dedicated ADR and frozen-split reproducibility audit.
