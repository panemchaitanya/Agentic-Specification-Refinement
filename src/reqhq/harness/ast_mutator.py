"""Deterministic Windows 11 / Python 3.15 native AST mutation engine."""

from __future__ import annotations

import ast
from collections.abc import Sequence
import copy
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from reqhq.harness.models import (
    MutantOutcome,
    MutantRecord,
    MutationReport,
    compute_mutation_score,
)

_COMPARE_OP_MAP: dict[type[ast.cmpop], tuple[type[ast.cmpop], str]] = {
    ast.Lt: (ast.LtE, "rel_lt_to_lte"),
    ast.LtE: (ast.Lt, "rel_lte_to_lt"),
    ast.Gt: (ast.GtE, "rel_gt_to_gte"),
    ast.GtE: (ast.Gt, "rel_gte_to_gt"),
    ast.Eq: (ast.NotEq, "rel_eq_to_neq"),
    ast.NotEq: (ast.Eq, "rel_neq_to_eq"),
}

_BOOL_OP_MAP: dict[type[ast.boolop], tuple[type[ast.boolop], str]] = {
    ast.And: (ast.Or, "bool_and_to_or"),
    ast.Or: (ast.And, "bool_or_to_and"),
}

_ARITH_OP_MAP: dict[type[ast.operator], tuple[type[ast.operator], str]] = {
    ast.Add: (ast.Sub, "arith_add_to_sub"),
    ast.Sub: (ast.Add, "arith_sub_to_add"),
}

_IGNORE_PATTERNS = shutil.ignore_patterns(
    "__pycache__",
    ".pytest_cache",
    ".git",
    ".venv",
    "*.pyc",
)


@dataclass(frozen=True)
class MutantCandidate:
    """Single deterministic AST mutation prepared for execution."""

    mutant_id: str
    rel_path: str
    line_no: int
    operator: str
    original_snippet: str
    mutated_snippet: str
    mutated_source: str


def _preorder_nodes(tree: ast.AST) -> list[ast.AST]:
    """Return every AST node in deterministic preorder DFS sequence."""
    ordered: list[ast.AST] = []

    def _visit(node: ast.AST) -> None:
        ordered.append(node)
        for child in ast.iter_child_nodes(node):
            _visit(child)

    _visit(tree)
    return ordered


def _replace_child_in_parent(
    root: ast.AST,
    target_node: ast.AST,
    replacement: ast.AST,
) -> bool:
    """Replace an exact AST node reference inside its parent field or list."""
    for parent in _preorder_nodes(root):
        for field_name, value in ast.iter_fields(parent):
            if value is target_node:
                setattr(parent, field_name, replacement)
                return True
            if isinstance(value, list):
                for idx, item in enumerate(value):
                    if item is target_node:
                        value[idx] = replacement
                        return True
    return False


def _discover_sites(tree: ast.AST) -> list[tuple[int, str, int]]:
    """Return deterministic mutation sites as (node_index, site_kind, sub_index)."""
    nodes = _preorder_nodes(tree)
    sites: list[tuple[int, str, int]] = []

    for idx, node in enumerate(nodes):
        if isinstance(node, ast.Compare):
            for op_idx, op in enumerate(node.ops):
                if type(op) in _COMPARE_OP_MAP:
                    sites.append((idx, "compare", op_idx))
        elif isinstance(node, ast.BoolOp) and type(node.op) in _BOOL_OP_MAP:
            sites.append((idx, "boolop", 0))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            sites.append((idx, "not_remove", 0))
        elif isinstance(node, ast.BinOp) and type(node.op) in _ARITH_OP_MAP:
            sites.append((idx, "arith", 0))
        elif isinstance(node, ast.Return) and node.value is not None:
            sites.append((idx, "return", 0))
        elif isinstance(node, ast.Constant) and type(node.value) is int:
            sites.append((idx, "boundary_int", 0))

    return sites


def _apply_site(
    base_tree: ast.AST,
    node_idx: int,
    site_kind: str,
    sub_index: int,
) -> tuple[str, int, str, str, str]:
    """Clone base_tree, apply one mutation site, and return metadata + unparsed source."""
    mut_tree = copy.deepcopy(base_tree)
    nodes = _preorder_nodes(mut_tree)
    target = nodes[node_idx]
    line_no = max(1, int(getattr(target, "lineno", 1)))
    original_snippet = ast.unparse(target)

    if site_kind == "compare":
        assert isinstance(target, ast.Compare)
        new_op_cls, op_name = _COMPARE_OP_MAP[type(target.ops[sub_index])]
        target.ops[sub_index] = new_op_cls()
        mutated_snippet = ast.unparse(target)
    elif site_kind == "boolop":
        assert isinstance(target, ast.BoolOp)
        new_op_cls, op_name = _BOOL_OP_MAP[type(target.op)]
        target.op = new_op_cls()
        mutated_snippet = ast.unparse(target)
    elif site_kind == "not_remove":
        assert isinstance(target, ast.UnaryOp)
        op_name = "bool_remove_not"
        replacement = target.operand
        mutated_snippet = ast.unparse(replacement)
        _replace_child_in_parent(mut_tree, target, replacement)
    elif site_kind == "arith":
        assert isinstance(target, ast.BinOp)
        new_op_cls, op_name = _ARITH_OP_MAP[type(target.op)]
        target.op = new_op_cls()
        mutated_snippet = ast.unparse(target)
    elif site_kind == "return":
        assert isinstance(target, ast.Return)
        if isinstance(target.value, ast.Constant) and type(target.value.value) is bool:
            target.value = ast.Constant(value=not target.value.value)
            op_name = "return_flip_bool"
        else:
            target.value = ast.Constant(value=None)
            op_name = "return_to_none"
        mutated_snippet = ast.unparse(target)
    else:  # boundary_int
        assert isinstance(target, ast.Constant) and type(target.value) is int
        orig_val = target.value
        if orig_val == 0:
            target.value = 1
            op_name = "const_zero_to_one"
        else:
            target.value = orig_val + 1
            op_name = "const_int_inc"
        mutated_snippet = ast.unparse(target)

    ast.fix_missing_locations(mut_tree)
    mutated_source = ast.unparse(mut_tree) + "\n"
    ast.parse(mutated_source)
    return op_name, line_no, original_snippet, mutated_snippet, mutated_source


def generate_mutants(
    source: str,
    rel_path: str,
    *,
    max_mutants: int | None = None,
    start_index: int = 1,
) -> list[MutantCandidate]:
    """Generate deterministic single-site AST mutants for one Python source string."""
    if max_mutants is not None and max_mutants <= 0:
        return []

    tree = ast.parse(source)
    sites = _discover_sites(tree)
    candidates: list[MutantCandidate] = []
    counter = start_index

    for node_idx, site_kind, sub_index in sites:
        if max_mutants is not None and len(candidates) >= max_mutants:
            break
        op_name, line_no, orig_snip, mut_snip, mut_src = _apply_site(
            tree,
            node_idx,
            site_kind,
            sub_index,
        )
        candidates.append(
            MutantCandidate(
                mutant_id=f"MUT-{counter:04d}",
                rel_path=rel_path,
                line_no=line_no,
                operator=op_name,
                original_snippet=orig_snip,
                mutated_snippet=mut_snip,
                mutated_source=mut_src,
            )
        )
        counter += 1

    return candidates


def _resolve_relative_path(workspace: Path, raw_path: Path | str) -> Path:
    """Resolve a target or test path strictly inside workspace."""
    candidate = Path(raw_path)
    full = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    try:
        rel = full.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(
            f"Path {raw_path!r} escapes workspace {workspace}"
        ) from exc
    if not full.exists():
        raise FileNotFoundError(f"Path does not exist inside workspace: {full}")
    return rel


class ASTMutantEngine:
    """Windows-native deterministic Python AST mutation generator and test runner."""

    runner_name: str = "ast_mutant_engine"

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
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

    def _build_env(self, sandbox: Path) -> dict[str, str]:
        env = os.environ.copy()
        extra_paths: list[str] = [str(sandbox)]
        src_dir = sandbox / "src"
        if src_dir.is_dir():
            extra_paths.insert(0, str(src_dir))
        existing = env.get("PYTHONPATH", "")
        if existing:
            extra_paths.append(existing)
        env["PYTHONPATH"] = os.pathsep.join(extra_paths)
        return env

    def _evaluate_candidate(
        self,
        workspace: Path,
        candidate: MutantCandidate,
        rel_test_paths: list[str],
        timeout_seconds: float,
    ) -> MutantRecord:
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(
            prefix="reqhq_mut_",
            ignore_cleanup_errors=True,
        ) as tmp_dir:
            sandbox = Path(tmp_dir) / "work"
            shutil.copytree(workspace, sandbox, ignore=_IGNORE_PATTERNS)
            target_file = sandbox / candidate.rel_path
            target_file.write_text(candidate.mutated_source, encoding="utf-8", newline="\n")

            cmd = [
                self.python_executable,
                "-m",
                "pytest",
                "-q",
                "--tb=no",
                "-o",
                "addopts=",
                *rel_test_paths,
            ]
            env = self._build_env(sandbox)
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(sandbox),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                outcome = MutantOutcome.TIMEOUT
            except OSError:
                outcome = MutantOutcome.ERROR
            else:
                if proc.returncode == 0:
                    outcome = MutantOutcome.SURVIVED
                elif proc.returncode == 1:
                    outcome = MutantOutcome.KILLED
                else:
                    outcome = MutantOutcome.ERROR

        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
        return MutantRecord(
            mutant_id=candidate.mutant_id,
            file_path=candidate.rel_path,
            line_no=candidate.line_no,
            operator=candidate.operator,
            original_snippet=candidate.original_snippet,
            mutated_snippet=candidate.mutated_snippet,
            outcome=outcome,
            elapsed_ms=elapsed_ms,
        )

    def run(
        self,
        workspace_dir: Path | str,
        target_files: Sequence[Path | str],
        test_paths: Sequence[Path | str] | None = None,
        *,
        timeout_seconds: float | None = None,
        max_mutants: int | None = None,
    ) -> MutationReport:
        """Generate and evaluate AST mutants in isolated temp workspaces."""
        workspace = Path(workspace_dir).resolve()
        if not workspace.is_dir():
            raise FileNotFoundError(f"Workspace directory not found: {workspace}")

        eff_timeout = self.timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        if eff_timeout <= 0.0:
            raise ValueError("timeout_seconds must be > 0")
        eff_max = self.max_mutants if max_mutants is None else int(max_mutants)
        if eff_max < 0:
            raise ValueError("max_mutants must be >= 0")

        rel_targets: list[str] = []
        for raw_target in target_files:
            rel = _resolve_relative_path(workspace, raw_target)
            rel_targets.append(rel.as_posix())

        rel_tests: list[str] = []
        if test_paths is not None:
            for raw_test in test_paths:
                rel = _resolve_relative_path(workspace, raw_test)
                rel_tests.append(rel.as_posix())

        candidates: list[MutantCandidate] = []
        next_idx = 1
        for rel_target in rel_targets:
            remaining = eff_max - len(candidates)
            if remaining <= 0:
                break
            source = (workspace / rel_target).read_text(encoding="utf-8")
            file_candidates = generate_mutants(
                source,
                rel_target,
                max_mutants=remaining,
                start_index=next_idx,
            )
            candidates.extend(file_candidates)
            next_idx += len(file_candidates)

        records: list[MutantRecord] = [
            self._evaluate_candidate(workspace, cand, rel_tests, eff_timeout)
            for cand in candidates
        ]

        killed = sum(1 for r in records if r.outcome == MutantOutcome.KILLED)
        survived = sum(1 for r in records if r.outcome == MutantOutcome.SURVIVED)
        timeout = sum(1 for r in records if r.outcome == MutantOutcome.TIMEOUT)
        errors = sum(1 for r in records if r.outcome == MutantOutcome.ERROR)
        score = compute_mutation_score(killed, survived, timeout, errors)

        return MutationReport(
            runner_name=self.runner_name,
            target_files=rel_targets,
            total_mutants=len(records),
            killed=killed,
            survived=survived,
            timeout=timeout,
            errors=errors,
            mutation_score=score,
            mutants=records,
        )


__all__ = [
    "ASTMutantEngine",
    "MutantCandidate",
    "generate_mutants",
]
