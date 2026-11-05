"""Stage 4 results-table generation - format fixed pre-data.

Produces the markdown tables the final report fills in, so the output
shape cannot be tuned after results exist. Inputs are scored tables from
eval/summarize.score_run (one per baseline) and raw run records for the
deviation flags.
"""
from __future__ import annotations


def paired(scored_a: dict, scored_b: dict, field: str) -> tuple:
    """Per-item paired lists for two baselines, sorted by item id.

    Items missing from either side are dropped (paired tests need both);
    the caller reports how many items were paired.
    """
    items = sorted(set(scored_a) & set(scored_b))
    return (items,
            [scored_a[i][field] for i in items],
            [scored_b[i][field] for i in items])


def nfr_prf_table(scored_by_baseline: dict) -> str:
    """Markdown table: per-baseline mean NFR precision/recall/F1 over items."""
    lines = ["| Baseline | Items | Mean NFR P | Mean NFR R | Mean NFR F1 |",
             "| --- | --- | --- | --- | --- |"]
    for baseline, scored in sorted(scored_by_baseline.items()):
        if not scored:
            continue
        n = len(scored)
        p = sum(s["precision"] for s in scored.values()) / n
        r = sum(s["recall"] for s in scored.values()) / n
        f = sum(s["f1"] for s in scored.values()) / n
        lines.append(f"| {baseline} | {n} | {p:.3f} | {r:.3f} | {f:.3f} |")
    return "\n".join(lines)


def readout_block(readout: dict) -> str:
    """Render one hypothesis readout dict (eval/scoring.py) as markdown."""
    lines = [f"### {readout['hypothesis']}: "
             f"{'SUPPORTED' if readout['supported'] else 'NOT supported'}",
             "", f"Fixed rule: {readout['rule']}", ""]
    skip = {"hypothesis", "supported", "rule"}
    for key, val in readout.items():
        if key in skip:
            continue
        if isinstance(val, float):
            val = f"{val:.4f}"
        lines.append(f"- {key}: {val}")
    return "\n".join(lines)


def deviation_flags(runs: list) -> dict:
    """D1/D2 flags from raw run records.

    d1_items: targeted-arm items where escalations exceeded asked
    questions (the MAX_QUESTIONS cap dropped some).
    conflict_share: share of findings that are conflicts, per arm - near
    zero on real data is the measurable signature of D2.
    """
    d1_items, kind_totals = [], {}
    for r in runs:
        if r.get("n_escalated", 0) > r.get("questions_asked", 0):
            d1_items.append(f"{r['baseline']}/{r['item_id']}/rep{r['repeat']}")
        for kind, n in (r.get("finding_kinds") or {}).items():
            kind_totals[kind] = kind_totals.get(kind, 0) + n
    total = sum(kind_totals.values()) or 1
    return {
        "d1_items": sorted(set(d1_items)),
        "finding_kind_totals": kind_totals,
        "conflict_share": round(kind_totals.get("conflict", 0) / total, 4),
    }
