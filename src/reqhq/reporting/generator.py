"""Final report Markdown and Overleaf-ready LaTeX table generator with Mock Quarantine."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Literal

from reqhq.reporting.models import ReportGenerationResult, VerdictStatus

TEAM_NAME = "req-hq"
TEAM_INSTITUTION = "IIIT Hyderabad"
TEAM_AUTHORS: tuple[str, ...] = (
    "Panem Chaitanya Pavan Kumar (2025204023)",
    "Anjali Yadav (2025204015)",
    "Anshul Agrawal (2025901015)",
)

DRY_RUN_BANNER_MD = (
    "> [!WARNING] DRY-RUN / UNSCORED MOCK DATA — NOT VALID FOR HYPOTHESIS CLAIMS"
)
DRY_RUN_BANNER_TEX = (
    r"\noindent\fbox{\parbox{0.96\linewidth}{"
    r"\textbf{WARNING: DRY-RUN / UNSCORED MOCK DATA --- NOT VALID FOR HYPOTHESIS CLAIMS.} "
    r"All H1--H4 evaluation claims are disabled and quarantined.}}"
)
QUARANTINED_STATUS = "QUARANTINED_MOCK_OR_INCOMPLETE"

EXPECTED_SVGS: tuple[tuple[str, str], ...] = (
    ("pareto_quality_vs_cloud_cost.svg", "Pareto Frontier: Quality vs. Cloud Cost"),
    ("pareto_quality_vs_latency.svg", "Pareto Frontier: Quality vs. Latency"),
    ("cond_abc_comparison.svg", "Condition A / B / C Coding Benchmark Comparison"),
    ("error_taxonomy_bar.svg", "7-Category Error Taxonomy Breakdown"),
)

TAXONOMY_CATEGORIES: tuple[str, ...] = (
    "SCHEMA_DRIFT",
    "ALLOY_SYNTAX",
    "ALLOY_VACUOUS",
    "UNSAT_CONTRADICTION",
    "ROUTING_AMBIGUITY",
    "PBT_COUNTEREXAMPLE",
    "QUOTA_FALLBACK",
)

_LATEX_ESCAPES: dict[str, str] = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "$": r"\$",
    "&": r"\&",
    "#": r"\#",
    "_": r"\_",
    "%": r"\%",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}

_QUARANTINE_SCRUB_RE = re.compile(
    r"VERIFIED_LIVE|SUPPORTED|REFUTED",
    flags=re.IGNORECASE,
)


def escape_latex(text: str) -> str:
    """Escape LaTeX special characters in a single pass so no unescaped metacharacters survive."""
    return "".join(_LATEX_ESCAPES.get(ch, ch) for ch in text)


def _normalize_cell_text(value: Any) -> str:
    raw = str(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return " ".join(raw.split())


def _md_cell(value: Any) -> str:
    return _normalize_cell_text(value).replace("|", r"\|")


def _tex_cell(value: Any) -> str:
    return escape_latex(_normalize_cell_text(value))


def _scrub_quarantined_text(text: str) -> str:
    """Remove forbidden live-verdict tokens from text emitted into quarantined outputs."""
    return _QUARANTINE_SCRUB_RE.sub(QUARANTINED_STATUS, text)


def _resolve_quarantine(summary: dict[str, Any]) -> tuple[bool, VerdictStatus, list[str]]:
    run_mode = str(summary.get("run_mode", "mock")).strip().lower()
    raw_quarantined = bool(summary.get("quarantined", True))
    raw_verdict = str(summary.get("verdict_status", "DRY_RUN_UNSCORED")).strip()
    reasons = [str(r) for r in (summary.get("quarantine_reasons") or []) if str(r).strip()]

    is_quarantined = (
        run_mode != "live"
        or raw_quarantined
        or raw_verdict != "VERIFIED_LIVE"
    )
    if is_quarantined:
        if run_mode != "live" and not any("run_mode" in r for r in reasons):
            reasons.append(f"run_mode={run_mode!r} (requires 'live')")
        if not reasons:
            reasons.append("Run failed live verification gates; quarantined fail-closed.")
        sanitized_reasons = [_scrub_quarantined_text(r) for r in reasons]
        return True, "DRY_RUN_UNSCORED", sanitized_reasons
    return False, "VERIFIED_LIVE", []


def _copy_svg_figures(
    source_dir: Path,
    out_dir: Path,
    summary: dict[str, Any],
) -> dict[str, str]:
    """Copy available SVG charts from source_dir into out_dir and return filename mapping."""
    source_resolved = source_dir.resolve()
    out_resolved = out_dir.resolve()
    discovered: dict[str, Path] = {}

    raw_svg_map = summary.get("svg_files")
    if isinstance(raw_svg_map, dict):
        for _key, raw_val in raw_svg_map.items():
            candidate = Path(str(raw_val))
            if not candidate.is_absolute():
                candidate = source_dir / candidate
            resolved = candidate.resolve()
            try:
                resolved.relative_to(source_resolved)
            except ValueError:
                continue
            if resolved.is_file() and resolved.suffix.lower() == ".svg":
                discovered[resolved.name] = resolved

    for expected_name, _caption in EXPECTED_SVGS:
        candidate = (source_dir / expected_name).resolve()
        if candidate.is_file() and expected_name not in discovered:
            discovered[expected_name] = candidate

    copied: dict[str, str] = {}
    for filename in sorted(discovered):
        src = discovered[filename]
        dst = (out_resolved / filename).resolve()
        if src != dst:
            shutil.copyfile(src, dst)
        copied[filename] = filename
    return copied


def _format_float(val: Any) -> str:
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return f"{float(val):.4f}"
    return _normalize_cell_text(val)


def _format_metrics_inline(metrics: Any) -> str:
    if not isinstance(metrics, dict) or not metrics:
        return "none"
    parts = [f"{k}={_format_float(v)}" for k, v in sorted(metrics.items())]
    return "; ".join(parts)


def _format_p_values_inline(p_values: Any) -> str:
    if not isinstance(p_values, dict) or not p_values:
        return "n/a"
    parts = [f"{k}: p={_format_float(v)}" for k, v in sorted(p_values.items())]
    return "; ".join(parts)


def _format_cis_inline(bootstrap_cis: Any) -> str:
    if not isinstance(bootstrap_cis, dict) or not bootstrap_cis:
        return "n/a"
    parts: list[str] = []
    for key in sorted(bootstrap_cis):
        ci = bootstrap_cis[key]
        if isinstance(ci, dict):
            mean_diff = _format_float(ci.get("mean_diff", 0.0))
            ci_low = _format_float(ci.get("ci_low", 0.0))
            ci_high = _format_float(ci.get("ci_high", 0.0))
            parts.append(f"{key}: {mean_diff} [{ci_low}, {ci_high}]")
        else:
            parts.append(f"{key}: {_normalize_cell_text(ci)}")
    return "; ".join(parts)


def _normalize_hypotheses(
    summary: dict[str, Any],
    *,
    quarantined: bool,
) -> list[dict[str, str]]:
    raw_hyps = summary.get("hypotheses")
    by_id: dict[str, dict[str, Any]] = {}
    if isinstance(raw_hyps, list):
        for item in raw_hyps:
            if isinstance(item, dict) and item.get("hypothesis_id"):
                by_id[str(item["hypothesis_id"])] = item

    default_titles = {
        "H1": "Iterative Elicitation Recall vs. Question Efficiency",
        "H2": "Local-First TIER_0 Parity with >=80% Cloud Reduction",
        "H3": "Alloy 6 Formal Detection & Socio-Technical Routing ADRs",
        "H4": "Full ReqHQ Bundle Downstream Coding Quality (Cond C vs. A/B)",
    }

    rows: list[dict[str, str]] = []
    for hid in ("H1", "H2", "H3", "H4"):
        entry = by_id.get(hid, {})
        title = _normalize_cell_text(entry.get("title") or default_titles[hid])
        metrics_str = _format_metrics_inline(entry.get("metrics"))
        p_str = _format_p_values_inline(entry.get("p_values"))
        ci_str = _format_cis_inline(entry.get("bootstrap_cis"))

        if quarantined:
            status = QUARANTINED_STATUS
            rationale = (
                "Disabled under Mock Quarantine: run is non-live or incomplete; "
                "no empirical claim is emitted."
            )
            title = _scrub_quarantined_text(title)
            metrics_str = _scrub_quarantined_text(metrics_str)
            p_str = _scrub_quarantined_text(p_str)
            ci_str = _scrub_quarantined_text(ci_str)
        else:
            status = _normalize_cell_text(entry.get("status") or "INCONCLUSIVE")
            rationale = _normalize_cell_text(entry.get("rationale") or "Evaluated on live run.")

        rows.append(
            {
                "hypothesis_id": hid,
                "title": title,
                "metrics": metrics_str,
                "p_values": p_str,
                "bootstrap_cis": ci_str,
                "status": status,
                "rationale": rationale,
            }
        )
    return rows


def _normalize_taxonomy(
    summary: dict[str, Any],
    *,
    quarantined: bool,
) -> tuple[int, list[dict[str, Any]]]:
    raw_tax = summary.get("taxonomy")
    by_cat: dict[str, dict[str, Any]] = {}
    total_failures = 0
    if isinstance(raw_tax, dict):
        total_failures = int(raw_tax.get("total_failures", 0))
        for item in raw_tax.get("categories") or []:
            if isinstance(item, dict) and item.get("category"):
                by_cat[str(item["category"])] = item

    rows: list[dict[str, Any]] = []
    for cat in TAXONOMY_CATEGORIES:
        item = by_cat.get(cat, {})
        count = int(item.get("count", 0))
        share = float(item.get("share_pct", 0.0))
        samples = [str(s) for s in (item.get("sample_ids") or [])[:5]]
        samples_text = ", ".join(samples) if samples else "none"
        if quarantined:
            samples_text = _scrub_quarantined_text(samples_text)
        rows.append(
            {
                "category": cat,
                "count": count,
                "share_pct": share,
                "samples": samples_text,
            }
        )
    return total_failures, rows


def _normalize_pareto_points(
    raw_points: Any,
    *,
    quarantined: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(raw_points, list):
        for item in raw_points:
            if not isinstance(item, dict):
                continue
            label = _normalize_cell_text(item.get("label", "unnamed"))
            if quarantined:
                label = _scrub_quarantined_text(label)
            rows.append(
                {
                    "label": label,
                    "quality": float(item.get("quality", 0.0)),
                    "cost": float(item.get("cost", 0.0)),
                    "is_frontier": bool(item.get("is_frontier", False)),
                }
            )
    return rows


def _render_markdown_report(
    summary: dict[str, Any],
    *,
    quarantined: bool,
    verdict_status: VerdictStatus,
    quarantine_reasons: list[str],
    copied_svgs: dict[str, str],
) -> str:
    run_id = _normalize_cell_text(summary.get("run_id", "unknown-run"))
    run_mode = _normalize_cell_text(summary.get("run_mode", "mock"))
    seed = int(summary.get("seed", 42))
    replicates = int(summary.get("replicates", 0))
    hw = summary.get("hardware_profile") if isinstance(summary.get("hardware_profile"), dict) else {}
    hw_summary = (
        f"TIER_{hw.get('tier', 'unknown')} | {hw.get('gpu_name', 'unknown')} | "
        f"{hw.get('vram_gb', 'n/a')} GB VRAM | {hw.get('ram_gb', 'n/a')} GB RAM"
        if hw
        else "Not recorded in analysis_summary.json"
    )
    if quarantined:
        run_id = _scrub_quarantined_text(run_id)
        run_mode = _scrub_quarantined_text(run_mode)
        hw_summary = _scrub_quarantined_text(hw_summary)

    hyp_rows = _normalize_hypotheses(summary, quarantined=quarantined)
    total_failures, tax_rows = _normalize_taxonomy(summary, quarantined=quarantined)
    cost_points = _normalize_pareto_points(summary.get("pareto_cost_points"), quarantined=quarantined)
    latency_points = _normalize_pareto_points(summary.get("pareto_latency_points"), quarantined=quarantined)

    lines: list[str] = [
        "# ReqHQ v2: Local-First Agentic Specification Refinement & Formal Verification",
        "",
        f"**Team:** `{TEAM_NAME}` — {TEAM_INSTITUTION}  ",
        f"**Authors:** {', '.join(TEAM_AUTHORS)}  ",
        f"**Run ID:** `{run_id}` | **Run Mode:** `{run_mode}` | **Verdict Status:** `{verdict_status}`  ",
        f"**Seed:** `{seed}` | **Replicates:** `{replicates}` | **Hardware:** {hw_summary}",
        "",
    ]

    if quarantined:
        lines.extend(
            [
                DRY_RUN_BANNER_MD,
                "",
                "**Quarantine Reasons:**",
            ]
        )
        for reason in quarantine_reasons:
            lines.append(f"- {reason}")
        lines.append("")

    # 1. Abstract
    lines.extend(
        [
            "## 1. Abstract",
            "",
            (
                "Ambiguous, under-specified, and mutually contradictory software requirements "
                "remain a leading cause of defects in both human-led and AI-assisted software "
                "engineering. ReqHQ v2 (`src/reqhq/`) evolves the legacy ReqHQ prototype "
                "(`src/specpipe/`) into a local-first, hardware-aware agentic requirements "
                "engineering workbench. ReqHQ v2 combines hierarchical lossless document "
                "ingestion, five sequential local SLM critics with delta-critic convergence, "
                "socio-technical triage producing Nygard Architecture Decision Records (ADRs), "
                "two-layer literate Alloy 6 formal modeling with non-vacuity and UNSAT core "
                "repair, and fail-closed SHA-256-locked Property-Based Testing (PBT) guardrails. "
                "This report is auto-generated strictly from SHA-256-tracked evaluation manifests "
                "under a fail-closed Mock Quarantine discipline."
            ),
            "",
        ]
    )

    # 2. Introduction
    lines.extend(
        [
            "## 2. Introduction",
            "",
            (
                "Modern AI coding agents generate syntactically valid code rapidly, yet they "
                "silently hallucinate missing business rules, violate cross-cutting non-functional "
                "constraints, or build software workarounds for organizational process problems. "
                "ReqHQ v2 addresses this upstream bottleneck by treating specification refinement "
                "as an auditable, formally verified engineering pipeline that runs predominantly "
                "on consumer laptops (`TIER_0` <= 4 GB VRAM via Ollama `qwen3:4b` / `phi4-mini`) "
                "while keeping cloud escalation tightly budgeted and PII-scrubbed."
            ),
            "",
        ]
    )

    # 3. Evolution from ReqHQ
    lines.extend(
        [
            "## 3. Evolution from ReqHQ (`src/specpipe/` -> `src/reqhq/`)",
            "",
            (
                "To preserve complete backward compatibility with the 87 legacy `specpipe` tests "
                "and frozen datasets (`data/frozen/pure/`, `data/frozen/promise_nfr/`), ReqHQ v2 "
                "was architected under an isolated package namespace (`src/reqhq/`, ADR-0002) "
                "across four incremental phases:"
            ),
            "",
            "- **Phase 1 (Core Infrastructure):** Hardware-aware local SLM router (`TIER_0`–`TIER_3`), "
            "SQLite `QuotaLedger` & `PromptCache`, PII scrubber, Day-1 document normalization & "
            "hierarchical chunking, append-only `SessionEventLog`, and `SystemContextBase`.",
            "- **Phase 2 (Elicitation, Triage & Alloy 6):** Iterative 5-critic delta convergence "
            "engine, 4-way socio-technical triage (`software`/`process`/`training`/`hybrid`) with "
            "Nygard Routing ADRs, two-layer literate Alloy 6 emitter, headless Alloy 6.2 runner, "
            "vacuity gate, and bounded UNSAT repair loop.",
            "- **Phase 3 (PBT Guardrails & Benchmarks):** `PBT_Spec.yaml` contract emitter, "
            "dual-layer Pydantic + Hypothesis guardrail codegen, fail-closed `pbt_lock.sha256` "
            "tamper lock, AST mutant harness, gap-injection oracle simulator, and 16-case "
            "cross-domain socio-technical benchmark.",
            "- **Phase 4 (Evaluation, Statistics & Reproducible Release):** Checkpointed Layers 1–4 "
            "evaluation runner, pure-Python stdlib statistical inference & SVG Pareto charts, "
            "7-category error taxonomy, Mock Quarantine enforcement, and SHA-256 release packaging.",
            "",
        ]
    )

    # 4. Architecture
    lines.extend(
        [
            "## 4. Architecture: Local-First `TIER_0`, Delta Critics & Socio-Technical Triage",
            "",
            (
                "ReqHQ v2 routes every routine extraction, chunk summarization, and critic pass "
                "through a local Ollama SLM matched to detected hardware (`TIER_0`: `qwen3:4b` "
                "with `phi4-mini` fallback; `TIER_1`: `qwen3:8b`; `TIER_2`: `qwen3:14b`; "
                "`TIER_3`: `gpt-oss:20b`). Outbound cloud escalations pass through deterministic "
                "PII scrubbing, prompt-hash caching, and a strict SQLite quota ceiling (90% of "
                "free-tier RPM/RPD/TPM limits). During elicitation, five specialized critics "
                "operate over a dependency-tracked requirement graph so user revisions trigger "
                "delta re-evaluation rather than full re-prompting. Socio-technical triage routes "
                "every requirement into `software`, `process`, `training`, or `hybrid` tracks "
                "and synthesizes traceable Nygard ADRs before any code is written."
            ),
            "",
        ]
    )

    # 5. Alloy 6 + PBT Guardrails
    lines.extend(
        [
            "## 5. Alloy 6 Formal Verification & PBT Guardrails",
            "",
            (
                "Software-routed requirements are compiled into a two-layer literate Alloy 6 model "
                "(`--alloy-compat alloy6|legacy`) linking `REQ-ID` and `ADR-ID` anchors to formal "
                "signatures, invariants, and assertions. A headless Alloy 6.2 verifier enforces "
                "syntax validity, non-vacuity (`run` predicate satisfiability before `check` passes), "
                "and bounded UNSAT/counterexample repair. Verified invariants are then lowered "
                "into `PBT_Spec.yaml`, executable runtime Pydantic guardrails, Hypothesis "
                "property tests, and a fail-closed `pbt_lock.sha256` manifest that prevents "
                "downstream coding agents from weakening or tampering with acceptance checks."
            ),
            "",
        ]
    )

    # 6. Experimental Setup
    lines.extend(
        [
            "## 6. Experimental Setup (Layers 1–4)",
            "",
            "The Phase 4 evaluation protocol (`src/reqhq/evaluation/`) executes four orthogonal layers "
            "with resumable atomic checkpointing and deterministic seeding:",
            "",
            "- **Layer 1 (Elicitation & Gap-Injection Oracle):** Evaluates iterative delta "
            "elicitation against single-pass and exhaustive-questioning baselines using human-verified "
            "frozen PURE documents (`data/frozen/pure/`), `GapInjector`, and `FrozenOracleSimulator`.",
            "- **Layer 2 (Socio-Technical Triage & Alloy Contradiction Detection):** Benchmarks "
            "the 16-case cross-domain suite (4 domains x 4 cases), measuring route accuracy, "
            "Alloy vs. LLM-only contradiction detection, blind ADR rubric scores, and Cohen's kappa.",
            "- **Layer 3 (Downstream Coding Harness — Cond A / B / C):** Evaluates 6 self-contained "
            "greenfield coding tasks (`refund_sla_engine`, `triage_escalation_queue`, "
            "`cold_chain_inventory`, `access_approval_workflow`, `loan_disbursement_gate`, "
            "`incident_shift_handover`) across Condition A (`raw_prd`), Condition B "
            "(`single_pass_nl_spec`), and Condition C (`reqhq_full_bundle`) with hidden acceptance "
            "Pass@1, Hypothesis PBT pass rate, AST mutation score, Alloy non-vacuity, and lock checks.",
            "- **Layer 4 (Hardware Telemetry & Cloud Cost):** Aggregates Windows 11 stdlib memory "
            "telemetry (`GetProcessMemoryInfo` / `tracemalloc`), `HardwareProfiler`, and `QuotaLedger` "
            "cloud request/token reductions.",
            "",
        ]
    )

    # 7. Results Tables for H1-H4
    lines.extend(
        [
            "## 7. Results: Hypotheses H1–H4",
            "",
            "| Hypothesis | Description | Metrics | p-values | 95% Bootstrap CIs | Verdict | Rationale |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
    )
    for row in hyp_rows:
        lines.append(
            f"| {_md_cell(row['hypothesis_id'])} | {_md_cell(row['title'])} | "
            f"{_md_cell(row['metrics'])} | {_md_cell(row['p_values'])} | "
            f"{_md_cell(row['bootstrap_cis'])} | `{_md_cell(row['status'])}` | "
            f"{_md_cell(row['rationale'])} |"
        )
    lines.append("")

    if cost_points or latency_points:
        lines.extend(
            [
                "### Pareto Frontier Summary",
                "",
                "| Chart | Configuration | Quality | Cost / Latency | On Frontier |",
                "| :--- | :--- | ---: | ---: | :---: |",
            ]
        )
        for pt in cost_points:
            lines.append(
                f"| Quality vs. Cloud Cost | {_md_cell(pt['label'])} | "
                f"{pt['quality']:.4f} | {pt['cost']:.4f} | "
                f"{'Yes' if pt['is_frontier'] else 'No'} |"
            )
        for pt in latency_points:
            lines.append(
                f"| Quality vs. Latency | {_md_cell(pt['label'])} | "
                f"{pt['quality']:.4f} | {pt['cost']:.4f} | "
                f"{'Yes' if pt['is_frontier'] else 'No'} |"
            )
        lines.append("")

    # 8. Embedded SVG Pareto & Taxonomy Figures
    lines.extend(
        [
            "## 8. Embedded SVG Pareto & Taxonomy Figures",
            "",
        ]
    )
    for svg_name, caption in EXPECTED_SVGS:
        if svg_name in copied_svgs:
            lines.append(f"### {caption}")
            lines.append("")
            lines.append(f"![{caption}]({copied_svgs[svg_name]})")
            lines.append("")
        else:
            lines.append(f"- *{caption}*: `{svg_name}` not present in analysis directory.")
    if not any(name in copied_svgs for name, _ in EXPECTED_SVGS):
        lines.append("")

    # 9. Error Analysis (7-Category Taxonomy)
    lines.extend(
        [
            "## 9. Error Analysis (7-Category Taxonomy)",
            "",
            f"Total categorized failure events across Layers 1–4: **{total_failures}**.",
            "",
            "| Error Category | Count | Share (%) | Sample Record IDs |",
            "| :--- | ---: | ---: | :--- |",
        ]
    )
    for row in tax_rows:
        lines.append(
            f"| `{row['category']}` | {row['count']} | {row['share_pct']:.2f}% | "
            f"{_md_cell(row['samples'])} |"
        )
    lines.append("")

    # 10. Threats to Validity
    lines.extend(
        [
            "## 10. Threats to Validity",
            "",
            "- **Internal Validity:** Simulated oracle answers in Layer 1 provide an extractive "
            "upper bound on stakeholder cooperation; deterministic seeding (`seed="
            f"{seed}`) and SHA-256 log verification mitigate silent drift across resumed runs.",
            "- **External Validity:** The 6 greenfield coding tasks and 16 cross-domain cases "
            "span Fintech, Healthcare, Logistics, Enterprise IT, and Operations, yet broader "
            "industrial brownfield repositories remain future empirical work.",
            "- **Construct Validity:** AST mutation score, Hypothesis PBT pass rates, and Alloy "
            "non-vacuity measure formal behavioral fidelity rather than human maintainability alone.",
            "- **Conclusion Validity & Mock Quarantine:** Pure-Python paired bootstrap CIs, exact "
            "McNemar tests, and Wilcoxon signed-rank tests avoid parametric assumptions. Any "
            "non-live, under-replicated (`replicates < 2`), unverified-gold, or tampered run is "
            "quarantined fail-closed as `DRY_RUN_UNSCORED` so plumbing checks can never masquerade "
            "as empirical evidence.",
            "",
        ]
    )

    # 11. Brownfield Seam
    lines.extend(
        [
            "## 11. Brownfield Extension Seam (`SystemContextBase`)",
            "",
            (
                "Although Phase 1–4 focus on greenfield specification refinement, ReqHQ v2 embeds "
                "an explicit brownfield extension seam in `src/reqhq/schemas/system_context.py` "
                "(ADR-0006). Downstream brownfield integrations subclass `SystemContextBase` "
                "(satisfying the runtime-checkable `SystemContext` Protocol) and attach typed "
                "`BrownfieldArtifactRef` descriptors for `architecture_md`, `ast_summary`, "
                "`alloy_model`, and `superseded_adr` artifacts with optional 64-hex SHA-256 "
                "digests. Because `to_prompt_context()` and `summary_metadata()` are abstract "
                "contract methods, legacy repository loaders plug directly into elicitation and "
                "formal synthesis without modifying Phase 1–4 core modules."
            ),
            "",
        ]
    )

    # 12. Conclusion
    conclusion_tail = (
        "This artifact reflects a quarantined dry-run / incomplete execution and is provided "
        "solely to validate pipeline plumbing and reproducibility packaging; execute a live "
        "Windows 11 `TIER_0` + Alloy 6.2 run per `REPRODUCIBILITY.md` to generate publishable "
        "empirical claims."
        if quarantined
        else (
            "All four evaluation layers completed under live verification gates with SHA-256 "
            "manifest integrity (`VERIFIED_LIVE`), establishing a reproducible local-first "
            "baseline for formally grounded agentic requirements engineering."
        )
    )
    lines.extend(
        [
            "## 12. Conclusion",
            "",
            (
                "ReqHQ v2 demonstrates an end-to-end, auditable architecture for transforming "
                "ambiguous prompts and requirements documents into socio-technically triaged ADRs, "
                "non-vacuous Alloy 6 models, and tamper-locked PBT guardrails under consumer "
                "hardware constraints. " + conclusion_tail
            ),
            "",
        ]
    )

    return "\n".join(lines)


def _render_latex_tables(
    summary: dict[str, Any],
    *,
    quarantined: bool,
    verdict_status: VerdictStatus,
) -> str:
    run_id = _normalize_cell_text(summary.get("run_id", "unknown-run"))
    run_mode = _normalize_cell_text(summary.get("run_mode", "mock"))
    if quarantined:
        run_id = _scrub_quarantined_text(run_id)
        run_mode = _scrub_quarantined_text(run_mode)

    hyp_rows = _normalize_hypotheses(summary, quarantined=quarantined)
    total_failures, tax_rows = _normalize_taxonomy(summary, quarantined=quarantined)
    cost_points = _normalize_pareto_points(summary.get("pareto_cost_points"), quarantined=quarantined)
    latency_points = _normalize_pareto_points(summary.get("pareto_latency_points"), quarantined=quarantined)

    lines: list[str] = [
        "% Auto-generated by ReqHQ v2 (src/reqhq/reporting/generator.py)",
        "% Requires \\usepackage{booktabs} in your Overleaf LaTeX preamble.",
        f"% Team: {TEAM_NAME} ({TEAM_INSTITUTION}) | Run ID: {run_id} | Mode: {run_mode} | Status: {verdict_status}",
        "",
    ]

    if quarantined:
        lines.extend(
            [
                DRY_RUN_BANNER_TEX,
                "",
            ]
        )

    # Table 1: H1-H4 Hypothesis Summary
    lines.extend(
        [
            r"\begin{table}[htbp]",
            r"\centering",
            r"\caption{ReqHQ v2 Empirical Hypothesis Evaluation Summary (H1--H4).}",
            r"\label{tab:reqhq_hypotheses}",
            r"\begin{tabular}{lp{3.6cm}p{3.4cm}p{2.4cm}l}",
            r"\toprule",
            r"ID & Hypothesis & Metrics & Statistics (p / 95\% CI) & Verdict \\",
            r"\midrule",
        ]
    )
    for row in hyp_rows:
        stats_combined = f"{row['p_values']}; {row['bootstrap_cis']}"
        lines.append(
            f"{_tex_cell(row['hypothesis_id'])} & "
            f"{_tex_cell(row['title'])} & "
            f"{_tex_cell(row['metrics'])} & "
            f"{_tex_cell(stats_combined)} & "
            f"{_tex_cell(row['status'])} \\\\"
        )
    lines.extend(
        [
            r"\bottomrule",
            r"\end{tabular}",
            r"\end{table}",
            "",
        ]
    )

    # Table 2: Pareto Frontier Summary
    lines.extend(
        [
            r"\begin{table}[htbp]",
            r"\centering",
            r"\caption{ReqHQ v2 Pareto Frontier Configurations (Quality vs. Cloud Cost and Latency).}",
            r"\label{tab:reqhq_pareto}",
            r"\begin{tabular}{llrrr}",
            r"\toprule",
            r"Dimension & Configuration & Quality & Cost/Latency & Frontier \\",
            r"\midrule",
        ]
    )
    if cost_points or latency_points:
        for pt in cost_points:
            lines.append(
                f"{_tex_cell('Cloud Cost')} & {_tex_cell(pt['label'])} & "
                f"{pt['quality']:.4f} & {pt['cost']:.4f} & "
                f"{'Yes' if pt['is_frontier'] else 'No'} \\\\"
            )
        for pt in latency_points:
            lines.append(
                f"{_tex_cell('Latency')} & {_tex_cell(pt['label'])} & "
                f"{pt['quality']:.4f} & {pt['cost']:.4f} & "
                f"{'Yes' if pt['is_frontier'] else 'No'} \\\\"
            )
    else:
        lines.append(r"n/a & None recorded & 0.0000 & 0.0000 & No \\")
    lines.extend(
        [
            r"\bottomrule",
            r"\end{tabular}",
            r"\end{table}",
            "",
        ]
    )

    # Table 3: 7-Category Error Taxonomy
    lines.extend(
        [
            r"\begin{table}[htbp]",
            r"\centering",
            f"\\caption{{ReqHQ v2 7-Category Error Taxonomy (Total Failures: {total_failures}).}}",
            r"\label{tab:reqhq_error_taxonomy}",
            r"\begin{tabular}{lrrp{4.5cm}}",
            r"\toprule",
            r"Error Category & Count & Share (\%) & Sample Record IDs \\",
            r"\midrule",
        ]
    )
    for row in tax_rows:
        lines.append(
            f"{_tex_cell(row['category'])} & {row['count']} & "
            f"{row['share_pct']:.2f}\\% & {_tex_cell(row['samples'])} \\\\"
        )
    lines.extend(
        [
            r"\bottomrule",
            r"\end{tabular}",
            r"\end{table}",
            "",
        ]
    )

    return "\n".join(lines)


def generate_final_report(
    analysis_dir: Path | str,
    out_dir: Path | str,
    *,
    fmt: Literal["md", "tex", "both"] = "both",
) -> ReportGenerationResult:
    """Generate FINAL_REPORT.md and/or tables_latex.tex from analysis_summary.json."""
    if fmt not in ("md", "tex", "both"):
        raise ValueError(f"Unsupported report format {fmt!r}; expected 'md', 'tex', or 'both'")

    in_path = Path(analysis_dir)
    if in_path.is_file():
        summary_path = in_path
        source_dir = in_path.parent
    else:
        source_dir = in_path
        summary_path = in_path / "analysis_summary.json"

    if not summary_path.is_file():
        raise FileNotFoundError(f"analysis_summary.json not found at: {summary_path}")

    raw_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(raw_summary, dict):
        raise ValueError("analysis_summary.json must contain a JSON object at top level")

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    quarantined, verdict_status, quarantine_reasons = _resolve_quarantine(raw_summary)
    copied_svgs = _copy_svg_figures(source_dir, out_path, raw_summary)

    report_md_path: Path | None = None
    tables_tex_path: Path | None = None

    if fmt in ("md", "both"):
        md_text = _render_markdown_report(
            raw_summary,
            quarantined=quarantined,
            verdict_status=verdict_status,
            quarantine_reasons=quarantine_reasons,
            copied_svgs=copied_svgs,
        )
        report_md_path = out_path / "FINAL_REPORT.md"
        with report_md_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(md_text)

    if fmt in ("tex", "both"):
        tex_text = _render_latex_tables(
            raw_summary,
            quarantined=quarantined,
            verdict_status=verdict_status,
        )
        tables_tex_path = out_path / "tables_latex.tex"
        with tables_tex_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(tex_text)

    return ReportGenerationResult(
        out_dir=out_path,
        report_md=report_md_path,
        tables_tex=tables_tex_path,
        verdict_status=verdict_status,
        quarantined=quarantined,
    )


__all__ = [
    "DRY_RUN_BANNER_MD",
    "DRY_RUN_BANNER_TEX",
    "EXPECTED_SVGS",
    "QUARANTINED_STATUS",
    "TAXONOMY_CATEGORIES",
    "TEAM_AUTHORS",
    "TEAM_INSTITUTION",
    "TEAM_NAME",
    "escape_latex",
    "generate_final_report",
]
