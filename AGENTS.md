# ReqHQ v2 Engineering & Verification Standards

Project-specific operating rules and lessons learned from Phases 1–3 of `reqhq` v2 (`C:\Projects\Agentic-Specification-Refinement`, Windows 11, Python 3.15.0).

## 1. Runtime, Environment & Live Tool Gates
- **Explicit Interpreter:** Always run Python, `pip`, and `pytest` via `.venv/Scripts/python.exe` (Python 3.15.0)—never bare `python` or global `pytest`.
- **Live Alloy 6.2 Gate by Default:** On machines with Temurin Java 21 and the Alloy 6.2 JAR installed, always export `REQHQ_ALLOY_JAR_PATH="C:/tools/alloy/org.alloytools.alloy.dist.jar"` (and ensure Temurin `java` is on `PATH`) before running `pytest` so `@pytest.mark.alloy_live` executes with **0 skipped**. Keep offline unit tests fast and self-contained via `MockAlloyAnalyzer` for zero-dependency CI.
- **Alloy 6.2 CLI Output Format:** Real Alloy 6.2 `exec` prints its summary table to **`stderr`** (not `stdout`), reports passing `check` commands as `UNSAT`, failing `check` counterexamples as `00. check <name> ... SAT`, and non-vacuous `run __vacuity_witness_*` checks as `SAT`. Always verify parsers against live JAR output in addition to mocks.
- **Python 3.15 Wheel Safety:** Check `--dry-run --only-binary=:all:` before adding dependencies; avoid C/Rust source builds or POSIX-only packages.

## 2. Windows 11 & Cross-Platform Discipline
- **`.gitattributes` Before LF-Strict Tests:** Git for Windows (`core.autocrlf = true`) converts `\n` to `\r\n` on checkout. Any directory whose tests assert LF-only line endings (`b"\r\n" not in raw`, e.g., `docs/adr/**`, `data/benchmarks/**/*.json`) MUST have a matching `text eol=lf` rule in `.gitattributes` from day one.
- **Windows Temp-Dir Handle Safety:** Always pass `ignore_cleanup_errors=True` to `tempfile.TemporaryDirectory(...)` whenever spawning subprocesses whose `cwd` is inside the temp directory (prevents transient `PermissionError: [WinError 32]` during Windows teardown).
- **No POSIX-Only Assumptions:** Avoid hard dependencies on POSIX-only primitives (`os.fork()`, `mutmut` without fallback). Put a `Protocol` (such as `MutationRunner`) in front of Windows-native implementations (`ASTMutantEngine`) and POSIX adapters (`MutmutRunner`).

## 3. 100% Coverage & Strict Hypothesis PBT Standards
- **Zero `# pragma: no cover` Shortcuts:** Aim for **100.00% line coverage** across `src/reqhq/` without papering over reachable logic. Inspect every missed branch to distinguish real bugs or dead code from missing tests.
- **Mandatory Internal Hypothesis PBT:** Every new module/phase must ship deterministic Hypothesis property-based tests (`@given`, `@settings(derandomize=True, max_examples=50)` or `RuleBasedStateMachine`) alongside example unit tests.
- **Strict Hypothesis Warnings & Bounds:** Run `pytest tests/reqhq` with `-W error::hypothesis.errors.HypothesisWarning`. Audit strategy bounds carefully:
  - Always set `allow_nan=False, allow_infinity=False` on float strategies unless testing NaN/Inf explicitly.
  - Use `not keyword.iskeyword(s)` rather than partial keyword lists for generated Python identifiers.
  - Set `deadline=None` on PBTs that perform filesystem/NTFS temp-dir I/O or subprocess calls.
  - Pin boundary edge cases (`0`, `1`, empty inputs, exact endpoints, `-0.0` normalization) explicitly via `@example(...)` or wide strategy ranges.

## 4. Preservation, Namespace Separation & Forward-Compatible Assertions
- **Legacy Isolation:** Never modify `src/specpipe/`, `data/frozen/`, or legacy `tests/test_*.py` (all 87 legacy tests must remain untouched and passing).
- **ADR Namespace Separation:** Keep repository governance Nygard ADRs (`docs/adr/0001`–`00XX`, LF-only, managed by `ADRManager`) strictly separated from generated target-system Routing/Assumption ADRs (`<export_dir>/ADRs/ADR-RXXX.md`, `ADR-AXXX.md`). Enforce `docs/adr/` write-refusal guards on all export/handoff/PBT CLI commands.
- **Monotonic Assertions on Append-Only Artifacts:** Never hardcode exact total counts (`len(adrs) == N`, `len(audit_log) == 2*N`) in tests for append-only artifacts (`docs/adr/`, `audit_log.jsonl`, `INDEX.md`). Use monotonic/prefix assertions (`>= N`, `numbers[:N] == 0001..000N`) so future phases never break earlier test suites.

## 5. Multi-Agent Swarm & Check-In Verification Discipline
- **Independent Post-Swarm Deliverable Audit:** Never trust worker self-reports. Physically inspect every created/edited file for completeness, zero `TODO`/`NotImplementedError` stubs, LF endings, doc-to-code filename/symbol parity (ADRs vs. live modules), and re-run the full verification suite (legacy + `tests/reqhq` + coverage + live Alloy) from scratch after the final edit.
- **Stacked PR Check-In Discipline:** When splitting work into disjoint ADR + implementation PRs targeting an integration branch, either merge each PR sequentially before cutting the next (so per-PR local `pytest` passes standalone), or batch-open all disjoint PRs upfront and run the full verification gate once all PRs are merged into the integration branch.
