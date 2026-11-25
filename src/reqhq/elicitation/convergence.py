"""Finding deduplication, high-info-gain question clustering, and convergence gate."""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import re

from reqhq.elicitation.models import (
    CriticFinding,
    ElicitationQuestion,
    FindingSeverity,
    SEVERITY_RANK,
)

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^a-z0-9\s]")


def _normalize_text(text: str) -> str:
    cleaned = _PUNCT_RE.sub(" ", text.lower())
    return _WS_RE.sub(" ", cleaned).strip()


def deduplicate_findings(findings: Sequence[CriticFinding]) -> list[CriticFinding]:
    """Deduplicate findings deterministically while preserving resolved history."""
    resolved_by_id: dict[str, CriticFinding] = {}
    unresolved_by_key: dict[tuple[str, str, str], CriticFinding] = {}

    for finding in findings:
        if finding.resolved:
            resolved_by_id[finding.finding_id] = finding
            continue

        key = (
            finding.req_id,
            finding.critic.value,
            _normalize_text(finding.title),
        )
        existing = unresolved_by_key.get(key)
        if existing is None:
            unresolved_by_key[key] = finding
        elif SEVERITY_RANK[finding.severity] > SEVERITY_RANK[existing.severity]:
            unresolved_by_key[key] = finding

    # Also collapse identical (req_id, normalized_question) unresolved duplicates across same critic.
    deduped_unresolved: dict[tuple[str, str, str], CriticFinding] = {}
    for finding in unresolved_by_key.values():
        qkey = (
            finding.req_id,
            finding.critic.value,
            _normalize_text(finding.suggested_question),
        )
        existing = deduped_unresolved.get(qkey)
        if existing is None or SEVERITY_RANK[finding.severity] > SEVERITY_RANK[existing.severity]:
            deduped_unresolved[qkey] = finding

    combined = list(deduped_unresolved.values()) + list(resolved_by_id.values())
    combined.sort(
        key=lambda f: (
            1 if f.resolved else 0,
            -SEVERITY_RANK[f.severity],
            f.req_id,
            f.critic.value,
            f.finding_id,
        )
    )
    return combined


def _score_cluster(cluster: Sequence[CriticFinding]) -> tuple[FindingSeverity, float]:
    max_sev = max(cluster, key=lambda f: SEVERITY_RANK[f.severity]).severity
    base_map = {
        FindingSeverity.BLOCKER: 0.60,
        FindingSeverity.MAJOR: 0.40,
        FindingSeverity.MINOR: 0.20,
    }
    req_count = len({f.req_id for f in cluster})
    critic_count = len({f.critic for f in cluster})
    finding_count = len(cluster)

    score = (
        base_map[max_sev]
        + min(0.20, 0.06 * max(0, finding_count - 1) + 0.07 * max(0, req_count - 1))
        + min(0.20, 0.06 * critic_count)
    )
    return max_sev, round(min(1.0, max(0.05, score)), 4)


def _render_cluster_question(cluster: Sequence[CriticFinding], index: int) -> ElicitationQuestion:
    severity, info_gain = _score_cluster(cluster)
    req_ids = sorted({f.req_id for f in cluster})
    finding_ids = [f.finding_id for f in cluster]

    unique_prompts: list[str] = []
    seen_p: set[str] = set()
    for f in sorted(cluster, key=lambda item: (-SEVERITY_RANK[item.severity], item.finding_id)):
        norm = _normalize_text(f.suggested_question)
        if norm not in seen_p:
            seen_p.add(norm)
            unique_prompts.append(f.suggested_question.strip())

    if len(unique_prompts) == 1:
        prompt = unique_prompts[0]
    else:
        joined = " ".join(unique_prompts[:3])
        prompt = f"[{', '.join(req_ids)}] {joined}"

    defaults: list[str] = []
    seen_d: set[str] = set()
    for f in cluster:
        d = f.recommended_default.strip()
        if d and _normalize_text(d) not in seen_d:
            seen_d.add(_normalize_text(d))
            defaults.append(d)
    default_assumption = (
        " ".join(defaults[:3])
        if defaults
        else f"Apply conservative default resolution for {', '.join(req_ids)}."
    )

    q_digest = hashlib.sha256(
        f"{','.join(req_ids)}|{_normalize_text(prompt)}".encode("utf-8")
    ).hexdigest()[:6]
    anchor_req = req_ids[0].replace("-", "") if req_ids else f"{index:03d}"

    return ElicitationQuestion(
        question_id=f"Q-{anchor_req}-{q_digest}",
        prompt=prompt,
        req_ids=req_ids,
        finding_ids=finding_ids,
        severity=severity,
        info_gain_score=info_gain,
        default_assumption=default_assumption,
    )


def cluster_and_rank_questions(
    findings: Sequence[CriticFinding],
    min_questions: int = 3,
    max_questions: int = 5,
) -> list[ElicitationQuestion]:
    """Cluster unresolved findings into ranked high-info-gain questions (typically 3-5)."""
    if min_questions < 1:
        raise ValueError("min_questions must be >= 1")
    if max_questions < min_questions:
        raise ValueError("max_questions must be >= min_questions")

    unresolved = [f for f in deduplicate_findings(findings) if not f.resolved]
    if not unresolved:
        return []

    # Step 1: Group identical normalized questions (e.g. same SLA question across reqs).
    by_question: dict[str, list[CriticFinding]] = {}
    for finding in unresolved:
        key = _normalize_text(finding.suggested_question)
        by_question.setdefault(key, []).append(finding)
    clusters: list[list[CriticFinding]] = list(by_question.values())

    # Step 2: If more clusters than max_questions, consolidate by req_id.
    if len(clusters) > max_questions:
        by_req: dict[str, list[CriticFinding]] = {}
        for cluster in clusters:
            anchor_req = sorted({f.req_id for f in cluster})[0]
            by_req.setdefault(anchor_req, []).extend(cluster)
        clusters = list(by_req.values())

    # Step 3: If still > max_questions, consolidate lowest-priority overflow by critic domain.
    if len(clusters) > max_questions:
        by_critic: dict[str, list[CriticFinding]] = {}
        for finding in unresolved:
            by_critic.setdefault(finding.critic.value, []).append(finding)
        if len(by_critic) <= max_questions:
            clusters = list(by_critic.values())

    # Rank clusters by (max severity, info gain score, req_id) and cap at max_questions.
    ranked_clusters = sorted(
        clusters,
        key=lambda cl: (
            -SEVERITY_RANK[_score_cluster(cl)[0]],
            -_score_cluster(cl)[1],
            sorted({f.req_id for f in cl})[0],
        ),
    )[:max_questions]

    return [
        _render_cluster_question(cluster, idx)
        for idx, cluster in enumerate(ranked_clusters, start=1)
    ]


def is_converged(findings: Sequence[CriticFinding]) -> bool:
    """Return True iff there are 0 unresolved BLOCKER and 0 unresolved MAJOR findings."""
    for finding in findings:
        if not finding.resolved and finding.severity in (
            FindingSeverity.BLOCKER,
            FindingSeverity.MAJOR,
        ):
            return False
    return True


__all__ = [
    "cluster_and_rank_questions",
    "deduplicate_findings",
    "is_converged",
]
