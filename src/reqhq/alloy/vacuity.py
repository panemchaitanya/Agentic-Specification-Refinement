"""Anti-vacuity witness injection for Alloy behavioral models."""

from __future__ import annotations

import re

_ASSERT_RE = re.compile(r"^\s*assert\s+([A-Za-z_][A-Za-z0-9_']*)\b", re.MULTILINE)
_CHECK_RE = re.compile(r"^\s*check\s+([A-Za-z_][A-Za-z0-9_']*)\b", re.MULTILINE)
_EXISTING_WITNESS_RE = re.compile(
    r"^\s*pred\s+(__vacuity_witness_[A-Za-z0-9_]+)\b",
    re.MULTILINE,
)


def _bounded_scope_and_bitwidth(max_scope: int) -> tuple[int, int]:
    if max_scope < 1:
        raise ValueError("max_scope must be >= 1")
    bitwidth = min(4, max(2, max_scope))
    return max_scope, bitwidth


def inject_vacuity_witnesses(
    behavior_als: str,
    *,
    max_scope: int = 4,
) -> tuple[str, list[str]]:
    """Inject paired non-trivial `pred __vacuity_witness_<name>` + `run` checks.

    Ensures that every assertion/check has a paired satisfiability witness so
    contradictory facts (vacuous truth / UNSAT) are always caught and rejected.
    """
    scope, bitwidth = _bounded_scope_and_bitwidth(max_scope)

    target_names: list[str] = []
    seen: set[str] = set()
    for pattern in (_ASSERT_RE, _CHECK_RE):
        for match in pattern.finditer(behavior_als):
            name = match.group(1)
            if name not in seen:
                seen.add(name)
                target_names.append(name)

    if not target_names:
        target_names = ["model"]

    existing = set(_EXISTING_WITNESS_RE.findall(behavior_als))
    witness_names: list[str] = []
    appended_blocks: list[str] = []

    for target in target_names:
        safe_target = re.sub(r"[^A-Za-z0-9_]", "_", target)
        witness_name = f"__vacuity_witness_{safe_target}"
        witness_names.append(witness_name)
        if witness_name in existing:
            continue
        appended_blocks.append(
            f"pred {witness_name} {{\n"
            f"  some univ\n"
            f"}}\n"
            f"run {witness_name} for {scope} but {bitwidth} int"
        )

    if not appended_blocks:
        return behavior_als, witness_names

    base = behavior_als.rstrip()
    separator = "\n\n// --- Auto-injected anti-vacuity witnesses ---\n"
    augmented = base + separator + "\n\n".join(appended_blocks) + "\n"
    return augmented, witness_names


__all__ = ["inject_vacuity_witnesses"]
