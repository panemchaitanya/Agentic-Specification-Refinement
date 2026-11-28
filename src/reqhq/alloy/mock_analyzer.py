"""Deterministic offline MockAlloyAnalyzer for unit tests and CLI --mock-alloy runs."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from reqhq.alloy.models import AlloyRunResult, AlloyRunStatus

ScriptedAlloyItem = (
    AlloyRunResult
    | AlloyRunStatus
    | Callable[[str, str, int, bool], AlloyRunResult]
)


class MockAlloyAnalyzer:
    """Offline AlloyAnalyzer implementation supporting scripted outcome sequences."""

    def __init__(
        self,
        scripted_results: Sequence[ScriptedAlloyItem] | None = None,
        *,
        default_status: AlloyRunStatus = AlloyRunStatus.VERIFIED,
    ) -> None:
        self._queue: list[ScriptedAlloyItem] = list(scripted_results or [])
        self.default_status = default_status
        self.calls: list[dict[str, Any]] = []

    def _result_from_status(
        self,
        status: AlloyRunStatus,
        *,
        check_vacuity: bool,
    ) -> AlloyRunResult:
        if status == AlloyRunStatus.VERIFIED:
            return AlloyRunResult(
                status=AlloyRunStatus.VERIFIED,
                command="mock-alloy exec behavior.als",
                exit_code=0,
                stdout="0 errors. Vacuity witnesses SAT. All assertions verified.",
                vacuity_passed=check_vacuity,
                elapsed_ms=1.0,
            )
        if status == AlloyRunStatus.COMPILE_ERROR:
            return AlloyRunResult(
                status=AlloyRunStatus.COMPILE_ERROR,
                command="mock-alloy exec behavior.als",
                exit_code=1,
                stderr="Syntax error in behavior.als at line 1",
                vacuity_passed=False,
                elapsed_ms=1.0,
            )
        if status == AlloyRunStatus.VACUOUS_UNSAT:
            return AlloyRunResult(
                status=AlloyRunStatus.VACUOUS_UNSAT,
                command="mock-alloy exec behavior.als",
                exit_code=0,
                stdout="run __vacuity_witness_model: UNSAT (vacuous model)",
                vacuity_passed=False,
                elapsed_ms=1.0,
            )
        return AlloyRunResult(
            status=AlloyRunStatus.COUNTEREXAMPLE_FOUND,
            command="mock-alloy exec behavior.als",
            exit_code=0,
            stdout="Counterexample found for assertion SafetyAuthorizedOwnership",
            vacuity_passed=check_vacuity,
            counterexample_trace=[
                {
                    "step": 0,
                    "state": "Init",
                    "actor": "HumanActor$0",
                    "activeEntities": "SystemEntity$0",
                }
            ],
            elapsed_ms=1.0,
        )

    def run_check(
        self,
        core_als: str,
        behavior_als: str,
        *,
        work_dir: Path,
        max_scope: int = 4,
        check_vacuity: bool = True,
    ) -> AlloyRunResult:
        """Record the invocation and return the next scripted or default AlloyRunResult."""
        self.calls.append(
            {
                "core_als": core_als,
                "behavior_als": behavior_als,
                "work_dir": Path(work_dir),
                "max_scope": max_scope,
                "check_vacuity": check_vacuity,
            }
        )

        if self._queue:
            item = self._queue.pop(0)
            if isinstance(item, AlloyRunResult):
                return item
            if isinstance(item, AlloyRunStatus):
                return self._result_from_status(item, check_vacuity=check_vacuity)
            return item(core_als, behavior_als, max_scope, check_vacuity)

        return self._result_from_status(self.default_status, check_vacuity=check_vacuity)


__all__ = ["MockAlloyAnalyzer", "ScriptedAlloyItem"]
