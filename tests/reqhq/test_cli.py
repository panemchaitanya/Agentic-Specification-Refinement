"""Offline unit tests for the ReqHQ v2 Typer CLI and ADR subcommands."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from typer.testing import CliRunner

SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from reqhq.cli import app
from reqhq.llm.quota_ledger import QuotaLedger
from reqhq.store.event_log import EventKind, SessionEventLog
from reqhq.store.prompt_cache import PromptCache

runner = CliRunner()


def test_cli_init_resume_revise_status_verify_and_export(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    doc_path = tmp_path / "requirements.md"
    doc_path.write_text(
        "# Payment Gateway\n\n"
        "The **Payment Service** (PS) must process transactions within 250ms.\n\n"
        "## Security\n\nAll tokens must expire within 15 minutes.\n",
        encoding="utf-8",
    )

    # 1. reqhq init with source ingestion, strict-local, and hardware tier override
    res_init = runner.invoke(
        app,
        [
            "init",
            "--session-id",
            "sess-demo",
            "--sessions-dir",
            str(sessions_dir),
            "--source",
            str(doc_path),
            "--strict-local",
            "--hardware-tier",
            "1",
        ],
    )
    assert res_init.exit_code == 0, res_init.output
    assert "Initialized session: sess-demo" in res_init.output
    assert "TIER_1" in res_init.output
    assert "Strict local: True" in res_init.output
    assert "Ingested 1 source(s)" in res_init.output

    # Missing source exits cleanly with code 1
    res_missing = runner.invoke(
        app,
        [
            "init",
            "--sessions-dir",
            str(sessions_dir),
            "--source",
            str(tmp_path / "does_not_exist.md"),
        ],
    )
    assert res_missing.exit_code == 1

    # Duplicate session id exits cleanly with code 1
    res_dup = runner.invoke(
        app,
        [
            "init",
            "--session-id",
            "sess-demo",
            "--sessions-dir",
            str(sessions_dir),
            "--hardware-tier",
            "0",
        ],
    )
    assert res_dup.exit_code == 1

    # Append two turns to exercise resume + revise invalidation
    event_log = SessionEventLog(sessions_dir)
    event_log.append_event(
        "sess-demo",
        EventKind.TURN_COMPLETED,
        payload={"summary": "Extracted FR-001"},
        turn_id="turn-1",
    )
    event_log.append_event(
        "sess-demo",
        EventKind.TURN_COMPLETED,
        payload={"summary": "Extracted FR-002"},
        turn_id="turn-2",
    )

    # 2. reqhq resume
    res_resume = runner.invoke(
        app,
        ["resume", "sess-demo", "--sessions-dir", str(sessions_dir)],
    )
    assert res_resume.exit_code == 0, res_resume.output
    assert "Resumed session: sess-demo" in res_resume.output
    assert "Active turns: 2" in res_resume.output

    # 3. reqhq revise turn-1 invalidates turn-2
    res_revise = runner.invoke(
        app,
        [
            "revise",
            "sess-demo",
            "turn-1",
            "--note",
            "Tighten latency SLA to 150ms",
            "--sessions-dir",
            str(sessions_dir),
        ],
    )
    assert res_revise.exit_code == 0, res_revise.output
    assert "Revised turn 'turn-1'" in res_revise.output
    assert "turn-2" in res_revise.output

    # Revising an unknown turn fails cleanly
    res_rev_err = runner.invoke(
        app,
        [
            "revise",
            "sess-demo",
            "turn-999",
            "--note",
            "invalid",
            "--sessions-dir",
            str(sessions_dir),
        ],
    )
    assert res_rev_err.exit_code == 1

    # 4. reqhq status (with canonical ReqHQConfig quota_ledger.db & prompt_cache.db)
    ledger = QuotaLedger(sessions_dir / "quota_ledger.db")
    ledger.record_usage("gemini", prompt_tokens=100, completion_tokens=50)
    ledger.record_rate_limit("groq", retry_after_seconds=45.0, reason="429 test")
    cache = PromptCache(sessions_dir / "prompt_cache.db")
    cache.put("k1", {"ok": True}, model="qwen3:8b")

    res_status = runner.invoke(
        app,
        [
            "status",
            "--session-id",
            "sess-demo",
            "--sessions-dir",
            str(sessions_dir),
            "--hardware-tier",
            "2",
        ],
    )
    assert res_status.exit_code == 0, res_status.output
    assert "TIER_2" in res_status.output
    assert "Quota [gemini]" in res_status.output
    assert "cooldown=" in res_status.output
    assert "Prompt cache:" in res_status.output
    assert "Session 'sess-demo'" in res_status.output

    # 5. reqhq verify
    res_verify = runner.invoke(
        app,
        ["verify", "sess-demo", "--sessions-dir", str(sessions_dir)],
    )
    assert res_verify.exit_code == 0, res_verify.output
    assert "PASSED" in res_verify.output
    assert "Snapshots verified: 1/1" in res_verify.output

    # 6. reqhq export json (to file) and md (to stdout and file)
    json_out = tmp_path / "exports" / "session.json"
    res_exp_json = runner.invoke(
        app,
        [
            "export",
            "sess-demo",
            "--format",
            "json",
            "--output",
            str(json_out),
            "--sessions-dir",
            str(sessions_dir),
        ],
    )
    assert res_exp_json.exit_code == 0, res_exp_json.output
    exported = json.loads(json_out.read_text(encoding="utf-8"))
    assert exported["session_id"] == "sess-demo"
    assert len(exported["snapshots"]) == 1

    res_exp_md = runner.invoke(
        app,
        [
            "export",
            "sess-demo",
            "--format",
            "md",
            "--sessions-dir",
            str(sessions_dir),
        ],
    )
    assert res_exp_md.exit_code == 0, res_exp_md.output
    assert "# ReqHQ Session Export: `sess-demo`" in res_exp_md.output
    assert "requirements.md" in res_exp_md.output

    # Missing session errors across commands
    for cmd in [
        ["resume", "missing-id", "--sessions-dir", str(sessions_dir)],
        ["verify", "missing-id", "--sessions-dir", str(sessions_dir)],
        ["export", "missing-id", "--sessions-dir", str(sessions_dir)],
        ["status", "--session-id", "missing-id", "--sessions-dir", str(sessions_dir)],
    ]:
        res = runner.invoke(app, cmd)
        assert res.exit_code == 1


def test_cli_adr_lifecycle_subcommands(tmp_path: Path) -> None:
    adr_dir = tmp_path / "adr"

    # 1. reqhq adr new
    res_new = runner.invoke(
        app,
        [
            "adr",
            "new",
            "Use Local First Routing",
            "--context",
            "Minimize cloud cost",
            "--decision",
            "Run Ollama locally first",
            "--consequences",
            "Requires local SLM",
            "--adr-dir",
            str(adr_dir),
        ],
    )
    assert res_new.exit_code == 0, res_new.output
    assert "Created ADR-0001" in res_new.output
    assert "[Proposed]" in res_new.output

    # 2. reqhq adr accept
    res_acc = runner.invoke(
        app,
        [
            "adr",
            "accept",
            "0001",
            "--author",
            "architect",
            "--note",
            "Approved",
            "--adr-dir",
            str(adr_dir),
        ],
    )
    assert res_acc.exit_code == 0, res_acc.output
    assert "Accepted ADR-0001" in res_acc.output

    # 3. reqhq adr supersede
    res_sup = runner.invoke(
        app,
        [
            "adr",
            "supersede",
            "0001",
            "Use Hybrid Local Cloud Router",
            "--decision",
            "Add 90 percent quota gate",
            "--adr-dir",
            str(adr_dir),
        ],
    )
    assert res_sup.exit_code == 0, res_sup.output
    assert "Superseded ADR-0001" in res_sup.output
    assert "ADR-0002" in res_sup.output

    # 4. reqhq adr deprecate
    res_dep = runner.invoke(
        app,
        [
            "adr",
            "deprecate",
            "0002",
            "--reason",
            "Replaced by v3 policy",
            "--adr-dir",
            str(adr_dir),
        ],
    )
    assert res_dep.exit_code == 0, res_dep.output
    assert "Deprecated ADR-0002" in res_dep.output

    # 5. reqhq adr sync-index
    res_sync = runner.invoke(
        app,
        ["adr", "sync-index", "--adr-dir", str(adr_dir)],
    )
    assert res_sync.exit_code == 0, res_sync.output
    assert "Synced ADR index" in res_sync.output
    assert (adr_dir / "INDEX.md").is_file()

    # Error handling for nonexistent ADR across subcommands
    for cmd in [
        ["adr", "accept", "9999", "--adr-dir", str(adr_dir)],
        ["adr", "deprecate", "9999", "--adr-dir", str(adr_dir)],
        ["adr", "supersede", "9999", "Replacement", "--adr-dir", str(adr_dir)],
        ["adr", "new", "!!!", "--adr-dir", str(adr_dir)],
    ]:
        res_bad = runner.invoke(app, cmd)
        assert res_bad.exit_code == 1


def test_cli_verify_detects_tampered_snapshot_and_empty_status(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    doc_path = tmp_path / "spec.md"
    doc_path.write_text("# Spec\n\nOriginal content.\n", encoding="utf-8")

    # Status before any sessions or quota db exist
    res_empty = runner.invoke(
        app,
        ["status", "--sessions-dir", str(sessions_dir), "--hardware-tier", "0"],
    )
    assert res_empty.exit_code == 0, res_empty.output
    assert "not yet initialized" in res_empty.output

    # Init session with source, then tamper with snapshot to verify failure path
    res_init = runner.invoke(
        app,
        [
            "init",
            "--session-id",
            "sess-tamper",
            "--sessions-dir",
            str(sessions_dir),
            "--source",
            str(doc_path),
            "--hardware-tier",
            "0",
        ],
    )
    assert res_init.exit_code == 0, res_init.output

    # Export empty-source markdown & stdout json
    res_json_stdout = runner.invoke(
        app,
        ["export", "sess-tamper", "--format", "json", "--sessions-dir", str(sessions_dir)],
    )
    assert res_json_stdout.exit_code == 0, res_json_stdout.output
    assert json.loads(res_json_stdout.output)["session_id"] == "sess-tamper"

    # Tamper with the snapshot file (temporarily make writable on Windows)
    snap_files = [
        p
        for p in (sessions_dir / "sess-tamper" / "snapshots").iterdir()
        if not p.name.endswith(".manifest.json")
    ]
    assert len(snap_files) == 1
    snap_files[0].chmod(0o666)
    snap_files[0].write_text("TAMPERED", encoding="utf-8")

    res_verify_fail = runner.invoke(
        app,
        ["verify", "sess-tamper", "--sessions-dir", str(sessions_dir)],
    )
    assert res_verify_fail.exit_code == 1
    assert "FAILED" in res_verify_fail.output

    # Verify the recorded failed_snapshots payload stores clean original filename
    ev_log = SessionEventLog(sessions_dir)
    verifications = ev_log.resume("sess-tamper")["verifications"]
    assert verifications[-1]["failed_snapshots"] == ["spec.md"]


def test_cli_edge_cases_unsupported_source_empty_export_and_legacy_quota(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions_edge"

    # Unsupported file extension exits cleanly without creating an orphan session
    bad_file = tmp_path / "binary.exe"
    bad_file.write_bytes(b"MZ")
    res_bad = runner.invoke(
        app,
        [
            "init",
            "--session-id",
            "sess-bad",
            "--sessions-dir",
            str(sessions_dir),
            "--source",
            str(bad_file),
            "--hardware-tier",
            "0",
        ],
    )
    assert res_bad.exit_code == 1
    assert "unsupported source file type" in res_bad.output
    assert not (sessions_dir / "sess-bad").exists()

    # Empty-metadata session export in markdown renders empty placeholders cleanly
    ev_log = SessionEventLog(sessions_dir)
    ev_log.create_session(session_id="sess-empty", metadata={})
    ev_log.append_event("sess-empty", EventKind.TURN_COMPLETED, payload={"x": 1}, turn_id="t1")
    ev_log.append_event("sess-empty", EventKind.TURN_COMPLETED, payload={"x": 2}, turn_id="t2")
    ev_log.revise("sess-empty", "t1", {"note": "rev"})

    res_resume = runner.invoke(
        app,
        ["resume", "sess-empty", "--sessions-dir", str(sessions_dir)],
    )
    assert res_resume.exit_code == 0, res_resume.output
    assert "Invalidated turns: t2" in res_resume.output

    res_md = runner.invoke(
        app,
        ["export", "sess-empty", "--format", "md", "--sessions-dir", str(sessions_dir)],
    )
    assert res_md.exit_code == 0, res_md.output
    assert "*(none)*" in res_md.output
    assert "*(no sources ingested)*" in res_md.output

    # Legacy quota.db fallback still works in `reqhq status`
    legacy_ledger = QuotaLedger(sessions_dir / "quota.db")
    legacy_ledger.record_usage("gemini", prompt_tokens=10, completion_tokens=5)
    res_legacy = runner.invoke(
        app,
        ["status", "--sessions-dir", str(sessions_dir), "--hardware-tier", "0"],
    )
    assert res_legacy.exit_code == 0, res_legacy.output
    assert "Quota [gemini]" in res_legacy.output
