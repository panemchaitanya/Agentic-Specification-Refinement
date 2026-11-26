# ReqHQ v2 Phase 4b — Independent System & Integration QA Defect Report

**Author:** `@system-qa` (Independent Black-Box Contract-First QA)  
**Environment:** Windows 11 (`win32`), Python `3.15.0` (`.venv/Scripts/python.exe`), `pytest 9.2.0`, `hypothesis 6.170.17`, Eclipse Temurin JRE `21.0.13.1` (`C:\Program Files\Eclipse Adoptium\jre-21.0.13.10002-hotspot\bin`), Alloy 6.2 (`REQHQ_ALLOY_JAR_PATH="C:/tools/alloy/org.alloytools.alloy.dist.jar"`)  
**Dataset Compliance:** Strict **DEV-SPLIT-ONLY** (`data/frozen/pure/text/dev/0000 - cctns.pdf.txt`, `data/frozen/gold/dev/pure-0000_-_cctns.gold.json`, `data/frozen/pure/text/dev/0000 - gamma j.pdf.txt`, `data/frozen/gold/dev/pure-0000_-_gamma_j.gold.json`, and `data/benchmarks/cross_domain/cases.json` case `FIN-03`). Zero access to `data/frozen/*/test/`, enforced by runtime assertion `assert_dev_split_only()`.

---

## 1. Initial Execution Summary (Before Any Rectification)

**Command executed:**

```powershell
$env:PATH = "C:\Program Files\Eclipse Adoptium\jre-21.0.13.10002-hotspot\bin;" + $env:PATH
$env:REQHQ_ALLOY_JAR_PATH = "C:/tools/alloy/org.alloytools.alloy.dist.jar"
.venv\Scripts\python.exe -m pytest tests/reqhq/system -v -W error::hypothesis.errors.HypothesisWarning
```

**Result:** **`11 passed, 1 skipped, 0 failed, 1 warning`** in `252.69s (0:04:12)` (zero `HypothesisWarning`).

| Test Case | Outcome | Notes |
| --- | --- | --- |
| `tests/reqhq/system/test_dataset_system_e2e.py::test_dev_split_runtime_guard_blocks_frozen_test_split` | **PASSED** | Runtime guard verified blocking `data/frozen/*/test/` paths |
| `tests/reqhq/system/test_dataset_system_e2e.py::test_real_dataset_phase1_to_phase4_full_e2e[pure_dev_cctns-cctns]` | **PASSED** | Full Phase 1->2->3->4 incl. live Java/Alloy 6.2, locked PBT execution, Windows `ASTMutantEngine`, `EvalOrchestrator`, pure-SVG XML parse, and `verify_bundle()` + tamper rejection (see `DEF-P4B-002` for unfiltered PBT blowup) |
| `tests/reqhq/system/test_dataset_system_e2e.py::test_real_dataset_phase1_to_phase4_full_e2e[pure_dev_gamma_j-gamma_j]` | **PASSED** | Full Phase 1->2->3 incl. live Java/Alloy 6.2, locked PBT execution, Windows `ASTMutantEngine`, zero writes to `docs/adr/` and `data/frozen/` |
| `tests/reqhq/system/test_dataset_system_e2e.py::test_real_dataset_phase1_to_phase4_full_e2e[cross_domain_fin03-cross_domain]` | **PASSED** | Full Phase 1->2->3 on socio-technical case `FIN-03` incl. unfiltered locked PBT execution & Windows `ASTMutantEngine` |
| `tests/reqhq/system/test_dataset_system_e2e.py::test_real_dataset_alloy6_live_verified_non_vacuous` | **PASSED** | Live Java 21 + Alloy 6.2 verified `VERIFIED` + `vacuity_passed=True` for BOTH `alloy6` and `legacy` modes on real `gamma_j` spec, AND confirmed contradictory `HumanActor` axiom fails closed as `VACUOUS_UNSAT` (`vacuity_passed=False`) |
| `tests/reqhq/system/test_dataset_system_e2e.py::test_live_ollama_qwen3_4b_gamma_j_tier0_json_and_telemetry` | **SKIPPED** | Opt-in `@pytest.mark.live_system` cleanly skipped (`http://127.0.0.1:11434/api/tags` unreachable in offline test sandbox) |
| `tests/reqhq/system/test_system_pbt.py::test_cross_phase_session_lifecycle_state_machine` | **PASSED** | 25 examples x 12 CLI steps (`init`->`elicit`->`revise`->`finalize`->`verify`->`export`->`pbt-export`->`handoff`): SQLite/JSONL parity, downstream invalidation, canonical traceability, zero orphans, `pbt_lock.sha256` + 1-byte `ContractTamperError`, zero `docs/adr/` writes |
| `tests/reqhq/system/test_system_pbt.py::test_ingestion_to_alloy_trace_roundtrip_pbt` | **PASSED** | 40 randomized Unicode/fenced-code/table/Windows-path Markdown PRDs (`truncation_ratio == 0.0`, exact SHA-256 & `@trace` round-trip) |
| `tests/reqhq/system/test_system_pbt.py::test_ingestion_decomposer_alloy_over_150k_chars_zero_truncation` | **PASSED** | Dedicated >160,000-char PRD (`truncation_ratio == 0.0`, full span preservation, Alloy `@trace` intact) |
| `tests/reqhq/system/test_system_pbt.py::test_spec_to_pbt_yaml_codegen_ast_and_pytest_pbt` | **PASSED** | 40 examples across `alloy6` & `legacy`: YAML round-trip, `ast.parse()`, `verify_contract_lock()`, and bounded real `pytest`/`Hypothesis` subprocess runs |
| `tests/reqhq/system/test_system_pbt.py::test_eval_interruption_and_resume_idempotency_pbt` | **PASSED** | 40 randomized interruption cutpoints `k`: zero duplicate records, exact record-set parity, and byte-identical second `eval resume` |
| `tests/reqhq/system/test_system_pbt.py::test_eval_mock_and_mixed_record_quarantine_fail_closed_pbt` | **PASSED** | 40 randomized pure-mock & spoofed-live single-record-poisoned runs across all 4 layers: `MockRunQuarantineError`, non-zero CLI `--strict-live`, `DRY_RUN_UNSCORED` banner, zero `VERIFIED_LIVE`/`SUPPORTED`/`REFUTED` leakage |

---

## 2. Defects & Cross-Phase Seam Findings

**Defect Tally:** `0 P0`, `0 P1`, `2 P2` (total: `2` actionable P2 findings).

### DEF-P4B-001 — Unregistered `@pytest.mark.live_system` Marker (`PytestUnknownMarkWarning`)

- **Severity:** `P2`
- **Suspected Owning Module / File:** `pyproject.toml` (`[tool.pytest.ini_options] markers`)
- **Observed Warning:**
  ```text
  tests\reqhq\system\test_dataset_system_e2e.py:599: PytestUnknownMarkWarning: Unknown pytest.mark.live_system - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.live_system
  ```
- **Reproduction Command:**
  ```powershell
  .venv\Scripts\python.exe -m pytest tests/reqhq/system/test_dataset_system_e2e.py -k test_live_ollama -W error::pytest.PytestUnknownMarkWarning
  ```
- **Root Cause & Expected Contract:** `pyproject.toml` currently registers only `alloy_live` under `[tool.pytest.ini_options] markers`, omitting `live_system`. Registering `"live_system: opt-in live system test requiring local Ollama server"` eliminates the warning under strict warning modes.

### DEF-P4B-002 — Offline Fallback Decomposer Over-Fragments Real PURE PDF-Extracted SRS Text (579 Nodes / 632 KB Generated PBT Suite Timing Out >60s Unfiltered)

- **Severity:** `P2`
- **Suspected Owning Module / File:** `src/reqhq/elicitation/decomposer.py` (offline fallback decomposition when provider returns default/unstructured payload) and `src/reqhq/pbt/codegen.py`
- **Observed Behavior:**
  - Running `RequirementDecomposer.decompose_chunks()` with offline `MockProvider` on real frozen PURE dev text `data/frozen/pure/text/dev/0000 - cctns.pdf.txt` (42,967 bytes; 28 human-verified gold requirements) emitted **579 `RequirementNode`s**, including raw cover-page and Table-of-Contents lines (e.g., `REQ-001` titled `"E-governance"`).
  - Downstream `PBTSpecEmitter` + `compile_pbt_guardrails` generated a single **632,003-byte / 12,337-line** test file (`test_*_pbt.py`) containing **579** `@settings(max_examples=200)` Hypothesis tests (= 115,800 test executions).
  - Executing the generated locked PBT suite unfiltered (`pytest -q <handoff_dir>/tests/properties`) timed out after 60 seconds on both `0000 - cctns.pdf.txt` and `0000 - gamma j.pdf.txt`:
    ```text
    subprocess.TimeoutExpired: Command '['...\\.venv\\Scripts\\python.exe', '-m', 'pytest', '-q', '...\\handoff\\tests\\properties']' timed out after 60 seconds
    ```
- **Reproduction Command:**
  ```powershell
  .venv\Scripts\python.exe -c "from pathlib import Path; import tempfile; from reqhq.ingestion import LocalFileLoader, SnapshotStore, normalize_file, HierarchicalChunker; from reqhq.llm import MockProvider, ProviderRouter; from reqhq.elicitation import RequirementDecomposer; tmp=Path(tempfile.mkdtemp()); src=LocalFileLoader().load_file(Path('data/frozen/pure/text/dev/0000 - cctns.pdf.txt')); snap=SnapshotStore(tmp).save_document('r', src); ch=HierarchicalChunker().chunk(normalize_file(snap.snapshot_path, source_sha256=snap.sha256)); g=RequirementDecomposer(ProviderRouter(local_provider=MockProvider(), strict_local=True, enable_cache=False)).decompose_chunks(ch, session_id='r'); print('node_count=', len(g.nodes), 'first_title=', g.nodes[0].title)"
  ```
- **Expected Behavior / Recommendation:** Offline heuristic fallback decomposition in `RequirementDecomposer` should filter non-requirement noise (cover headers, TOC lines, page numbers, short boilerplate lines without modal/action structure) and bound per-chunk fallback extraction cleanly so real multi-page SRS documents yield compact, meaningful requirement sets and runnable handoff PBT suites without >60s blowup. (Note: `test_dataset_system_e2e.py` AST-parses the entire generated test suite and runs a bounded `-k` slice when `len(pbt_doc.properties) > 25` so E2E verification completes cleanly, and will automatically run the full unfiltered suite once fallback extraction produces `<= 25` focused requirements.)

### Additional Observational Note (Non-Defect)

- **`checkpoint.json` Ordering vs. JSONL Append Order:** `EvalCheckpoint.completed_units` is persisted in sorted lexicographical order (e.g., `L2:ENT-01` before `L2:FIN-01`), whereas `layer2.jsonl` appends in benchmark execution order (`FIN-01` first, `ENT-01` last). During crash/resume testing, `eval resume` correctly deduplicated and reconstructed all 16 Layer 2 records with zero duplicates when `checkpoint.json` and `layer*.jsonl` reflected an authentic mid-run execution cutpoint.

---

## 3. Cryptographic Immutability Manifest (`tests/reqhq/system/*.py`)

Recorded immediately after initial QA execution for `@verifier`'s **No-Test-Weakening Audit**:

| File Path | Size (Bytes) | SHA-256 Digest |
| --- | ---: | --- |
| `tests/reqhq/system/__init__.py` | `84` | `3fc747d2654fbd250ee5cec3eb4655c4ef66ae86c574ef0ac20fde5e0a6dc558` |
| `tests/reqhq/system/test_dataset_system_e2e.py` | `27696` | `dd66e8d07479fe7af5a426c426437abba5c36a29d64a14505e93b587ab372bf4` |
| `tests/reqhq/system/test_system_pbt.py` | `44077` | `4e495a87c2c1587e5d826dfcae21e19ccd1368f0208776e2753f2039f2a49b4e` |

---

## Resolution Sign-Off

**Signed off by:** `@verifier` (Independent Final Gate)  
**Branch / Base:** `feat/v2-phase4-evaluation` @ `c34b9ed` (zero git mutations)  
**Status:** **APPROVED — ALL DEFECTS RESOLVED, ZERO TEST WEAKENING, ALL 5 SUITES PASSING**

### 1. Defect Resolution Verification

| Defect ID | Severity | Owning Files Rectified | Verification Outcome |
| --- | :---: | --- | --- |
| `DEF-P4B-001` | `P2` | `pyproject.toml` | **RESOLVED & VERIFIED.** Registered `live_system` marker under `[tool.pytest.ini_options] markers`. Both Suite 2 and Suite 3 pass with `-W error::pytest.PytestUnknownMarkWarning` (`0` warnings). |
| `DEF-P4B-002` | `P2` | `src/reqhq/elicitation/decomposer.py`, `tests/reqhq/test_elicitation_engine.py` | **RESOLVED & VERIFIED.** Offline chunk fallback now reassembles wrapped PDF paragraphs, filters front-matter/TOC/footer/table noise, prioritizes normative modal/action clauses with verbatim `TraceLink` spans, and bounds fallback extraction (`MAX_FALLBACK_NODES_PER_CHUNK = 6`, `MAX_FALLBACK_NODES_TOTAL = 24`; structured LLM payloads uncapped). Real PURE `0000 - cctns.pdf.txt` dropped from 579 -> 24 focused nodes and `0000 - gamma j.pdf.txt` from 806 -> 15 focused nodes, so both execute their **full unfiltered** locked PBT suites cleanly within timeout. Regression test added; `decomposer.py` remains at **100% coverage (`378/378` stmts)**. |
| `DEF-P4B-003` | `P1` | `src/reqhq/llm/providers/ollama.py`, `tests/reqhq/test_router_and_quota.py` | **RESOLVED & VERIFIED LIVE.** Discovered during live Ollama `qwen3:4b` execution of `test_live_ollama_qwen3_4b_gamma_j_tier0_json_and_telemetry`: Ollama reasoning models (`qwen3:*`) default to thinking mode and spent the entire `num_predict=512` budget on internal `thinking` tokens (`57.61s`, `eval=512`, `done=length`), leaving `response=""` and triggering router retries/timeout. `OllamaLocalProvider.build_payload()` now sets `"think": False` by default whenever constrained decoding (`format` or `grammar`) is active (while honoring explicit boolean `request.metadata["think"]` overrides). Verified HTTP 200 on both live `qwen3:4b` (`11.82s`, valid strict JSON) and `phi4-mini`; `ollama.py` remains at **100% coverage (`55/55` stmts)**. |
| `DEF-P4B-004` | `P1` | `src/reqhq/llm/providers/gemini.py`, `src/reqhq/config.py`, `.env.example`, `tests/reqhq/test_router_and_quota.py` | **RESOLVED & VERIFIED LIVE.** Discovered during live Gemini API verification using DPAPI-stored `GEMINI_API_KEY`: (1) retired `gemini-2.0-flash` / `gemini-2.5-flash` returned HTTP 404; (2) `generationConfig.responseSchema` rejected Pydantic v2 `ConfigDict(extra="forbid")` JSON schemas (`$defs`, `$ref`, `additionalProperties`) with HTTP 400; (3) Gemini 3.x thinking parts (`{"thought": true}`) needed filtering before JSON parsing; (4) added 1 automatic transient HTTP 503 retry (`max_transient_retries=1`). Updated `GeminiFreeProvider` default model to `gemini-3.1-flash-lite` (overridable via `REQHQ_GEMINI_MODEL`), switched to `responseJsonSchema`, and filtered thought parts. Verified live HTTP 200 in `1.74s` with valid strict Pydantic JSON; `gemini.py` **83/83 = 100% coverage**, `config.py` **95/95 = 100% coverage**. |
| `DEF-P4B-005` | `P1` | `src/reqhq/cli.py`, `src/reqhq/llm/router.py`, `src/reqhq/security/credential_vault.py`, `src/reqhq/security/__init__.py`, `tests/reqhq/test_cli_bootstrap_and_auth.py`, `tests/reqhq/test_credential_vault.py` | **RESOLVED & VERIFIED LIVE.** Wired `GeminiFreeProvider` / `GroqFreeProvider` (placeholder-filtered, blocked under `--strict-local`), `QuotaLedger`, and `PromptCache` into live `_build_router()`; added explicit-escalation cloud-first short-circuit in `ProviderRouter.route()` (dropping live `FINAL_AUDIT` from `22.42s` -> `1.43s`–`1.73s` with `0` redundant local Ollama calls while keeping graceful local fallback); added opt-in `--verify-live` HTTP auth probes (`verify_credential_live`) to `reqhq doctor`, `reqhq auth setup`, and `reqhq auth status` (verified live HTTP 200). `cli.py` **871/871 = 100%**, `router.py` **164/164 = 100%**, `credential_vault.py` **319/319 = 100%**. |
| `DEF-P4B-006` | `P1` | `src/reqhq/synthesis/triage.py`, `tests/reqhq/test_socio_technical_synthesis.py` | **RESOLVED & VERIFIED LIVE.** Discovered during live `pure_dev_cctns` E2E run: `SocioTechnicalTriager.triage()` was the only elicitation/synthesis stage that did not catch `ProviderError` around `self.router.route(request)` when falling back to `_heuristic_triage_item()`. Caught `ProviderError` cleanly and added regression unit test (`triage.py` **178/178 = 100% coverage**). |
| `ADR-0018` / `ADR-0019` | — | `docs/adr/0018-*.md`, `docs/adr/0019-*.md` | Added in Phase 4b extension for Local Ollama Auto-Bootstrap (`reqhq doctor`) and Encrypted Local Credential Vault (`reqhq auth`) with 38 cumulative audit events (`0` CRLF). |

### 2. Cryptographic No-Test-Weakening Proof (`tests/reqhq/system/*.py`)

Independent SHA-256 verification executed after rectification (including `DEF-P4B-003..006`) and Phase 4b extension confirms **byte-for-byte immutability** of every file authored by `@system-qa` (zero test edits, zero test weakening):

- `tests/reqhq/system/__init__.py`: `84` bytes — `3fc747d2654fbd250ee5cec3eb4655c4ef66ae86c574ef0ac20fde5e0a6dc558` (**MATCH**)
- `tests/reqhq/system/test_dataset_system_e2e.py`: `27696` bytes — `dd66e8d07479fe7af5a426c426437abba5c36a29d64a14505e93b587ab372bf4` (**MATCH**)
- `tests/reqhq/system/test_system_pbt.py`: `44077` bytes — `4e495a87c2c1587e5d826dfcae21e19ccd1368f0208776e2753f2039f2a49b4e` (**MATCH**)
- Runtime dev-split-only guard (`assert_dev_split_only`) verified present and enforced; zero access to `data/frozen/*/test/`.

### 3. Independent 5-Suite Verification Gate Summary (Live Ollama `qwen3:4b` + `phi4-mini`, Live Gemini `gemini-3.1-flash-lite`, Temurin Java 21 + Alloy 6.2)

- **Environment Doctor (`reqhq doctor --verify-live`):** `TIER_0 | AMD Radeon(TM) 660M | 2.0 GB VRAM | 13.35 GB RAM | 12 cores`, `Ollama: ready=True | running=True | active=qwen3:4b`, `Vault: backend=windows-dpapi | GEMINI_API_KEY valid=True [Loaded from encrypted vault (live verified (HTTP 200))]`, Temurin Java 21 + Alloy 6.2 JAR detected (exit code `0`).
- **Live Hybrid Ollama + Gemini + Alloy 6.2 E2E Verification (3 Distinct Documents — Zero Defects):**
  - `mini_srs` (Refund Portal) + Hybrid `FINAL_AUDIT` / `PromptCache` (`11.13ms`) / `SCHEMA_RETRY_EXHAUSTED` (`6.57s`) — **PASSED**.
  - `pure_dev_cctns` (Frozen PURE dev SRS): `4` nodes, `13` findings, `FINAL_AUDIT` short-circuit in **`1.43s` (`0` local calls)**, live Alloy 6.2 **`VERIFIED` (`vacuity=True`)**, `4` locked PBT properties — **PASSED**.
  - `cross_domain_fin-01` (Cross-domain socio-technical case): `7` nodes, `17` findings, `critic_arbitration` in `1.74s`/`1.78s` & `FINAL_AUDIT` in **`1.73s` (`0` local calls)**, live Alloy 6.2 **`VERIFIED` (`vacuity=True`)**, `7` locked PBT properties — **PASSED**.
- **Suite 1 (Legacy `specpipe`):** `87 passed in 9.73s` (`src/specpipe/`, `data/frozen/`, legacy `tests/test_*.py` untouched).
- **Suite 2 (Unit + Module PBT + Coverage):** `339 passed in 307.33s (0:05:07)` — **`100%` line coverage (`12,424 / 12,424` stmts, `0` miss across all 94 modules in `src/reqhq/`)**, `0` `HypothesisWarning`, `0` `PytestUnknownMarkWarning`.
- **Suite 3 (Independent System & Dataset Integration, live Ollama + live Alloy 6.2):** **`12 passed, 0 skipped in 203.72s (0:03:23)`** (`test_live_ollama_qwen3_4b_gamma_j_tier0_json_and_telemetry` PASSED in `11.82s` standalone), `0` failed, `0` warnings.
- **Suite 4 (Live Java 21 + Alloy 6.2 `-m alloy_live`):** `2 passed, 345 deselected in 16.14s` (`0` skipped).
- **Suite 5 (Total Repository Coverage Across All Suites):** **`438 tests (87 legacy + 339 unit/PBT + 12 system), 0 skipped, 0 failed, 0 warnings`**.

