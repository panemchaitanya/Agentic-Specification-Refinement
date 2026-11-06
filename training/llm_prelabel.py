"""LLM pre-labeling of PURE gold skeletons (owner-approved free-model route).

Approved by the owner 6 Nov 2026 (~12:07 IST): free-model labeling on the
real dataset, documented honestly as free-tier labels, upgradeable before
30 Nov. Output is NEVER gold: each suggestion file carries
provenance.status "LLM-SUGGESTED - unverified", which eval/gold.py's
fail-closed whitelist rejects. A human verification + adjudication pass
(annotate_critic.py flow) is what may later promote labels to FROZEN gold.

Resumable: items with an existing suggestion file are skipped. Stops
cleanly on --max-seconds (for sandboxed runs), --max-calls, or the first
unrecoverable LLM error; re-run to continue.

Usage: GEMINI_API_KEY=... python training/llm_prelabel.py [--split dev]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from specpipe.llm import GeminiLLM, LLMError
from specpipe.schemas import NFR_CATEGORIES

GOLD_DIR = ROOT / "data" / "frozen" / "gold"
TEXT_DIR = ROOT / "data" / "frozen" / "pure" / "text"
OUT_DIR = ROOT / "data" / "frozen" / "gold_llm_suggestions"
LOG_DIR = ROOT / "eval" / "runs"
MAX_DOC_CHARS = 120_000
MAX_REQS = 40
KINDS = set(NFR_CATEGORIES) | {"FR"}

SYSTEM = """You are a requirements extractor. Given a software requirements
document, extract its requirements as JSON. Rules:
- FR: a behavior the system performs (actor + action + object).
- NFR: a quality/constraint/property, one of: security, performance,
  scalability, reliability, usability, other.
- Quality statements ("must be fast", "shall be secure") are NFRs, never FRs.
- Each text must be a single, atomic, testable requirement sentence.
- Be conservative: extract only requirements the document actually states
  or directly implies as obligations; do not invent.
Return ONLY JSON: {"requirements": [{"kind": "FR"|"security"|..., "text": "..."}],
"implied_categories": [...categories present as NFRs...]}."""

PROMPT_VERSION = "prelabel-v1"


def extract_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return json.loads(text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=["dev", "test"])
    ap.add_argument("--max-calls", type=int, default=150)
    ap.add_argument("--max-seconds", type=float, default=100)
    ap.add_argument("--sleep", type=float, default=2.0)
    args = ap.parse_args()

    llm = GeminiLLM()
    started = time.time()
    run_id = f"PRELABEL-{args.split}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    log_path = LOG_DIR / f"{run_id}.jsonl"
    out_dir = OUT_DIR / args.split
    out_dir.mkdir(parents=True, exist_ok=True)
    calls = done = skipped = 0

    with open(log_path, "a", encoding="utf-8") as log:
        log.write(json.dumps({
            "type": "header", "run_id": run_id, "label": "llm-prelabel",
            "note": ("free-tier LLM suggestions on real PURE text "
                     "(owner-approved 6 Nov); UNVERIFIED, never gold"),
            "model": llm.model, "prompt_version": PROMPT_VERSION,
            "split": args.split}) + "\n")
        for gold_path in sorted((GOLD_DIR / args.split).glob("*.gold.json")):
            item_id = gold_path.stem.replace(".gold", "")
            out_path = out_dir / f"{item_id}.llm_suggestions.json"
            if out_path.exists():
                skipped += 1
                continue
            if calls >= args.max_calls or time.time() - started > args.max_seconds:
                log.write(json.dumps({"type": "stopped", "calls": calls,
                                      "done": done, "skipped": skipped}) + "\n")
                print(f"stop: calls={calls} done={done} skipped={skipped} "
                      f"(resume by re-running)")
                return 0
            skel = json.loads(gold_path.read_text(encoding="utf-8"))
            doc_name = Path(skel["prd_path"]).name + ".txt"
            text_path = TEXT_DIR / args.split / doc_name
            if not text_path.exists():
                log.write(json.dumps({"type": "error", "item_id": item_id,
                                      "error": f"text missing: {text_path.name}"}) + "\n")
                continue
            doc = text_path.read_text(encoding="utf-8", errors="replace")
            truncated = len(doc) > MAX_DOC_CHARS
            doc = doc[:MAX_DOC_CHARS]
            try:
                raw = llm.complete(SYSTEM, "Document:\n" + doc)
                payload = extract_json(raw)
            except (LLMError, json.JSONDecodeError, ValueError) as exc:
                log.write(json.dumps({"type": "error", "item_id": item_id,
                                      "error": str(exc)[:300]}) + "\n")
                print(f"ERROR on {item_id}: {str(exc)[:120]}; partial log kept, re-run to resume")
                return 1
            calls += 1
            reqs, seen = [], set()
            for r in payload.get("requirements", []):
                kind, text = r.get("kind"), str(r.get("text", "")).strip()
                if kind not in KINDS or not text or text.lower() in seen:
                    continue
                seen.add(text.lower())
                reqs.append({"id": f"G{len(reqs)+1}", "kind": kind, "text": text})
                if len(reqs) >= MAX_REQS:
                    break
            cats = [c for c in payload.get("implied_categories", [])
                    if c in NFR_CATEGORIES]
            suggestion = {
                "item_id": item_id,
                "suggested_requirements": reqs,
                "suggested_implied_categories": sorted(set(cats)),
                "provenance": {
                    "status": "LLM-SUGGESTED - unverified, not gold",
                    "model": llm.model, "prompt_version": PROMPT_VERSION,
                    "generated_at": f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
                    "source_document_sha256": hashlib.sha256(
                        doc.encode()).hexdigest(),
                    "doc_truncated": truncated,
                    "token_usage": llm.last_usage,
                },
            }
            out_path.write_text(json.dumps(suggestion, indent=2) + "\n",
                                encoding="utf-8")
            log.write(json.dumps({"type": "prelabel", "item_id": item_id,
                                  "n_suggested": len(reqs),
                                  "truncated": truncated,
                                  "token_usage": llm.last_usage}) + "\n")
            done += 1
            time.sleep(args.sleep)
        log.write(json.dumps({"type": "complete", "calls": calls,
                              "done": done, "skipped": skipped}) + "\n")
    print(f"complete: calls={calls} done={done} skipped={skipped} log={log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
