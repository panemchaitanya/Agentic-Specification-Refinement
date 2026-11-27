"""Live LLM-backed CodingAgent for Condition A/B/C Layer 3 empirical evaluation (ADR-0021)."""

from __future__ import annotations

import ast
import difflib
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from reqhq.evaluation.tasks import (
    DEFAULT_CODING_TASKS_DIR,
    EXTENDED_CODING_TASKS_DIR,
    load_coding_tasks,
)
from reqhq.harness.experiment import AgentRunResult
from reqhq.harness.models import ExperimentCondition
from reqhq.llm.providers.base import LLMRequest, ProviderError
from reqhq.llm.router import EscalationReason, ProviderRouter

_PYTEST_COUNT_RE = re.compile(
    r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed)\b",
    re.IGNORECASE,
)
_FENCED_PY_RE = re.compile(
    r"```(?:python|py)?\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)


class _CodeGenPayload(BaseModel):
    """Strict JSON schema for LLM code + unit test synthesis."""

    model_config = ConfigDict(extra="forbid")

    module_code: str = Field(..., min_length=1)
    test_code: str = Field(default="")


def _write_lf(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = content.replace("\r\n", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(normalized)


def parse_pytest_pass_rate(output: str, returncode: int) -> float:
    """Parse a pytest summary line into a bounded pass rate in [0.0, 1.0]."""
    passed = 0
    failed = 0
    for count_str, label in _PYTEST_COUNT_RE.findall(output):
        count = int(count_str)
        norm = label.lower()
        if norm in ("passed", "xfailed", "xpassed"):
            passed += count
        elif norm in ("failed", "error", "errors"):
            failed += count
    total = passed + failed
    if total > 0:
        return round(passed / total, 4)
    return 1.0 if returncode == 0 else 0.0


def extract_code_payload(text: str, parsed: dict[str, Any] | None) -> tuple[str, str]:
    """Extract (module_code, test_code) from parsed schema output or fenced code fallback."""
    if parsed is not None:
        try:
            payload = _CodeGenPayload.model_validate(parsed)
            return payload.module_code.strip(), payload.test_code.strip()
        except Exception:
            pass

    blocks = [m.group(1).strip() for m in _FENCED_PY_RE.finditer(text) if m.group(1).strip()]
    if len(blocks) >= 2:
        return blocks[0], blocks[1]
    if len(blocks) == 1:
        return blocks[0], ""
    return text.strip(), ""


def _read_optional_capped(path: Path, max_chars: int) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:max_chars]


class LiveLLMCodingAgent:
    """Real LLM-backed CodingAgent enforcing strict Cond A/B/C isolation & zero hidden-test leakage."""

    def __init__(
        self,
        router: ProviderRouter,
        *,
        tasks_root: Path | str | None = None,
        max_repair_rounds: int = 2,
        python_executable: str | None = None,
        escalate_repairs: bool = True,
    ) -> None:
        if max_repair_rounds < 0:
            raise ValueError("max_repair_rounds must be >= 0")
        self.router = router
        self.tasks_root = Path(tasks_root) if tasks_root is not None else DEFAULT_CODING_TASKS_DIR
        self.max_repair_rounds = int(max_repair_rounds)
        self.python_executable = python_executable or sys.executable
        self.escalate_repairs = bool(escalate_repairs)
        self._task_index = {task.task_id: task for task in load_coding_tasks(self.tasks_root)}
        if tasks_root is None and EXTENDED_CODING_TASKS_DIR.is_dir():
            for ext_task in load_coding_tasks(EXTENDED_CODING_TASKS_DIR):
                self._task_index[ext_task.task_id] = ext_task

    def build_condition_prompt(
        self,
        *,
        case_id: str,
        condition: ExperimentCondition,
        bundle_dir: Path | None,
    ) -> tuple[str, str]:
        """Build isolated (system_prompt, user_prompt) for Cond A, B, or C without hidden-test access."""
        task = self._task_index.get(case_id)
        if task is None or task.task_dir is None:
            raise KeyError(f"Unknown coding task_id for LiveLLMCodingAgent: {case_id!r}")

        system_prompt = (
            "You are a senior Python 3.15 software engineer. "
            "Implement clean, deterministic, self-contained stdlib-only Python code and pytest unit tests. "
            "Return ONLY valid JSON matching the schema with keys `module_code` and `test_code`."
        )

        contract_header = (
            f"Target Python module file: `{task.target_module}.py`\n"
            f"Required public entry symbol: `{task.entry_symbol}`\n"
            f"Unit test file: `test_{task.target_module}.py` (importing `{task.entry_symbol}` from `{task.target_module}`)\n"
        )

        if condition == ExperimentCondition.COND_A:
            raw_prd = (task.task_dir / "raw_prd.md").read_text(encoding="utf-8")
            user_prompt = (
                f"{contract_header}\n"
                "## Stakeholder PRD (Condition A — Raw PRD Only)\n\n"
                f"{raw_prd}\n\n"
                f"Implement `{task.target_module}.py` exposing `{task.entry_symbol}` and concise pytest tests "
                f"in `test_{task.target_module}.py`."
            )
            return system_prompt, user_prompt

        nl_spec = (task.task_dir / "single_pass_nl_spec.md").read_text(encoding="utf-8")
        if condition == ExperimentCondition.COND_B:
            user_prompt = (
                f"{contract_header}\n"
                "## Specification (Condition B — Single-Pass NL Spec)\n\n"
                f"{nl_spec}\n\n"
                f"Implement `{task.target_module}.py` satisfying every functional requirement above "
                f"and pytest unit tests in `test_{task.target_module}.py`."
            )
            return system_prompt, user_prompt

        # Condition C: Read-only ReqHQ full handoff bundle (never mutates bundle_dir!)
        bundle_sections: list[str] = []
        if bundle_dir is not None:
            for rel_name, cap in (
                ("HANDOFF_INSTRUCTIONS.md", 1200),
                ("SPEC.md", 2400),
                ("DESIGN.md", 1600),
                ("PBT_Spec.yaml", 2400),
            ):
                snippet = _read_optional_capped(Path(bundle_dir) / rel_name, cap)
                if snippet:
                    bundle_sections.append(f"### {rel_name}\n{snippet}")

        bundle_blob = "\n\n".join(bundle_sections) if bundle_sections else "(No bundle artifacts provided)"
        user_prompt = (
            f"{contract_header}\n"
            "## Specification & ReqHQ Formal/PBT Handoff Bundle (Condition C)\n\n"
            f"{nl_spec}\n\n"
            "## Formal Specification, Design & Locked PBT Contract\n\n"
            f"{bundle_blob}\n\n"
            f"Implement `{task.target_module}.py` satisfying ALL exact boundaries, error conditions, "
            f"determinism/idempotency invariants, and thorough boundary pytest unit tests in "
            f"`test_{task.target_module}.py`."
        )
        return system_prompt, user_prompt

    def _run_pytest(
        self,
        test_targets: list[Path],
        *,
        cwd: Path,
        pythonpath_entries: list[Path],
        timeout_seconds: float,
    ) -> tuple[int, str]:
        resolved_cwd = Path(cwd).resolve()
        resolved_targets = [Path(t).resolve() for t in test_targets]
        resolved_pythonpath = [Path(p).resolve() for p in pythonpath_entries]

        env = os.environ.copy()
        extra_pp = os.pathsep.join(str(p) for p in resolved_pythonpath)
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{extra_pp}{os.pathsep}{existing_pp}" if existing_pp else extra_pp

        cmd = [
            self.python_executable,
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            *(str(t) for t in resolved_targets),
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(resolved_cwd),
                env=env,
                capture_output=True,
                text=True,
                timeout=max(5.0, timeout_seconds),
                check=False,
            )
            combined = (proc.stdout + "\n" + proc.stderr).strip()
            return proc.returncode, combined
        except subprocess.TimeoutExpired as exc:
            return 124, f"pytest timed out after {timeout_seconds:.1f}s: {exc}"

    def _check_syntax(self, code: str, filename: str) -> str | None:
        if not code.strip():
            return f"{filename} is empty"
        try:
            ast.parse(code, filename=filename)
        except SyntaxError as exc:
            return f"SyntaxError in {filename} line {exc.lineno}: {exc.msg}"
        return None

    def run_trial(
        self,
        *,
        case_id: str,
        condition: ExperimentCondition,
        seed: int,
        workspace_dir: Path,
        bundle_dir: Path | None,
        timeout_seconds: float,
    ) -> AgentRunResult:
        """Execute a live LLM coding trial inside workspace_dir and score against hidden tests."""
        task = self._task_index.get(case_id)
        if task is None or task.task_dir is None:
            raise KeyError(f"Unknown coding task_id for LiveLLMCodingAgent: {case_id!r}")

        resolved_workspace = Path(workspace_dir).resolve()
        resolved_bundle = Path(bundle_dir).resolve() if bundle_dir is not None else None
        resolved_workspace.mkdir(parents=True, exist_ok=True)
        module_rel = f"{task.target_module}.py"
        test_rel = f"test_{task.target_module}.py"
        module_path = resolved_workspace / module_rel
        test_path = resolved_workspace / test_rel

        system_prompt, base_prompt = self.build_condition_prompt(
            case_id=case_id,
            condition=condition,
            bundle_dir=resolved_bundle,
        )

        logs: list[str] = [
            f"[LiveLLMCodingAgent] case={case_id} condition={condition.value} seed={seed}\n"
        ]
        total_tokens = 0
        total_requests = 0
        stall_count = 0
        module_code = ""
        test_code = ""
        test_output = ""
        pbt_output = ""
        pbt_pass_rate = 0.0

        current_prompt = base_prompt
        max_rounds = 1 + self.max_repair_rounds

        for round_idx in range(max_rounds):
            escalation = (
                EscalationReason.FINAL_AUDIT
                if (round_idx > 0 and self.escalate_repairs)
                else None
            )
            req = LLMRequest(
                prompt=current_prompt,
                system_prompt=system_prompt,
                temperature=0.0,
                max_tokens=2048,
                response_schema=_CodeGenPayload,
                metadata={
                    "stage": "live_coding_agent",
                    "case_id": case_id,
                    "condition": condition.value,
                    "seed": seed,
                    "round": round_idx,
                },
            )
            try:
                resp = self.router.route(
                    req,
                    escalation_reason=escalation,
                    bypass_cache=(round_idx > 0),
                )
            except ProviderError as exc:
                stall_count += 1
                logs.append(f"[round {round_idx}] ProviderError: {exc}\n")
                break

            if resp.cloud_verified:
                total_requests += 1
                total_tokens += resp.total_tokens
            logs.append(
                f"[round {round_idx}] provider={resp.provider}({resp.model}) "
                f"tokens={resp.total_tokens} escalated={resp.escalated} "
                f"cloud_verified={resp.cloud_verified}\n"
            )

            cand_mod, cand_test = extract_code_payload(resp.text, resp.parsed)
            if cand_mod:
                module_code = cand_mod
            if cand_test:
                test_code = cand_test

            mod_err = self._check_syntax(module_code, module_rel)
            test_err = (
                self._check_syntax(test_code, test_rel)
                if test_code.strip()
                else None
            )
            if mod_err or test_err:
                stall_count += 1
                err_msg = "; ".join(e for e in (mod_err, test_err) if e)
                logs.append(f"[round {round_idx}] syntax check failed: {err_msg}\n")
                if round_idx < max_rounds - 1:
                    current_prompt = (
                        f"{base_prompt}\n\n"
                        f"[REPAIR ROUND {round_idx + 1}] Previous output had syntax errors: {err_msg}\n"
                        "Fix all syntax errors and return valid JSON with `module_code` and `test_code`."
                    )
                    continue
                break

            _write_lf(module_path, module_code)
            if not test_code.strip():
                test_code = (
                    f"from {task.target_module} import {task.entry_symbol}\n\n"
                    f"def test_symbol_importable() -> None:\n"
                    f"    assert callable({task.entry_symbol})\n"
                )
            _write_lf(test_path, test_code)

            # Execute agent's own unit tests
            unit_rc, unit_out = self._run_pytest(
                [test_path],
                cwd=resolved_workspace,
                pythonpath_entries=[resolved_workspace],
                timeout_seconds=timeout_seconds,
            )
            test_output = unit_out

            # For Condition C, also execute the read-only locked PBT suite in bundle_dir!
            pbt_rc = unit_rc
            pbt_out = unit_out
            if condition == ExperimentCondition.COND_C and resolved_bundle is not None:
                pbt_props_dir = resolved_bundle / "tests" / "properties"
                if pbt_props_dir.is_dir():
                    pbt_rc, pbt_out = self._run_pytest(
                        [pbt_props_dir],
                        cwd=resolved_workspace,
                        pythonpath_entries=[resolved_workspace, resolved_bundle],
                        timeout_seconds=timeout_seconds,
                    )
            pbt_output = pbt_out
            pbt_pass_rate = parse_pytest_pass_rate(pbt_out, pbt_rc)

            if unit_rc == 0 and pbt_rc == 0:
                logs.append(f"[round {round_idx}] unit & PBT checks PASSED (rate={pbt_pass_rate})\n")
                break

            stall_count += 1
            failure_excerpt = (unit_out + "\n" + pbt_out).strip()[-1200:]
            logs.append(f"[round {round_idx}] test/PBT failure (unit_rc={unit_rc}, pbt_rc={pbt_rc})\n")
            if round_idx < max_rounds - 1:
                current_prompt = (
                    f"{base_prompt}\n\n"
                    f"[REPAIR ROUND {round_idx + 1}] Previous implementation:\n"
                    f"```python\n{module_code}\n```\n\n"
                    f"Test/PBT failure output:\n```\n{failure_excerpt}\n```\n\n"
                    "Fix the defects while keeping all required invariants intact and return valid JSON."
                )

        # Post-generation ONLY: run unseen hidden acceptance suite (zero leakage to prompts!)
        hidden_pass = False
        hidden_test_path = (task.task_dir / "hidden_tests" / "test_acceptance.py").resolve()
        if module_path.is_file():
            hidden_rc, hidden_out = self._run_pytest(
                [hidden_test_path],
                cwd=resolved_workspace,
                pythonpath_entries=[resolved_workspace],
                timeout_seconds=timeout_seconds,
            )
            hidden_pass = hidden_rc == 0
            logs.append(f"[hidden_acceptance] rc={hidden_rc} pass={hidden_pass}\n")

        diff_lines = list(
            difflib.unified_diff(
                [],
                module_code.splitlines(keepends=True),
                fromfile="/dev/null",
                tofile=f"b/{module_rel}",
            )
        )
        diff_patch = "".join(diff_lines)

        return AgentRunResult(
            diff_patch=diff_patch,
            log_text="".join(logs),
            test_output=test_output or "(no test output)",
            pbt_output=pbt_output or "(no pbt output)",
            hidden_pass_at_1=hidden_pass,
            pbt_pass_rate=pbt_pass_rate,
            stall_count=stall_count,
            target_files=[module_rel] if module_path.is_file() else [],
            test_files=[test_rel] if test_path.is_file() else [],
            quota_tokens_used=total_tokens,
            quota_requests_used=total_requests,
        )


__all__ = [
    "LiveLLMCodingAgent",
    "extract_code_payload",
    "parse_pytest_pass_rate",
]
