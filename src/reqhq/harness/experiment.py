"""Condition A/B/C experiment harness, CodingAgent protocol, and deterministic MockCodingAgent."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import csv
import json
from pathlib import Path
import time
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from reqhq.harness.models import (
    RESULTS_CSV_FIELDNAMES,
    ExperimentCondition,
    MutationReport,
    TrialResultRow,
)
from reqhq.harness.mutation import MutationRunner, select_mutation_runner
from reqhq.llm.quota_ledger import QuotaLedger


class AgentRunResult(BaseModel):
    """Structured telemetry and artifact text returned by a CodingAgent trial."""

    model_config = ConfigDict(extra="forbid")

    diff_patch: str = ""
    log_text: str = ""
    test_output: str = ""
    pbt_output: str = ""
    hidden_pass_at_1: bool
    pbt_pass_rate: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    stall_count: int = Field(default=0, ge=0)
    target_files: list[str] = Field(default_factory=list)
    test_files: list[str] = Field(default_factory=list)
    quota_tokens_used: int = Field(default=0, ge=0)
    quota_requests_used: int = Field(default=0, ge=0)


@runtime_checkable
class CodingAgent(Protocol):
    """Protocol for an automated coding agent evaluated under Condition A/B/C."""

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
        """Execute a single coding trial inside workspace_dir and return results."""


def _write_lf(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = content.replace("\r\n", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(normalized)


def append_trial_result_csv(csv_path: Path | str, row: TrialResultRow) -> Path:
    """Append a single TrialResultRow to results.csv with strict deterministic headers."""
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = (not path.exists()) or (path.stat().st_size == 0)

    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(RESULTS_CSV_FIELDNAMES),
            lineterminator="\n",
        )
        if needs_header:
            writer.writeheader()
        writer.writerow(row.to_csv_dict())
    return path


def read_trial_results_csv(csv_path: Path | str) -> list[TrialResultRow]:
    """Read and strictly validate every row in a results.csv file."""
    path = Path(csv_path)
    rows: list[TrialResultRow] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != RESULTS_CSV_FIELDNAMES:
            raise ValueError(
                f"Invalid results.csv header: expected {RESULTS_CSV_FIELDNAMES}, "
                f"got {tuple(reader.fieldnames or ())}"
            )
        for raw in reader:
            rows.append(
                TrialResultRow(
                    trial_id=raw["trial_id"],
                    case_id=raw["case_id"],
                    condition=ExperimentCondition(raw["condition"]),
                    seed=int(raw["seed"]),
                    hidden_pass_at_1=raw["hidden_pass_at_1"].strip().lower() == "true",
                    pbt_pass_rate=float(raw["pbt_pass_rate"]),
                    mutation_score=float(raw["mutation_score"]),
                    lock_verified=raw["lock_verified"].strip().lower() == "true",
                    stall_count=int(raw["stall_count"]),
                    wall_clock_seconds=float(raw["wall_clock_seconds"]),
                    quota_tokens_used=int(raw["quota_tokens_used"]),
                    quota_requests_used=int(raw["quota_requests_used"]),
                    artifacts_dir=raw["artifacts_dir"],
                )
            )
    return rows


class MockCodingAgent:
    """Deterministic offline CodingAgent simulating Condition A/B/C runs."""

    def __init__(
        self,
        *,
        tamper_lock: bool = False,
        simulate_timeout: bool = False,
    ) -> None:
        self.tamper_lock = bool(tamper_lock)
        self.simulate_timeout = bool(simulate_timeout)

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
        """Synthesize deterministic code + tests for the requested condition."""
        if self.simulate_timeout:
            raise TimeoutError(
                f"MockCodingAgent timed out after {timeout_seconds:.2f}s on {case_id}"
            )

        workspace_dir.mkdir(parents=True, exist_ok=True)
        module_path = workspace_dir / "domain_logic.py"
        test_path = workspace_dir / "test_domain_logic.py"

        if condition == ExperimentCondition.COND_A:
            module_src = (
                "def check_quota(used: int, limit: int, active: bool) -> bool:\n"
                "    return used < limit and active\n"
            )
            test_src = (
                "from domain_logic import check_quota\n\n"
                "def test_basic_quota() -> None:\n"
                "    assert check_quota(1, 10, True) is True\n"
            )
            hidden_pass = False
            pbt_pass_rate = 0.5
            stall_count = 1
            tokens_used = 420
            requests_used = 2
            test_output = "1 passed (weak unit coverage; boundary cases untested)\n"
            pbt_output = "Falsifying example: used=10, limit=10, active=True (1/2 passed)\n"
        elif condition == ExperimentCondition.COND_B:
            module_src = (
                "def check_quota(used: int, limit: int, active: bool) -> bool:\n"
                "    return used <= limit and active\n"
            )
            test_src = (
                "from domain_logic import check_quota\n\n"
                "def test_nominal_and_inactive() -> None:\n"
                "    assert check_quota(5, 10, True) is True\n"
                "    assert check_quota(5, 10, False) is False\n"
            )
            hidden_pass = (seed % 2) == 0
            pbt_pass_rate = 0.8
            stall_count = 0
            tokens_used = 680
            requests_used = 3
            test_output = "2 passed (moderate coverage)\n"
            pbt_output = "4/5 properties passed\n"
        else:
            module_src = (
                "def check_quota(used: int, limit: int, active: bool) -> bool:\n"
                "    return used <= limit and active\n"
            )
            test_src = (
                "from domain_logic import check_quota\n\n"
                "def test_quota_boundaries() -> None:\n"
                "    assert check_quota(10, 10, True) is True\n"
                "    assert check_quota(11, 10, True) is False\n"
                "    assert check_quota(5, 10, False) is False\n"
                "    assert check_quota(11, 10, False) is False\n"
            )
            hidden_pass = True
            pbt_pass_rate = 1.0
            stall_count = 0
            tokens_used = 950
            requests_used = 4
            test_output = "4 passed (full boundary + contract coverage)\n"
            pbt_output = "All Hypothesis properties passed (derandomize=True)\n"

            if self.tamper_lock and bundle_dir is not None:
                pbt_yaml = Path(bundle_dir) / "PBT_Spec.yaml"
                if pbt_yaml.exists():
                    existing = pbt_yaml.read_text(encoding="utf-8")
                    _write_lf(pbt_yaml, existing + "# tampered_by_mock_agent\n")

        _write_lf(module_path, module_src)
        _write_lf(test_path, test_src)

        diff_patch = (
            f"--- /dev/null\n"
            f"+++ b/domain_logic.py\n"
            f"@@ -0,0 +1,2 @@\n"
            + "".join(f"+{line}\n" for line in module_src.splitlines())
        )
        log_text = (
            f"[MockCodingAgent] case={case_id} condition={condition.value} "
            f"seed={seed} timeout={timeout_seconds}s tamper_lock={self.tamper_lock}\n"
        )

        return AgentRunResult(
            diff_patch=diff_patch,
            log_text=log_text,
            test_output=test_output,
            pbt_output=pbt_output,
            hidden_pass_at_1=hidden_pass,
            pbt_pass_rate=pbt_pass_rate,
            stall_count=stall_count,
            target_files=["domain_logic.py"],
            test_files=["test_domain_logic.py"],
            quota_tokens_used=tokens_used,
            quota_requests_used=requests_used,
        )


class ExperimentHarness:
    """Reproducible Condition A/B/C trial runner with contract-lock & mutation verification."""

    def __init__(
        self,
        *,
        agent: CodingAgent | None = None,
        mutation_runner: MutationRunner | None = None,
        quota_ledger: QuotaLedger | None = None,
        timeout_seconds: float = 30.0,
        run_mutation: bool = True,
    ) -> None:
        if timeout_seconds <= 0.0:
            raise ValueError("timeout_seconds must be > 0")
        self.agent: CodingAgent = agent if agent is not None else MockCodingAgent()
        self.mutation_runner: MutationRunner = (
            mutation_runner
            if mutation_runner is not None
            else select_mutation_runner(timeout_seconds=timeout_seconds)
        )
        self.quota_ledger = quota_ledger
        self.timeout_seconds = float(timeout_seconds)
        self.run_mutation = bool(run_mutation)

    def _snapshot_quota_totals(self) -> tuple[int, int]:
        """Read cumulative daily tokens and requests from optional QuotaLedger."""
        if self.quota_ledger is None:
            return 0, 0
        status = self.quota_ledger.get_status()
        tokens = 0
        requests = 0
        for info in status.values():
            if info.get("configured"):
                tokens += int(info.get("tpd_used", 0))
                requests += int(info.get("rpd_used", 0))
        return tokens, requests

    def _verify_cond_c_lock(
        self,
        bundle_dir: Path | None,
    ) -> tuple[bool, str, Exception | None]:
        """Verify locked PBT artifacts for Condition C, failing closed on tamper or absence."""
        if bundle_dir is None:
            return False, "Condition C missing bundle_dir; contract lock unverified.\n", None

        from reqhq.pbt import ContractTamperError, verify_contract_lock

        try:
            verify_contract_lock(bundle_dir)
        except ContractTamperError as exc:
            return False, f"CONTRACT TAMPER REJECTED: {exc}\n", exc
        return True, "Contract lock verified (pbt_lock.sha256 OK).\n", None

    def run_trial(
        self,
        *,
        case_id: str,
        condition: ExperimentCondition,
        seed: int,
        out_dir: Path | str,
        bundle_dir: Path | str | None = None,
        trial_id: str | None = None,
        timeout_seconds: float | None = None,
        fail_on_tamper: bool = False,
    ) -> TrialResultRow:
        """Execute one Condition A/B/C trial, persist artifacts, and append to results.csv."""
        eff_timeout = self.timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        if eff_timeout <= 0.0:
            raise ValueError("timeout_seconds must be > 0")

        out_root = Path(out_dir)
        resolved_trial_id = trial_id or f"{case_id}__{condition.value}__seed{seed}"
        artifacts_dir = out_root / "trials" / resolved_trial_id
        workspace_dir = artifacts_dir / "workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        bundle_path = Path(bundle_dir) if bundle_dir is not None else None
        tokens_before, reqs_before = self._snapshot_quota_totals()
        started = time.perf_counter()

        timed_out = False
        try:
            agent_result = self.agent.run_trial(
                case_id=case_id,
                condition=condition,
                seed=seed,
                workspace_dir=workspace_dir,
                bundle_dir=bundle_path,
                timeout_seconds=eff_timeout,
            )
        except TimeoutError as exc:
            timed_out = True
            agent_result = AgentRunResult(
                diff_patch="",
                log_text=f"TIMEOUT: {exc}\n",
                test_output="TIMEOUT\n",
                pbt_output="TIMEOUT\n",
                hidden_pass_at_1=False,
                pbt_pass_rate=0.0,
                stall_count=1,
            )

        log_chunks = [agent_result.log_text]
        tamper_exc: Exception | None = None

        if condition == ExperimentCondition.COND_C:
            lock_verified, lock_msg, tamper_exc = self._verify_cond_c_lock(bundle_path)
            log_chunks.append(lock_msg)
        else:
            lock_verified = False

        if condition == ExperimentCondition.COND_C and not lock_verified:
            hidden_pass = False
            pbt_pass_rate = 0.0
            mutation_report = MutationReport(
                runner_name=self.mutation_runner.runner_name,
                target_files=list(agent_result.target_files),
                total_mutants=0,
                killed=0,
                survived=0,
                timeout=0,
                errors=0,
                mutation_score=0.0,
                mutants=[],
            )
        elif self.run_mutation and (not timed_out) and agent_result.target_files:
            hidden_pass = agent_result.hidden_pass_at_1
            pbt_pass_rate = agent_result.pbt_pass_rate
            mutation_report = self.mutation_runner.run(
                workspace_dir,
                agent_result.target_files,
                test_paths=agent_result.test_files or None,
                timeout_seconds=eff_timeout,
            )
        else:
            hidden_pass = agent_result.hidden_pass_at_1
            pbt_pass_rate = agent_result.pbt_pass_rate
            mutation_report = MutationReport(
                runner_name=self.mutation_runner.runner_name,
                target_files=list(agent_result.target_files),
                total_mutants=0,
                killed=0,
                survived=0,
                timeout=0,
                errors=0,
                mutation_score=0.0,
                mutants=[],
            )

        wall_clock = round(max(0.0, time.perf_counter() - started), 6)
        tokens_after, reqs_after = self._snapshot_quota_totals()
        ledger_tokens_delta = max(0, tokens_after - tokens_before)
        ledger_reqs_delta = max(0, reqs_after - reqs_before)

        quota_tokens_used = ledger_tokens_delta + agent_result.quota_tokens_used
        quota_requests_used = ledger_reqs_delta + agent_result.quota_requests_used

        _write_lf(artifacts_dir / "diff.patch", agent_result.diff_patch)
        _write_lf(artifacts_dir / "log.txt", "".join(log_chunks))
        _write_lf(artifacts_dir / "test_output.txt", agent_result.test_output)
        _write_lf(artifacts_dir / "pbt_output.txt", agent_result.pbt_output)
        _write_lf(
            artifacts_dir / "mutation_report.json",
            json.dumps(mutation_report.model_dump(mode="json"), indent=2, sort_keys=True),
        )

        row = TrialResultRow(
            trial_id=resolved_trial_id,
            case_id=case_id,
            condition=condition,
            seed=seed,
            hidden_pass_at_1=hidden_pass,
            pbt_pass_rate=pbt_pass_rate,
            mutation_score=mutation_report.mutation_score,
            lock_verified=lock_verified,
            stall_count=agent_result.stall_count,
            wall_clock_seconds=wall_clock,
            quota_tokens_used=quota_tokens_used,
            quota_requests_used=quota_requests_used,
            artifacts_dir=artifacts_dir.as_posix(),
        )
        append_trial_result_csv(out_root / "results.csv", row)

        if fail_on_tamper and tamper_exc is not None:
            raise tamper_exc

        return row

    def run_experiment(
        self,
        *,
        case_ids: Sequence[str],
        out_dir: Path | str,
        conditions: Sequence[ExperimentCondition] = (
            ExperimentCondition.COND_A,
            ExperimentCondition.COND_B,
            ExperimentCondition.COND_C,
        ),
        seeds: Sequence[int] = (42,),
        bundle_dirs: Mapping[str, Path | str] | None = None,
        fail_on_tamper: bool = False,
    ) -> list[TrialResultRow]:
        """Run all (case_id, condition, seed) trials in deterministic order."""
        bundle_map = bundle_dirs or {}
        rows: list[TrialResultRow] = []
        for case_id in case_ids:
            case_bundle = bundle_map.get(case_id)
            for condition in conditions:
                for seed in seeds:
                    row = self.run_trial(
                        case_id=case_id,
                        condition=condition,
                        seed=seed,
                        out_dir=out_dir,
                        bundle_dir=case_bundle,
                        fail_on_tamper=fail_on_tamper,
                    )
                    rows.append(row)
        return rows


__all__ = [
    "AgentRunResult",
    "CodingAgent",
    "ExperimentHarness",
    "MockCodingAgent",
    "append_trial_result_csv",
    "read_trial_results_csv",
]
