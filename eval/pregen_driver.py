"""Pre-freeze bounded GENERATION driver (resumable, budgeted).

Per doc: B1,B2,B3 concurrently (one canonical harness log each,
run_id <baseline>-pure-dev-pregen-20261106-<item_id>). Manifest tracks
per-baseline completion; reruns run only missing baselines. Bounded
429/503 repair: sleep 65s and retry, max 2 retries per baseline per
invocation; on sustained failure the invocation pauses (resumable),
never an uncontrolled loop. Gold JSON read ONLY for the prd_path->text
mapping. Usage: pregen_driver.py <budget_seconds>
"""
import json, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0, "src"); sys.path.insert(0, ".")
from eval.harness import ExperimentConfig, run_experiment
from specpipe.llm import GeminiLLM, LLMError

GOLD_DEV = Path("data/frozen/gold/dev")
TEXT_DEV = Path("data/frozen/pure/text/dev")
MANIFEST = Path("eval/runs/PREGEN-20261106-manifest.json")
BASELINES = ("B1", "B2", "B3")
NOTE = ("bounded dev generation (120k-char prefix scope), pre-freeze "
        "generation only - UNSCORED until gold FROZEN; not the final "
        "TEST protocol")
BUDGET = float(sys.argv[1]) if len(sys.argv) > 1 else 95.0
START = time.time()

items = []
for path in sorted(GOLD_DEV.glob("*.gold.json")):
    item_id = path.name.replace(".gold.json", "")
    raw = json.loads(path.read_text(encoding="utf-8"))
    doc_name = Path(raw["prd_path"]).name + ".txt"
    text_path = TEXT_DEV / doc_name
    if text_path.exists():
        items.append({"id": item_id,
                      "prd_text": text_path.read_text(encoding="utf-8")})

if MANIFEST.exists():
    state = json.loads(MANIFEST.read_text())
else:
    state = {"label": NOTE, "scope_chars": 120000, "docs": {},
             "status": "running",
             "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

def save():
    MANIFEST.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

def run_one(b, item):
    cfg = ExperimentConfig(baseline=b, dataset_id="pure-dev-frozen",
                           repeats=1, input_scope_chars=120000,
                           persist_requirements=True, note=NOTE)
    rid = f"{b}-pure-dev-pregen-20261106-{item['id']}"
    return b, run_experiment(cfg, [item], GeminiLLM, "eval/runs", run_id=rid)

def run_one_bounded(b, item):
    """Bounded 429/503 repair: 65s sleep between attempts, max 3 tries."""
    for attempt in range(3):
        try:
            return run_one(b, item)
        except LLMError as exc:
            msg = str(exc)
            if ("429" in msg or "503" in msg) and attempt < 2:
                time.sleep(65)
                continue
            raise

def log_ok(p):
    try:
        return bool(p) and Path(p).is_file() and Path(p).stat().st_size > 0
    except Exception:
        return False

def missing_baselines(doc_rec):
    logs = (doc_rec or {}).get("logs", {})
    return [b for b in BASELINES if not log_ok(logs.get(b, ""))]

def doc_done(doc_rec):
    return not missing_baselines(doc_rec)

done = {d for d, r in state["docs"].items() if doc_done(r)}
remaining = [i for i in items if i["id"] not in done]
partial = [i["id"] for i in remaining if state["docs"].get(i["id"], {}).get("logs")]
print(f"items={len(items)} done={len(done)} remaining={len(remaining)} "
      f"(partial={len(partial)}) budget={BUDGET}s", flush=True)

pause = False
for item in remaining:
    if time.time() - START > BUDGET:
        pause = True
        break
    t0 = time.time()
    rec = state["docs"].get(item["id"], {"logs": {}})
    need = missing_baselines(rec)
    try:
        with ThreadPoolExecutor(max_workers=3) as ex:
            futs = [ex.submit(run_one_bounded, b, item) for b in need]
            for f in futs:
                b, log = f.result()
                rec["logs"][b] = log
                save()
    except LLMError as exc:
        rec["last_error"] = str(exc)[:200]
        state["docs"][item["id"]] = rec
        state["status"] = f"paused: sustained LLMError on {item['id']}"
        save()
        print(f"PAUSED: sustained LLMError on {item['id']}: "
              f"{str(exc)[:120]}", flush=True)
        sys.exit(2)
    rec["status"] = "done"
    rec["seconds"] = round(time.time() - t0, 1)
    rec.pop("last_error", None)
    state["docs"][item["id"]] = rec
    save()
    print(f"done {item['id']} ({','.join(need)}) in {rec['seconds']}s", flush=True)

if pause:
    state["status"] = "paused (budget)"
    save()
    print("PAUSED: budget spent", flush=True)
    sys.exit(0)
state["status"] = "done"
save()
print("PREGEN DONE", flush=True)
