"""Merge human verdicts over LLM suggestions into FROZEN gold files.

Reads data/frozen/gold_verdicts/<split>/<item>.<annotator>.verdict.json
files produced by verify_suggestions.py and promotes them to gold.

FAIL-CLOSED document eligibility (docs/evaluation.md rubric R5, aligned
with eval/gold.py::gold_doc_scorable): a document is promoted to
"FROZEN - human-verified (anchored)" ONLY when every suggestion has a
VERIFIED resolution and every annotator confirmed reading the full LLM
scope:

- single annotator: accept/reject/edit verdicts stand; a skip is
  UNRESOLVED.
- two annotators: majority accept promotes (ties and disagreements are
  UNRESOLVED "no_majority"); both reject is a verified rejection;
  contested edits (accepts with differing new_kind/new_text) are
  UNRESOLVED "contested_edit" - no last-wins.
- any annotator who did not read/confirm the full LLM scope makes the
  document REVIEW-INCOMPLETE.

Unresolved suggestions are recorded in provenance.excluded under their
class ("skipped", "no_majority", "contested_edit"); verified rejections
are recorded under "rejected" (resolved - they do not affect
eligibility, and a prediction matching one still counts as a false
positive). ANY unresolved class makes the document REVIEW-INCOMPLETE:
not FROZEN, refused by eval/gold.py, never scored. No partial-gold PRF.

Kappa (kappa_accept_reject) is the ANCHORED verification agreement on
accept/reject over items where BOTH annotators decided accept or
reject; skips are excluded from kappa and recorded in excluded.

Implied categories: when all annotators reviewed them (verdict field
implied_categories_reviewed) and agree, the merged list is written to
gold.implied_categories with provenance.implied_categories_verified =
true (H3 eligibility, eval/scoring.py::h3_eligible). Unreviewed or
disagreeing categories leave the document FROZEN for H1/H2 but
ineligible for H3 (vacuous completeness would inflate the ratio).

Original skeletons are backed up under data/frozen/gold/_skeleton_backup/
on first write.

Usage: python training/merge_verdicts.py --split dev [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "data" / "frozen" / "gold"
VERDICTS = ROOT / "data" / "frozen" / "gold_verdicts"
BACKUP = GOLD / "_skeleton_backup"


def cohen_kappa(a: list, b: list) -> float | None:
    """Cohen's kappa over two aligned label lists."""
    if len(a) != len(b) or not a:
        return None
    n = len(a)
    agree = sum(1 for x, y in zip(a, b) if x == y)
    ca, cb = Counter(a), Counter(b)
    pe = sum((ca[k] / n) * (cb[k] / n) for k in set(ca) | set(cb))
    if pe == 1.0:
        return 1.0
    return (agree / n - pe) / (1 - pe)


def load_item_verdicts(split: str, item_id: str) -> list[dict]:
    out = []
    for p in sorted((VERDICTS / split).glob(f"{item_id}.*.verdict.json")):
        out.append(json.loads(p.read_text()))
    return out


def _read_full_scope(v: dict) -> bool:
    rs = v.get("review_scope") or {}
    return bool(rs.get("reviewer_read_full_llm_scope"))


def _aggregate_scope(vs: list[dict], full: bool) -> dict:
    scopes = [v.get("review_scope") or {} for v in vs]
    return {
        "doc_chars_total": max((s.get("doc_chars_total") or 0)
                               for s in scopes),
        "llm_scope_chars": max((s.get("llm_scope_chars") or 0)
                               for s in scopes),
        "chars_presented_to_reviewer": min(
            (s.get("chars_presented_to_reviewer") or 0) for s in scopes),
        "reviewer_read_full_llm_scope": full,
    }


def _merge_implied(vs: list[dict], excluded: dict) -> tuple[list, bool]:
    """Merged implied categories + verified flag (H3 eligibility)."""
    if not all(v.get("implied_categories_reviewed") for v in vs):
        return [], False
    lists = [sorted(v.get("implied_categories") or []) for v in vs]
    if any(l != lists[0] for l in lists):
        excluded.setdefault("contested_implied", []).append(
            "implied_categories")
        return [], False
    return lists[0], True


def promote(split: str, item_id: str, dry: bool) -> dict | None:
    vs = load_item_verdicts(split, item_id)
    if not vs:
        return None
    sugg_path = ROOT / vs[0]["source_suggestion_file"]
    sugg = {r["id"]: r for r in json.loads(sugg_path.read_text())
            ["suggested_requirements"]}
    skel_path = GOLD / split / f"{item_id}.gold.json"
    skel = json.loads(skel_path.read_text())

    excluded: dict[str, list] = {}
    gold_reqs = []
    blocked = []

    # --- scope gate: every annotator must have read + confirmed the full
    #     LLM scope (fail-closed; eval/gold.py enforces the same rule)
    incomplete = [v["annotator"] for v in vs if not _read_full_scope(v)]
    if incomplete:
        blocked.append("incomplete review scope: " + ", ".join(incomplete))

    # --- per-suggestion resolution
    for rid, r in sugg.items():
        decisions = [next((x for x in v["verdicts"] if x["id"] == rid),
                          None) for v in vs]
        if any(d is None or d["decision"] == "s" for d in decisions):
            excluded.setdefault("skipped", []).append(rid)
            continue
        accepts = [d for d in decisions if d["decision"] == "a"]
        if len(accepts) * 2 <= len(decisions):
            if accepts:
                excluded.setdefault("no_majority", []).append(rid)
            else:
                excluded.setdefault("rejected", []).append(rid)
            continue
        # majority accept: edits must agree across accepting annotators
        kinds = {d.get("new_kind") for d in accepts}
        texts = {d.get("new_text") for d in accepts}
        if len(kinds) > 1 or len(texts) > 1:
            excluded.setdefault("contested_edit", []).append(rid)
            continue
        kind = next(iter(kinds)) or r["kind"]
        text = next(iter(texts)) or r["text"]
        gold_reqs.append({"id": rid, "kind": kind, "text": text,
                          "source": "llm-suggestion+human-verify"})
    added_ids = set()
    for v in vs:
        for a in v["added_requirements"]:
            key = a["text"].strip().lower()
            if key in added_ids:
                continue
            added_ids.add(key)
            gold_reqs.append({
                "id": f"human-{len(added_ids):03d}",
                "kind": a["kind"], "text": a["text"],
                "source": f"human-added:{v['annotator']}"})

    implied, implied_verified = _merge_implied(vs, excluded)

    # --- anchored accept/reject kappa over items both annotators decided
    kappa_report = {}
    if len(vs) > 1:
        ids = [r["id"] for r in sugg.values()]
        for va, vb in combinations(vs, 2):
            da, db = ([next((x["decision"] for x in v["verdicts"]
                             if x["id"] == i), None) for i in ids]
                      for v in (va, vb))
            keep = [(x, y) for x, y in zip(da, db)
                    if x in ("a", "r") and y in ("a", "r")]
            k = (cohen_kappa([x for x, _ in keep], [y for _, y in keep])
                 if keep else None)
            kappa_report[f"{va['annotator']}~{vb['annotator']}"] = k

    unresolved = {c: ids for c, ids in excluded.items()
                  if c != "rejected" and ids}
    if unresolved:
        blocked.append("unresolved suggestions: " + ", ".join(
            f"{c}={len(i)}" for c, i in sorted(unresolved.items())))
    if not gold_reqs and not unresolved:
        blocked.append("no verified requirements remain (all rejected, "
                       "none added) - adjudicate or drop from scoring set")

    prov = skel["provenance"]
    prov["verified_by"] = [v["annotator"] for v in vs]
    if kappa_report:
        prov["kappa_accept_reject"] = kappa_report
    prov["review_scope"] = _aggregate_scope(vs, not incomplete)
    if excluded:
        prov["excluded"] = excluded
    if implied_verified:
        skel["implied_categories"] = implied
        prov["implied_categories_verified"] = True

    n_ann = len(vs)
    if blocked:
        prov["status"] = "REVIEW-INCOMPLETE (anchored)"
        prov["blocked_reasons"] = blocked
        # partial gold kept as adjudication evidence only; the status is
        # not FROZEN-prefixed, so eval/gold.py refuses to load it
        skel["gold_requirements"] = gold_reqs
    else:
        prov["status"] = (
            f"FROZEN - human-verified (anchored, "
            f"{'single' if n_ann == 1 else str(n_ann)} annotator"
            f"{'s' if n_ann > 1 else ''})")
        skel["gold_requirements"] = gold_reqs
    if not dry:
        BACKUP.mkdir(exist_ok=True)
        bak = BACKUP / split / f"{item_id}.gold.json"
        if not bak.exists():
            bak.parent.mkdir(parents=True, exist_ok=True)
            bak.write_text(skel_path.read_text())
        skel_path.write_text(json.dumps(skel, indent=2) + "\n",
                             encoding="utf-8")
    return {"item_id": item_id, "annotators": n_ann,
            "requirements": len(gold_reqs), "kappa": kappa_report,
            "blocked": blocked,
            "status": ("blocked" if blocked else "frozen")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=["dev", "test"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    items = sorted({p.name.split(".")[0]
                    for p in (VERDICTS / args.split).glob("*.verdict.json")}
                   ) if (VERDICTS / args.split).exists() else []
    if not items:
        print(f"no verdict files under {VERDICTS / args.split}")
        return 1
    done = [promote(args.split, i, args.dry_run) for i in items]
    done = [d for d in done if d]
    frozen = [d for d in done if not d["blocked"]]
    held = [d for d in done if d["blocked"]]
    print(json.dumps({"promoted": len(frozen),
                      "review_incomplete": len(held),
                      "dry_run": args.dry_run, "items": done}, indent=2))
    if held:
        print("\nREVIEW-INCOMPLETE documents (need re-review or "
              "adjudication before scoring):")
        for d in held:
            print(f"  {d['item_id']}: {'; '.join(d['blocked'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
