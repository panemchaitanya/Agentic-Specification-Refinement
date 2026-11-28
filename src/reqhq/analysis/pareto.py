"""Pure-Python Pareto frontier computation and zero-dependency valid XML/SVG chart generators."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

from reqhq.analysis.models import ErrorTaxonomyReport, ParetoPoint

_XML_ENTITIES = {'"': "&quot;", "'": "&apos;"}

_CHART_FILENAMES: dict[str, str] = {
    "pareto_quality_vs_cloud_cost": "pareto_quality_vs_cloud_cost.svg",
    "pareto_quality_vs_latency": "pareto_quality_vs_latency.svg",
    "cond_abc_comparison": "cond_abc_comparison.svg",
    "error_taxonomy_bar": "error_taxonomy_bar.svg",
}


def _esc(text: str) -> str:
    """XML-escape arbitrary text for safe SVG element and attribute interpolation."""
    return _xml_escape(str(text), _XML_ENTITIES)


def compute_pareto_frontier(points: Sequence[ParetoPoint]) -> list[ParetoPoint]:
    """Compute exact Pareto frontier maximizing `quality` and minimizing `cost`.

    Point `q` dominates `p` iff `q.quality >= p.quality`, `q.cost <= p.cost`,
    and at least one inequality is strict. Returns a new list preserving input order.
    """
    for idx, pt in enumerate(points):
        if not (math.isfinite(pt.quality) and math.isfinite(pt.cost)):
            raise ValueError(
                f"ParetoPoint at index {idx} ({pt.label!r}) has non-finite values: "
                f"quality={pt.quality!r}, cost={pt.cost!r}"
            )

    result: list[ParetoPoint] = []
    for i, p in enumerate(points):
        is_dominated = any(
            (q.quality >= p.quality and q.cost <= p.cost)
            and (q.quality > p.quality or q.cost < p.cost)
            for j, q in enumerate(points)
            if j != i
        )
        result.append(
            ParetoPoint(
                label=p.label,
                quality=p.quality,
                cost=p.cost,
                is_frontier=not is_dominated,
            )
        )
    return result


def _watermark_element(width: int, height: int) -> str:
    cx = width / 2.0
    cy = height / 2.0
    return (
        f'  <g opacity="0.22" transform="rotate(-24 {cx:.1f} {cy:.1f})">\n'
        f'    <text x="{cx:.1f}" y="{cy:.1f}" text-anchor="middle" '
        f'font-family="Arial, sans-serif" font-size="40" font-weight="bold" fill="#dc2626">'
        f"DRY-RUN MOCK DATA</text>\n"
        f"  </g>\n"
    )


def _padded_bounds(values: Sequence[float]) -> tuple[float, float]:
    lo = min(values)
    hi = max(values)
    span = hi - lo
    if span == 0.0:
        pad = max(abs(lo) * 0.1, 1.0)
        return lo - pad, hi + pad
    pad = span * 0.12
    return lo - pad, hi + pad


def render_pareto_svg(
    points: Sequence[ParetoPoint],
    *,
    title: str,
    x_label: str,
    y_label: str,
    dry_run_watermark: bool = False,
    width: int = 760,
    height: int = 440,
) -> str:
    """Render a zero-dependency valid XML/SVG scatter plot with Pareto frontier polyline."""
    evaluated = compute_pareto_frontier(points)
    left, right, top, bottom = 84.0, 40.0, 56.0, 74.0
    plot_w = width - left - right
    plot_h = height - top - bottom

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">\n',
        f'  <rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>\n',
        f'  <text x="{width / 2:.1f}" y="30" text-anchor="middle" '
        f'font-family="Arial, sans-serif" font-size="18" font-weight="bold" fill="#0f172a">'
        f"{_esc(title)}</text>\n",
    ]

    if not evaluated:
        parts.append(
            f'  <rect x="{left:.1f}" y="{top:.1f}" width="{plot_w:.1f}" height="{plot_h:.1f}" '
            f'fill="#f8fafc" stroke="#cbd5e1" stroke-width="1"/>\n'
        )
        parts.append(
            f'  <text x="{left + plot_w / 2:.1f}" y="{top + plot_h / 2:.1f}" text-anchor="middle" '
            f'font-family="Arial, sans-serif" font-size="14" fill="#64748b">'
            f"No Pareto points available</text>\n"
        )
    else:
        x_min, x_max = _padded_bounds([p.cost for p in evaluated])
        y_min, y_max = _padded_bounds([p.quality for p in evaluated])

        def sx(val: float) -> float:
            return left + ((val - x_min) / (x_max - x_min)) * plot_w

        def sy(val: float) -> float:
            return top + plot_h - ((val - y_min) / (y_max - y_min)) * plot_h

        parts.append(
            f'  <rect x="{left:.1f}" y="{top:.1f}" width="{plot_w:.1f}" height="{plot_h:.1f}" '
            f'fill="#f8fafc" stroke="#cbd5e1" stroke-width="1"/>\n'
        )

        # Gridlines and ticks
        for step in range(5):
            frac = step / 4.0
            gx = left + frac * plot_w
            xv = x_min + frac * (x_max - x_min)
            parts.append(
                f'  <line x1="{gx:.1f}" y1="{top:.1f}" x2="{gx:.1f}" y2="{top + plot_h:.1f}" '
                f'stroke="#e2e8f0" stroke-width="1"/>\n'
            )
            parts.append(
                f'  <text x="{gx:.1f}" y="{top + plot_h + 18:.1f}" text-anchor="middle" '
                f'font-family="Arial, sans-serif" font-size="11" fill="#475569">{xv:.2f}</text>\n'
            )

            gy = top + plot_h - frac * plot_h
            yv = y_min + frac * (y_max - y_min)
            parts.append(
                f'  <line x1="{left:.1f}" y1="{gy:.1f}" x2="{left + plot_w:.1f}" y2="{gy:.1f}" '
                f'stroke="#e2e8f0" stroke-width="1"/>\n'
            )
            parts.append(
                f'  <text x="{left - 10:.1f}" y="{gy + 4:.1f}" text-anchor="end" '
                f'font-family="Arial, sans-serif" font-size="11" fill="#475569">{yv:.2f}</text>\n'
            )

        # Frontier polyline
        frontier_pts = sorted(
            (p for p in evaluated if p.is_frontier),
            key=lambda p: (p.cost, -p.quality),
        )
        if len(frontier_pts) >= 2:
            poly_coords = " ".join(f"{sx(p.cost):.1f},{sy(p.quality):.1f}" for p in frontier_pts)
            parts.append(
                f'  <polyline points="{poly_coords}" fill="none" stroke="#0f766e" '
                f'stroke-width="2.5" stroke-dasharray="6,4"/>\n'
            )

        # Scatter points and labels
        for pt in evaluated:
            px = sx(pt.cost)
            py = sy(pt.quality)
            fill = "#0f766e" if pt.is_frontier else "#94a3b8"
            radius = 6 if pt.is_frontier else 5
            parts.append(
                f'  <circle cx="{px:.1f}" cy="{py:.1f}" r="{radius}" fill="{fill}" '
                f'stroke="#0f172a" stroke-width="1"/>\n'
            )
            parts.append(
                f'  <text x="{px + 8:.1f}" y="{py - 6:.1f}" '
                f'font-family="Arial, sans-serif" font-size="11" fill="#1e293b">'
                f"{_esc(pt.label)}</text>\n"
            )

    # Axes labels
    parts.append(
        f'  <text x="{left + plot_w / 2:.1f}" y="{height - 20:.1f}" text-anchor="middle" '
        f'font-family="Arial, sans-serif" font-size="13" font-weight="bold" fill="#1e293b">'
        f"{_esc(x_label)}</text>\n"
    )
    parts.append(
        f'  <text x="22" y="{top + plot_h / 2:.1f}" text-anchor="middle" '
        f'transform="rotate(-90 22 {top + plot_h / 2:.1f})" '
        f'font-family="Arial, sans-serif" font-size="13" font-weight="bold" fill="#1e293b">'
        f"{_esc(y_label)}</text>\n"
    )

    # Legend
    lx = width - 210
    parts.append(
        f'  <rect x="{lx}" y="62" width="160" height="46" rx="4" fill="#ffffff" '
        f'stroke="#cbd5e1" stroke-width="1"/>\n'
        f'  <circle cx="{lx + 14}" cy="78" r="5" fill="#0f766e" stroke="#0f172a" stroke-width="1"/>\n'
        f'  <text x="{lx + 26}" y="82" font-family="Arial, sans-serif" font-size="11" '
        f'fill="#1e293b">Pareto Frontier</text>\n'
        f'  <circle cx="{lx + 14}" cy="96" r="5" fill="#94a3b8" stroke="#0f172a" stroke-width="1"/>\n'
        f'  <text x="{lx + 26}" y="100" font-family="Arial, sans-serif" font-size="11" '
        f'fill="#475569">Dominated</text>\n'
    )

    if dry_run_watermark:
        parts.append(_watermark_element(width, height))

    parts.append("</svg>\n")
    return "".join(parts)


def render_cond_abc_svg(
    cond_metrics: dict[str, dict[str, float]],
    *,
    title: str = "Condition A / B / C Comparison",
    dry_run_watermark: bool = False,
    width: int = 760,
    height: int = 440,
) -> str:
    """Render grouped bar chart comparing Cond A, Cond B, and Cond C across Layer 3 metrics."""
    left, right, top, bottom = 78.0, 36.0, 68.0, 74.0
    plot_w = width - left - right
    plot_h = height - top - bottom

    metrics_spec = (
        ("hidden_pass_at_1", "Hidden Pass@1"),
        ("pbt_pass_rate", "PBT Pass Rate"),
        ("mutation_score", "Mutation Score"),
    )
    conds_spec = (
        ("raw_prd", "Cond A (Raw PRD)", "#94a3b8"),
        ("single_pass_nl_spec", "Cond B (Single-Pass NL)", "#f59e0b"),
        ("reqhq_full_bundle", "Cond C (reqhq Bundle)", "#0f766e"),
    )

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">\n',
        f'  <rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>\n',
        f'  <text x="{width / 2:.1f}" y="28" text-anchor="middle" '
        f'font-family="Arial, sans-serif" font-size="18" font-weight="bold" fill="#0f172a">'
        f"{_esc(title)}</text>\n",
        f'  <rect x="{left:.1f}" y="{top:.1f}" width="{plot_w:.1f}" height="{plot_h:.1f}" '
        f'fill="#f8fafc" stroke="#cbd5e1" stroke-width="1"/>\n',
    ]

    # Legend across top
    legend_x = left
    for _, cond_label, color in conds_spec:
        parts.append(
            f'  <rect x="{legend_x:.1f}" y="42" width="12" height="12" fill="{color}"/>\n'
            f'  <text x="{legend_x + 17:.1f}" y="52" font-family="Arial, sans-serif" '
            f'font-size="11" fill="#1e293b">{_esc(cond_label)}</text>\n'
        )
        legend_x += 200.0

    # Horizontal gridlines (0%..100%)
    for step in range(5):
        frac = step / 4.0
        gy = top + plot_h - frac * plot_h
        parts.append(
            f'  <line x1="{left:.1f}" y1="{gy:.1f}" x2="{left + plot_w:.1f}" y2="{gy:.1f}" '
            f'stroke="#e2e8f0" stroke-width="1"/>\n'
        )
        parts.append(
            f'  <text x="{left - 10:.1f}" y="{gy + 4:.1f}" text-anchor="end" '
            f'font-family="Arial, sans-serif" font-size="11" fill="#475569">'
            f"{int(round(frac * 100))}%</text>\n"
        )

    # Grouped bars
    group_w = plot_w / len(metrics_spec)
    bar_w = min(46.0, (group_w * 0.72) / len(conds_spec))
    for g_idx, (metric_key, metric_label) in enumerate(metrics_spec):
        group_center = left + (g_idx + 0.5) * group_w
        group_start = group_center - (bar_w * len(conds_spec)) / 2.0
        for c_idx, (cond_key, _, color) in enumerate(conds_spec):
            raw_val = float(cond_metrics.get(cond_key, {}).get(metric_key, 0.0))
            val = max(0.0, min(1.0, raw_val))
            bar_h = val * plot_h
            bx = group_start + c_idx * bar_w
            by = top + plot_h - bar_h
            parts.append(
                f'  <rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w - 4:.1f}" height="{bar_h:.1f}" '
                f'fill="{color}" stroke="#0f172a" stroke-width="0.6"/>\n'
            )
            parts.append(
                f'  <text x="{bx + (bar_w - 4) / 2:.1f}" y="{by - 5:.1f}" text-anchor="middle" '
                f'font-family="Arial, sans-serif" font-size="10" fill="#1e293b">'
                f"{val * 100:.0f}%</text>\n"
            )
        parts.append(
            f'  <text x="{group_center:.1f}" y="{top + plot_h + 22:.1f}" text-anchor="middle" '
            f'font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#1e293b">'
            f"{_esc(metric_label)}</text>\n"
        )

    # Axes labels
    parts.append(
        f'  <text x="{left + plot_w / 2:.1f}" y="{height - 16:.1f}" text-anchor="middle" '
        f'font-family="Arial, sans-serif" font-size="13" font-weight="bold" fill="#1e293b">'
        f"Layer 3 Evaluation Metrics</text>\n"
    )
    parts.append(
        f'  <text x="22" y="{top + plot_h / 2:.1f}" text-anchor="middle" '
        f'transform="rotate(-90 22 {top + plot_h / 2:.1f})" '
        f'font-family="Arial, sans-serif" font-size="13" font-weight="bold" fill="#1e293b">'
        f"Score (%)</text>\n"
    )

    if dry_run_watermark:
        parts.append(_watermark_element(width, height))

    parts.append("</svg>\n")
    return "".join(parts)


def render_taxonomy_bar_svg(
    taxonomy: ErrorTaxonomyReport,
    *,
    title: str = "7-Category Error Taxonomy",
    dry_run_watermark: bool = False,
    width: int = 760,
    height: int = 440,
) -> str:
    """Render horizontal bar chart of the 7 standardized failure categories."""
    left, right, top, bottom = 195.0, 95.0, 62.0, 56.0
    plot_w = width - left - right
    plot_h = height - top - bottom

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">\n',
        f'  <rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>\n',
        f'  <text x="{width / 2:.1f}" y="28" text-anchor="middle" '
        f'font-family="Arial, sans-serif" font-size="18" font-weight="bold" fill="#0f172a">'
        f"{_esc(title)}</text>\n",
        f'  <text x="{width / 2:.1f}" y="48" text-anchor="middle" '
        f'font-family="Arial, sans-serif" font-size="12" fill="#475569">'
        f"Total failures: {taxonomy.total_failures}</text>\n",
        f'  <rect x="{left:.1f}" y="{top:.1f}" width="{plot_w:.1f}" height="{plot_h:.1f}" '
        f'fill="#f8fafc" stroke="#cbd5e1" stroke-width="1"/>\n',
    ]

    max_count = max((item.count for item in taxonomy.categories), default=0)
    scale_max = max(max_count, 1)

    # Vertical gridlines
    for step in range(5):
        frac = step / 4.0
        gx = left + frac * plot_w
        parts.append(
            f'  <line x1="{gx:.1f}" y1="{top:.1f}" x2="{gx:.1f}" y2="{top + plot_h:.1f}" '
            f'stroke="#e2e8f0" stroke-width="1"/>\n'
        )
        parts.append(
            f'  <text x="{gx:.1f}" y="{top + plot_h + 18:.1f}" text-anchor="middle" '
            f'font-family="Arial, sans-serif" font-size="11" fill="#475569">'
            f"{frac * scale_max:.1f}</text>\n"
        )

    n_cats = max(len(taxonomy.categories), 1)
    row_h = plot_h / n_cats
    bar_h = min(26.0, row_h * 0.62)

    for idx, item in enumerate(taxonomy.categories):
        cy = top + (idx + 0.5) * row_h
        by = cy - bar_h / 2.0
        bw = (item.count / scale_max) * plot_w
        parts.append(
            f'  <text x="{left - 10:.1f}" y="{cy + 4:.1f}" text-anchor="end" '
            f'font-family="Arial, sans-serif" font-size="11" fill="#1e293b">'
            f"{_esc(item.category.value)}</text>\n"
        )
        parts.append(
            f'  <rect x="{left:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{bar_h:.1f}" '
            f'fill="#2563eb" stroke="#1e3a8a" stroke-width="0.6"/>\n'
        )
        parts.append(
            f'  <text x="{left + bw + 6:.1f}" y="{cy + 4:.1f}" '
            f'font-family="Arial, sans-serif" font-size="11" fill="#1e293b">'
            f"{item.count} ({item.share_pct:.1f}%)</text>\n"
        )

    parts.append(
        f'  <text x="{left + plot_w / 2:.1f}" y="{height - 14:.1f}" text-anchor="middle" '
        f'font-family="Arial, sans-serif" font-size="13" font-weight="bold" fill="#1e293b">'
        f"Failure Event Count</text>\n"
    )

    if dry_run_watermark:
        parts.append(_watermark_element(width, height))

    parts.append("</svg>\n")
    return "".join(parts)


def generate_all_charts(
    out_dir: Path | str,
    *,
    cost_points: Sequence[ParetoPoint],
    latency_points: Sequence[ParetoPoint],
    cond_metrics: dict[str, dict[str, float]],
    taxonomy: ErrorTaxonomyReport,
    dry_run_watermark: bool,
) -> dict[str, Path]:
    """Generate, XML-validate, and write all 4 Phase 4 SVG charts with LF endings."""
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    svgs: dict[str, str] = {
        "pareto_quality_vs_cloud_cost": render_pareto_svg(
            cost_points,
            title="Pareto Frontier: Quality vs. Cloud Cost",
            x_label="Mean Cloud Requests",
            y_label="Composite Quality Score",
            dry_run_watermark=dry_run_watermark,
        ),
        "pareto_quality_vs_latency": render_pareto_svg(
            latency_points,
            title="Pareto Frontier: Quality vs. Wall-Clock Latency",
            x_label="Mean Wall-Clock Latency (s)",
            y_label="Composite Quality Score",
            dry_run_watermark=dry_run_watermark,
        ),
        "cond_abc_comparison": render_cond_abc_svg(
            cond_metrics,
            title="Condition A / B / C Coding Quality Comparison",
            dry_run_watermark=dry_run_watermark,
        ),
        "error_taxonomy_bar": render_taxonomy_bar_svg(
            taxonomy,
            title="7-Category Evaluation Error Taxonomy",
            dry_run_watermark=dry_run_watermark,
        ),
    }

    written: dict[str, Path] = {}
    for key, svg_content in svgs.items():
        ET.fromstring(svg_content)
        target = out_path / _CHART_FILENAMES[key]
        with target.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(svg_content)
        written[key] = target
    return written


__all__ = [
    "compute_pareto_frontier",
    "generate_all_charts",
    "render_cond_abc_svg",
    "render_pareto_svg",
    "render_taxonomy_bar_svg",
]
