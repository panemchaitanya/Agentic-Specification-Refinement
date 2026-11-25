"""Deterministic gap-injection engine with strict zero-verbatim-leakage enforcement."""

from __future__ import annotations

from collections.abc import Sequence
import random
import re

from reqhq.benchmark.models import GapClause, GapInjectionResult, GoldGapManifest

_MIN_DROP_RATIO = 0.20
_MAX_DROP_RATIO = 0.40
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_ws(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _verify_zero_verbatim_leakage(
    degraded_prd: str,
    stripped_clauses: Sequence[GapClause],
) -> None:
    """Raise ValueError if any stripped clause text appears verbatim in degraded_prd."""
    norm_prd_lower = _normalize_ws(degraded_prd).lower()
    for clause in stripped_clauses:
        raw_text = clause.text.strip()
        if raw_text and raw_text in degraded_prd:
            raise ValueError(
                f"Verbatim leakage detected: stripped clause {clause.clause_id!r} "
                "appears verbatim in degraded_prd"
            )
        norm_clause = _normalize_ws(raw_text).lower()
        if len(norm_clause) >= 12 and norm_clause in norm_prd_lower:
            raise ValueError(
                f"Normalized verbatim leakage detected: stripped clause {clause.clause_id!r} "
                "appears in degraded_prd"
            )


class GapInjector:
    """Deterministically strips 20%–40% of clauses from a specification and verifies zero leakage."""

    def __init__(self, seed: int = 42) -> None:
        self.seed = seed

    def inject(
        self,
        spec_id: str,
        title: str,
        clauses: Sequence[GapClause],
        *,
        drop_ratio: float = 0.30,
    ) -> GapInjectionResult:
        """Strip round(len * drop_ratio) clauses deterministically and render a degraded PRD."""
        clean_spec_id = spec_id.strip()
        clean_title = title.strip()
        if not clean_spec_id:
            raise ValueError("spec_id must not be blank")
        if not clean_title:
            raise ValueError("title must not be blank")
        if not (_MIN_DROP_RATIO <= drop_ratio <= _MAX_DROP_RATIO):
            raise ValueError(
                f"drop_ratio must be within [{_MIN_DROP_RATIO:.2f}, {_MAX_DROP_RATIO:.2f}], "
                f"got {drop_ratio}"
            )

        clause_list = list(clauses)
        if not clause_list:
            raise ValueError("clauses must contain at least one GapClause")

        seen_ids: set[str] = set()
        for clause in clause_list:
            if clause.clause_id in seen_ids:
                raise ValueError(f"Duplicate clause_id in input clauses: {clause.clause_id!r}")
            seen_ids.add(clause.clause_id)

        total = len(clause_list)
        drop_count = max(1, min(total, round(total * drop_ratio)))

        rng = random.Random(self.seed)
        stripped_indices = set(rng.sample(range(total), drop_count))

        stripped_clauses = [c for idx, c in enumerate(clause_list) if idx in stripped_indices]
        retained_clauses = [c for idx, c in enumerate(clause_list) if idx not in stripped_indices]

        degraded_prd = self._render_degraded_prd(clean_spec_id, clean_title, retained_clauses)
        _verify_zero_verbatim_leakage(degraded_prd, stripped_clauses)

        manifest = GoldGapManifest(
            spec_id=clean_spec_id,
            seed=self.seed,
            drop_ratio=drop_ratio,
            total_clauses=total,
            stripped_clauses=stripped_clauses,
            retained_clauses=retained_clauses,
        )
        return GapInjectionResult(
            degraded_prd=degraded_prd,
            manifest=manifest,
            leakage_verified=True,
        )

    @staticmethod
    def _render_degraded_prd(
        spec_id: str,
        title: str,
        retained_clauses: Sequence[GapClause],
    ) -> str:
        lines = [
            f"# {title}",
            "",
            f"Spec ID: {spec_id}",
            "",
            "## Specification Clauses",
        ]
        if retained_clauses:
            for clause in retained_clauses:
                lines.append(f"- [{clause.clause_id}] ({clause.category.value}) {clause.text}")
        else:
            lines.append("_No clauses retained after gap injection._")
        return "\n".join(lines) + "\n"


__all__ = ["GapInjector"]
