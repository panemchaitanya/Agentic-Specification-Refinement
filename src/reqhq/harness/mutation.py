"""Pluggable MutationRunner Protocol, POSIX-gated MutmutRunner, and platform selector."""

from __future__ import annotations

from collections.abc import Sequence
import importlib.util
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Protocol, runtime_checkable

from reqhq.harness.ast_mutator import ASTMutantEngine
from reqhq.harness.models import MutationReport, compute_mutation_score

_LABEL_RE = re.compile(
    r"\b(?P<label>killed|survived|timeout|suspicious|errors?)\s*[:=]\s*(?P<count>\d+)",
    re.IGNORECASE,
)


@runtime_checkable
class MutationRunner(Protocol):
    """Protocol implemented by Windows AST and POSIX mutmut mutation runners."""

    runner_name: str

    def run(
        self,
        workspace_dir: Path | str,
        target_files: Sequence[Path | str],
        test_paths: Sequence[Path | str] | None = None,
        *,
        timeout_seconds: float | None = None,
        max_mutants: int | None = None,
    ) -> MutationReport:
        """Execute mutation testing over target_files inside workspace_dir."""


def _parse_mutmut_counts(output: str) -> tuple[int, int, int, int]:
    """Parse killed, survived, timeout, and error counts from mutmut CLI output."""
    counts = {"killed": 0, "survived": 0, "timeout": 0, "errors": 0}
    for match in _LABEL_RE.finditer(output):
        label = match.group("label").lower()
        value = int(match.group("count"))
        if label == "killed":
            counts["killed"] = value
        elif label == "survived":
            counts["survived"] = value
        elif label == "timeout":
            counts["timeout"] = value
        else:
            counts["errors"] += value
    return counts["killed"], counts["survived"], counts["timeout"], counts["errors"]


class MutmutRunner:
    """POSIX-only adapter for mutmut (guarded against Windows fork() crashes)."""

    runner_name: str = "mutmut"

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        max_mutants: int = 50,
        python_executable: str | None = None,
    ) -> None:
        if timeout_seconds <= 0.0:
            raise ValueError("timeout_seconds must be > 0")
        if max_mutants < 0:
            raise ValueError("max_mutants must be >= 0")
        self.timeout_seconds = float(timeout_seconds)
        self.max_mutants = int(max_mutants)
        self.python_executable = python_executable or sys.executable

    @staticmethod
    def is_supported() -> tuple[bool, str]:
        """Return (True, 'ok') only on non-Windows platforms with mutmut installed."""
        if sys.platform == "win32" or sys.platform.startswith("win"):
            return (
                False,
                "MutmutRunner requires POSIX fork() and is not supported on Windows "
                f"(platform={sys.platform!r}); use ASTMutantEngine instead.",
            )
        has_module = importlib.util.find_spec("mutmut") is not None
        has_binary = shutil.which("mutmut") is not None
        if not (has_module or has_binary):
            return (
                False,
                "mutmut is not installed in the active environment; "
                "install mutmut on POSIX or use ASTMutantEngine.",
            )
        return True, "ok"

    def run(
        self,
        workspace_dir: Path | str,
        target_files: Sequence[Path | str],
        test_paths: Sequence[Path | str] | None = None,
        *,
        timeout_seconds: float | None = None,
        max_mutants: int | None = None,
    ) -> MutationReport:
        """Invoke mutmut on POSIX platforms; fail fast with RuntimeError on Windows/missing mutmut."""
        supported, reason = self.is_supported()
        if not supported:
            raise RuntimeError(reason)

        workspace = Path(workspace_dir).resolve()
        if not workspace.is_dir():
            raise FileNotFoundError(f"Workspace directory not found: {workspace}")

        eff_timeout = self.timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        if eff_timeout <= 0.0:
            raise ValueError("timeout_seconds must be > 0")

        normalized_targets = [Path(p).as_posix() for p in target_files]
        cmd = [
            self.python_executable,
            "-m",
            "mutmut",
            "run",
            "--paths-to-mutate",
            ",".join(normalized_targets),
        ]
        if test_paths:
            cmd.extend(["--tests-dir", ",".join(Path(p).as_posix() for p in test_paths)])

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=eff_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return MutationReport(
                runner_name=self.runner_name,
                target_files=normalized_targets,
                total_mutants=1,
                killed=0,
                survived=0,
                timeout=1,
                errors=0,
                mutation_score=0.0,
                mutants=[],
            )

        combined_output = f"{proc.stdout}\n{proc.stderr}"
        killed, survived, timeout, errors = _parse_mutmut_counts(combined_output)
        total = killed + survived + timeout + errors
        if total == 0 and proc.returncode != 0:
            errors = 1
            total = 1

        score = compute_mutation_score(killed, survived, timeout, errors)
        return MutationReport(
            runner_name=self.runner_name,
            target_files=normalized_targets,
            total_mutants=total,
            killed=killed,
            survived=survived,
            timeout=timeout,
            errors=errors,
            mutation_score=score,
            mutants=[],
        )


def select_mutation_runner(
    *,
    prefer_mutmut: bool = False,
    timeout_seconds: float = 15.0,
    max_mutants: int = 50,
) -> MutationRunner:
    """Select the appropriate MutationRunner for the host platform.

    On Windows 11 (or whenever ``prefer_mutmut=False`` or ``mutmut`` is unavailable),
    this automatically returns ``ASTMutantEngine``.
    """
    if prefer_mutmut:
        supported, _ = MutmutRunner.is_supported()
        if supported:
            return MutmutRunner(
                timeout_seconds=timeout_seconds,
                max_mutants=max_mutants,
            )
    return ASTMutantEngine(
        timeout_seconds=timeout_seconds,
        max_mutants=max_mutants,
    )


__all__ = [
    "MutationRunner",
    "MutmutRunner",
    "select_mutation_runner",
]
