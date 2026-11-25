"""Loader and validator for the 6 greenfield Layer 3 benchmark coding tasks."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from reqhq.evaluation.models import CodingTaskMeta

DEFAULT_CODING_TASKS_DIR: Path = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "benchmarks"
    / "coding_tasks"
)

EXPECTED_TASK_IDS: tuple[str, ...] = (
    "refund_sla_engine",
    "triage_escalation_queue",
    "cold_chain_inventory",
    "access_approval_workflow",
    "loan_disbursement_gate",
    "incident_shift_handover",
)

_REQUIRED_FILES: tuple[str, ...] = (
    "task_meta.json",
    "raw_prd.md",
    "single_pass_nl_spec.md",
    "hidden_tests/test_acceptance.py",
)


def _canonical_task_sort_key(meta: CodingTaskMeta) -> tuple[int, str]:
    if meta.task_id in EXPECTED_TASK_IDS:
        return (0, f"{EXPECTED_TASK_IDS.index(meta.task_id):02d}_{meta.task_id}")
    return (1, meta.task_id)


def load_coding_tasks(
    root: Path | str | None = None,
    *,
    max_tasks: int | None = None,
) -> list[CodingTaskMeta]:
    """Load and validate all greenfield Layer 3 coding tasks from disk.

    Verifies that every task directory contains ``task_meta.json``, ``raw_prd.md``,
    ``single_pass_nl_spec.md``, and syntactically valid ``hidden_tests/test_acceptance.py``.
    """
    base_dir = Path(root) if root is not None else DEFAULT_CODING_TASKS_DIR
    if not base_dir.is_dir():
        raise FileNotFoundError(f"Coding tasks directory not found: {base_dir}")
    if max_tasks is not None and max_tasks < 1:
        raise ValueError(f"max_tasks must be >= 1 when specified, got {max_tasks}")

    tasks: list[CodingTaskMeta] = []
    seen_ids: set[str] = set()

    for subdir in sorted(p for p in base_dir.iterdir() if p.is_dir()):
        for rel_file in _REQUIRED_FILES:
            candidate = subdir / rel_file
            if not candidate.is_file():
                raise FileNotFoundError(
                    f"Coding task {subdir.name!r} is missing required file: {candidate}"
                )

        hidden_test_path = subdir / "hidden_tests" / "test_acceptance.py"
        hidden_src = hidden_test_path.read_text(encoding="utf-8")
        try:
            ast.parse(hidden_src, filename=str(hidden_test_path))
        except SyntaxError as exc:
            raise ValueError(
                f"Invalid Python syntax in hidden acceptance test {hidden_test_path}: {exc}"
            ) from exc

        raw_meta = json.loads((subdir / "task_meta.json").read_text(encoding="utf-8"))
        meta = CodingTaskMeta.model_validate(raw_meta).model_copy(
            update={"task_dir": subdir.resolve()}
        )
        if meta.task_id in seen_ids:
            raise ValueError(f"Duplicate coding task_id detected: {meta.task_id!r}")
        seen_ids.add(meta.task_id)
        tasks.append(meta)

    if not tasks:
        raise ValueError(f"No coding tasks found under {base_dir}")

    tasks.sort(key=_canonical_task_sort_key)
    if max_tasks is not None:
        return tasks[:max_tasks]
    return tasks


__all__ = [
    "DEFAULT_CODING_TASKS_DIR",
    "EXPECTED_TASK_IDS",
    "load_coding_tasks",
]
