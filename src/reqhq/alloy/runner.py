"""AlloyAnalyzer protocol and headless Java/Alloy CLI execution runner."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Protocol, runtime_checkable

from reqhq.alloy.models import AlloyRunResult, AlloyRunStatus
from reqhq.alloy.vacuity import inject_vacuity_witnesses

_COMPILE_ERROR_RE = re.compile(
    r"(syntax\s+error|type\s+error|compilation\s+failed|cannot\s+be\s+resolved|parse\s+error)",
    re.IGNORECASE,
)
_VACUITY_UNSAT_RE = re.compile(
    r"(__vacuity_witness_[A-Za-z0-9_]+[^\r\n]*\bUNSAT\b|VACUOUS_UNSAT|vacuity[^\r\n]*UNSAT)",
    re.IGNORECASE,
)
_GENERIC_UNSAT_RE = re.compile(r"\b(UNSAT|unsatisfiable)\b|UNSAT\s+core", re.IGNORECASE)
_COUNTEREXAMPLE_RE = re.compile(
    r"((?<!\bno\s)counterexample\s+found|may\s+be\s+violated|COUNTEREXAMPLE_FOUND|---INSTANCE---)",
    re.IGNORECASE,
)
_LINE_NUM_RE = re.compile(
    r"(?:\bline\s+(\d+)|\.als:(\d+)|\bL(\d+)\b)",
    re.IGNORECASE,
)
_FACT_NAME_RE = re.compile(
    r"(?:\bfact\s+([A-Za-z_][A-Za-z0-9_]*)\b|UNSAT\s+core\s*:\s*\[([^\]]+)\])",
    re.IGNORECASE,
)
_TRACE_STEP_RE = re.compile(
    r"^\s*(?:Step|State)\s+(\d+)\s*(?:\(([^)]*)\))?\s*:\s*(.+)$",
    re.IGNORECASE,
)
_RELATION_LINE_RE = re.compile(r"^\s*(?:this/|skolem\s+\$)?([A-Za-z_][A-Za-z0-9_/$]*)\s*=\s*\{([^}]*)\}\s*$")


@runtime_checkable
class AlloyAnalyzer(Protocol):
    """Protocol for offline mock and real headless Alloy Analyzer runners."""

    def run_check(
        self,
        core_als: str,
        behavior_als: str,
        *,
        work_dir: Path,
        max_scope: int = 4,
        check_vacuity: bool = True,
    ) -> AlloyRunResult:
        """Execute compilation, optional vacuity witnesses, and assertion checks."""
        ...


def _extract_unsat_lines(text: str) -> list[int]:
    lines: list[int] = []
    seen: set[int] = set()
    for match in _LINE_NUM_RE.finditer(text):
        raw = next(g for g in match.groups() if g is not None)
        num = int(raw)
        if num > 0 and num not in seen:
            seen.add(num)
            lines.append(num)
    return sorted(lines)


def _extract_unsat_facts(text: str) -> list[str]:
    facts: list[str] = []
    seen: set[str] = set()
    for match in _FACT_NAME_RE.finditer(text):
        single = match.group(1)
        bracket_list = match.group(2)
        if single:
            if single not in seen:
                seen.add(single)
                facts.append(single)
        elif bracket_list:
            for item in bracket_list.split(","):
                cleaned = item.strip().strip("'\"")
                if cleaned and cleaned not in seen:
                    seen.add(cleaned)
                    facts.append(cleaned)
    return facts


def _extract_counterexample_trace(text: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    current_relations: dict[str, Any] = {}

    for line in text.splitlines():
        step_match = _TRACE_STEP_RE.match(line)
        if step_match:
            step_num = int(step_match.group(1))
            state_label = (step_match.group(2) or f"State{step_num}").strip()
            detail = step_match.group(3).strip()
            entry: dict[str, Any] = {"step": step_num, "state": state_label}
            # Parse simple key=value pairs separated by commas when present.
            kv_pairs = [part.strip() for part in detail.split(",") if "=" in part]
            if kv_pairs:
                for pair in kv_pairs:
                    k, v = pair.split("=", 1)
                    entry[k.strip()] = v.strip()
            else:
                entry["detail"] = detail
            steps.append(entry)
            continue

        rel_match = _RELATION_LINE_RE.match(line)
        if rel_match:
            rel_name = rel_match.group(1).strip()
            rel_val = rel_match.group(2).strip()
            current_relations[rel_name] = rel_val

    if not steps and current_relations:
        steps.append({"step": 0, "state": "CounterexampleInstance", **current_relations})

    return steps


def parse_alloy_output(
    stdout: str,
    stderr: str,
    exit_code: int,
    *,
    command: str = "",
    elapsed_ms: float = 0.0,
    check_vacuity: bool = True,
) -> AlloyRunResult:
    """Parse Alloy CLI stdout/stderr and exit code into a structured AlloyRunResult."""
    combined = f"{stdout}\n{stderr}"

    if exit_code != 0 or _COMPILE_ERROR_RE.search(combined):
        return AlloyRunResult(
            status=AlloyRunStatus.COMPILE_ERROR,
            command=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            vacuity_passed=False,
            unsat_lines=_extract_unsat_lines(combined),
            unsat_facts=_extract_unsat_facts(combined),
            elapsed_ms=elapsed_ms,
        )

    if _VACUITY_UNSAT_RE.search(combined) or (
        check_vacuity and _GENERIC_UNSAT_RE.search(combined) and "SAT" not in stdout.replace("UNSAT", "")
    ):
        return AlloyRunResult(
            status=AlloyRunStatus.VACUOUS_UNSAT,
            command=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            vacuity_passed=False,
            unsat_lines=_extract_unsat_lines(combined),
            unsat_facts=_extract_unsat_facts(combined),
            elapsed_ms=elapsed_ms,
        )

    if _COUNTEREXAMPLE_RE.search(combined):
        return AlloyRunResult(
            status=AlloyRunStatus.COUNTEREXAMPLE_FOUND,
            command=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            vacuity_passed=check_vacuity,
            counterexample_trace=_extract_counterexample_trace(combined),
            elapsed_ms=elapsed_ms,
        )

    return AlloyRunResult(
        status=AlloyRunStatus.VERIFIED,
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        vacuity_passed=check_vacuity,
        elapsed_ms=elapsed_ms,
    )


class HeadlessAlloyRunner:
    """Executes headless Alloy 6 CLI (`java -jar <REQHQ_ALLOY_JAR_PATH>`) via subprocess.run."""

    def __init__(
        self,
        jar_path: Path | str | None = None,
        *,
        java_bin: str = "java",
        timeout_seconds: float = 30.0,
        bitwidth: int = 4,
        solver: str = "sat4j",
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if bitwidth < 1 or bitwidth > 4:
            raise ValueError("bitwidth must be bounded between 1 and 4")
        resolved_jar = jar_path if jar_path is not None else os.environ.get("REQHQ_ALLOY_JAR_PATH")
        self.jar_path: Path | None = Path(resolved_jar) if resolved_jar else None
        self.java_bin = java_bin
        self.timeout_seconds = timeout_seconds
        self.bitwidth = bitwidth
        self.solver = solver

    def is_available(self) -> bool:
        """Return True iff both `java` and the configured Alloy JAR are present on disk."""
        return (
            shutil.which(self.java_bin) is not None
            and self.jar_path is not None
            and self.jar_path.is_file()
        )

    def build_command(
        self,
        behavior_path: Path,
        out_dir: Path,
        *,
        max_scope: int = 4,
    ) -> list[str]:
        """Build the headless `java -jar <jar>` command with bounded scope/bitwidth flags."""
        if self.jar_path is None:
            raise ValueError(
                "Alloy JAR path is not configured; set jar_path or REQHQ_ALLOY_JAR_PATH"
            )
        bounded_scope = max(1, min(max_scope, 4))
        bounded_bitwidth = min(self.bitwidth, 4)
        return [
            self.java_bin,
            f"-Dalloy.maxScope={bounded_scope}",
            f"-Dalloy.bitwidth={bounded_bitwidth}",
            "-jar",
            str(self.jar_path),
            "exec",
            "-f",
            "-o",
            str(out_dir),
            "-s",
            self.solver,
            str(behavior_path),
        ]

    def run_check(
        self,
        core_als: str,
        behavior_als: str,
        *,
        work_dir: Path,
        max_scope: int = 4,
        check_vacuity: bool = True,
    ) -> AlloyRunResult:
        """Write `.als` modules to `work_dir` and invoke headless Alloy Analyzer."""
        if self.jar_path is None:
            return AlloyRunResult(
                status=AlloyRunStatus.COMPILE_ERROR,
                exit_code=-1,
                stderr="Alloy JAR path is not configured (set REQHQ_ALLOY_JAR_PATH).",
                vacuity_passed=False,
            )

        target_dir = Path(work_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        out_dir = target_dir / "alloy_out"
        out_dir.mkdir(parents=True, exist_ok=True)

        core_path = target_dir / "core_domain.als"
        behavior_path = target_dir / "behavior.als"

        effective_behavior = behavior_als
        if check_vacuity:
            effective_behavior, _ = inject_vacuity_witnesses(behavior_als, max_scope=max_scope)

        core_path.write_text(
            core_als if core_als.endswith("\n") else core_als + "\n",
            encoding="utf-8",
        )
        behavior_path.write_text(
            effective_behavior if effective_behavior.endswith("\n") else effective_behavior + "\n",
            encoding="utf-8",
        )

        cmd = self.build_command(behavior_path, out_dir, max_scope=max_scope)
        cmd_str = " ".join(cmd)
        start = time.perf_counter()

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(target_dir),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            stderr_msg = f"Alloy execution timed out after {self.timeout_seconds}s"
            if exc.stderr:
                stderr_msg = f"{stderr_msg}\n{exc.stderr}"
            return AlloyRunResult(
                status=AlloyRunStatus.COMPILE_ERROR,
                command=cmd_str,
                exit_code=-1,
                stdout=str(exc.stdout or ""),
                stderr=stderr_msg,
                vacuity_passed=False,
                elapsed_ms=elapsed_ms,
            )
        except OSError as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            return AlloyRunResult(
                status=AlloyRunStatus.COMPILE_ERROR,
                command=cmd_str,
                exit_code=-1,
                stderr=f"Failed to invoke Alloy runner: {exc}",
                vacuity_passed=False,
                elapsed_ms=elapsed_ms,
            )

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return parse_alloy_output(
            proc.stdout or "",
            proc.stderr or "",
            proc.returncode,
            command=cmd_str,
            elapsed_ms=elapsed_ms,
            check_vacuity=check_vacuity,
        )


__all__ = [
    "AlloyAnalyzer",
    "HeadlessAlloyRunner",
    "parse_alloy_output",
]
