"""Build PRD-to-spec pair candidates from open-project proposal history.

Proposal evaluation data (Section 4): pair PRD-style proposals with their
accepted specifications from Kubernetes Enhancement Proposals (KEPs),
Rust RFCs and Python PEPs.

Pair construction (candidate, NOT gold - PROJECT_PLAN.md research gap 1):
  proposal side = the document's content at the commit that ADDED it
                  (only what was visible at proposal time; nothing later
                  is folded in - this is the leakage guard),
  spec side     = the document's content at HEAD (the evolved, discussed,
                  accepted state).

These pairs are candidates. A human must validate a sample (checklist in
the output) before the pairs are frozen or used for evaluation; pairing
quality is an open research question, not an assumption.

Storage: blobless git clones live in data/raw/pairs/ (not committed).
This script writes data/raw/pairs/candidates/<source>/<id>/{proposal,spec}.txt
and data/frozen/pairs/pilot_sample.json (the recorded pilot sample +
hand-validation checklist). Full freeze happens only after validation.

No API keys needed: uses plain git with --filter=blob:none.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "pairs"
OUT = ROOT / "data" / "frozen" / "pairs"

SOURCES = {
    # name -> (clone URL, path regex for candidate documents, id regex)
    "kep": ("https://github.com/kubernetes/enhancements.git",
            r"keps/sig-[^/]+/\d+-[^/]+/README\.md$",
            r"keps/sig-[^/]+/(\d+)-[^/]+/README\.md$"),
    "rust-rfc": ("https://github.com/rust-lang/rfcs.git",
                 r"text/\d+-[^/]+\.md$",
                 r"text/(\d+)-[^/]+\.md$"),
    "pep": ("https://github.com/python/peps.git",
            r"peps/pep-\d+\.(rst|md)$",
            r"peps/pep-(\d+)\.(rst|md)$"),
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True).stdout


def ensure_clone(name: str, url: str) -> Path:
    dest = RAW / f"{name}.git"
    if not dest.exists():
        print(f"clone (blobless) {url}")
        subprocess.run(["git", "clone", "--filter=blob:none", "--bare", url, str(dest)],
                       check=True)
    else:
        print(f"fetch update {name}")
        subprocess.run(["git", "-C", str(dest), "fetch", "--filter=blob:none", "origin"],
                       check=True)
    return dest


def head_ref(repo: Path) -> str:
    # Bare clone: default branch lives under refs/heads/*, not refs/remotes.
    for ref in ("HEAD", "master", "main"):
        try:
            return git(repo, "rev-parse", ref).strip()
        except subprocess.CalledProcessError:
            continue
    heads = git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/").split()
    if heads:
        return git(repo, "rev-parse", heads[0]).strip()
    raise RuntimeError("no HEAD ref found")


def candidates(repo: Path, head: str, path_re: str, id_re: str) -> list[dict]:
    paths = git(repo, "ls-tree", "-r", "--name-only", head).splitlines()
    out = []
    rx, rx_id = re.compile(path_re), re.compile(id_re)
    for p in paths:
        if rx.search(p):
            m = rx_id.search(p)
            out.append({"path": p, "id": m.group(1) if m else p})
    return out


def build_pair(repo: Path, head: str, cand: dict, dest: Path) -> dict | None:
    """Write proposal/spec text for one candidate; None if not buildable."""
    path = cand["path"]
    # --follow traces the document across repo restructures (e.g. the
    # Sept 2023 peps/ move); without it, the "adding" commit of a moved
    # path is the MOVE commit and the proposal side silently contains
    # post-acceptance content - leakage into the proposal.
    followed = True
    proposal_path = path
    try:
        log = git(repo, "log", "--follow", "--name-status",
                          "--format=COMMIT:%H", head, "--", path)
        # Track the path backwards through renames to its true origin.
        current_commit, current_path, first_commit = None, path, None
        for line in log.splitlines():
            if line.startswith("COMMIT:"):
                current_commit = line[7:]
                first_commit = current_commit
            elif line.startswith("R") and "	" in line:
                _, src, dst = line.split("	")
                if dst == current_path:
                    current_path = src
        proposal_path = current_path
    except subprocess.CalledProcessError:
        first_commit = None
    if not first_commit:
        followed = False
        try:
            added = git(repo, "log", "--diff-filter=A", "--format=%H",
                        head, "--", path).splitlines()
        except subprocess.CalledProcessError:
            return None
        if not added:
            return None
        first_commit = added[-1]
    try:
        proposal = git(repo, "show", f"{first_commit}:{proposal_path}")
        spec = git(repo, "show", f"{head}:{path}")
    except subprocess.CalledProcessError:
        return None
    if len(proposal.strip()) < 200 or len(spec.strip()) < 200:
        return None  # too thin to be a usable pair
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "proposal.txt").write_text(proposal)
    (dest / "spec.txt").write_text(spec)
    when = git(repo, "show", "-s", "--format=%cI", first_commit).strip()
    return {"id": cand["id"], "path": path,
            "added_commit": first_commit, "added_at": when,
            "rename_followed": followed,
            "proposal_path": proposal_path,
            "head": head,
            "proposal_sha256": hashlib.sha256(proposal.encode()).hexdigest(),
            "spec_sha256": hashlib.sha256(spec.encode()).hexdigest(),
            "proposal_bytes": len(proposal.encode()),
            "spec_bytes": len(spec.encode())}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sample", type=int, default=4,
                    help="candidates per source for the pilot sample")
    ap.add_argument("--sources", default=",".join(SOURCES),
                    help="comma list of sources (kep,rust-rfc,pep)")
    args = ap.parse_args()

    sample_records = []
    for name in args.sources.split(","):
        url, path_re, id_re = SOURCES[name]
        repo = ensure_clone(name, url)
        head = head_ref(repo)
        cands = candidates(repo, head, path_re, id_re)
        print(f"{name}: {len(cands)} candidate documents at {head[:8]}")
        # Deterministic sample: rank by sha256 of path.
        cands.sort(key=lambda c: hashlib.sha256(c["path"].encode()).hexdigest())
        built = 0
        for cand in cands:
            if built >= args.sample:
                break
            dest = RAW / "candidates" / name / cand["id"]
            rec = build_pair(repo, head, cand, dest)
            if rec:
                rec["source"] = name
                sample_records.append(rec)
                built += 1
        print(f"{name}: built {built} sample pairs")

    OUT.mkdir(parents=True, exist_ok=True)
    # Merge with any existing pilot sample (per-source runs accumulate).
    existing_pairs = []
    existing_validation = {}
    pilot_path = OUT / "pilot_sample.json"
    if pilot_path.exists():
        prev = json.loads(pilot_path.read_text())
        existing_pairs = prev.get("pairs", [])
        existing_validation = prev.get("validation", {})
    have = {(r["source"], r["id"]) for r in sample_records}
    merged = [r for r in existing_pairs if (r["source"], r["id"]) not in have]
    sample_records = merged + sample_records
    pilot = {
        "created_by": "training/build_pairs.py",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "PILOT SAMPLE - candidates only, not frozen, not gold",
        "construction": {
            "proposal_side": "document content at its adding commit only "
                             "(proposal-time visibility; leakage guard)",
            "spec_side": "document content at HEAD at build time",
        },
        "hand_validation_checklist": [
            "proposal side reads like a proposal/motivation, not a finished spec",
            "spec side is the evolved version of the SAME proposal",
            "no later revisions folded into the proposal side",
            "pair is usable: enough content on both sides to extract FRs/NFRs",
        ],
        "validation": {f"{r['source']}:{r['id']}": existing_validation.get(
            f"{r['source']}:{r['id']}",
            {"reviewer": None, "checks_passed": None, "notes": None})
            for r in sample_records},
        "pairs": sample_records,
        "next_step": "Hand-validate this sample, then freeze a validated pair "
                     "set with a by-proposal split before any evaluation.",
    }
    (OUT / "pilot_sample.json").write_text(json.dumps(pilot, indent=2) + "\n")
    print(f"wrote {OUT/'pilot_sample.json'} with {len(sample_records)} pairs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
