"""Typer CLI for ReqHQ v2: session lifecycle, hardware status, verification, export, and ADRs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

import typer

app = typer.Typer(
    name="reqhq",
    help="ReqHQ v2: Local-first agentic requirements engineering CLI.",
    no_args_is_help=True,
)
adr_app = typer.Typer(
    name="adr",
    help="Create and manage Architecture Decision Records (ADRs).",
    no_args_is_help=True,
)
app.add_typer(adr_app, name="adr")


def _render_markdown_export(
    session_state: dict,
    snapshots: list,
) -> str:
    lines: list[str] = [
        f"# ReqHQ Session Export: `{session_state['session_id']}`",
        "",
        "## Session Metadata",
        "",
    ]
    metadata = session_state.get("metadata") or {}
    if metadata:
        for key in sorted(metadata.keys()):
            val = metadata[key]
            if isinstance(val, (dict, list)):
                lines.append(f"- **{key}**: `{json.dumps(val, sort_keys=True)}`")
            else:
                lines.append(f"- **{key}**: {val}")
    else:
        lines.append("- *(none)*")

    lines.extend(["", "## Summary", ""])
    lines.append(f"- **Total events**: {session_state.get('total_events', 0)}")
    lines.append(f"- **Active events**: {len(session_state.get('active_events', []))}")
    lines.append(f"- **Active turns**: {len(session_state.get('active_turns', []))}")
    lines.append(f"- **Invalidated turns**: {len(session_state.get('invalidated_turns', []))}")
    lines.append(f"- **Snapshots**: {len(snapshots)}")

    sources = session_state.get("sources") or []
    lines.extend(["", "## Ingested Sources", ""])
    if sources:
        for src in sources:
            name = src.get("filename") or src.get("source_uri") or "unknown"
            sha = src.get("sha256", "")
            chunks = src.get("chunk_count", 0)
            lines.append(f"- `{name}` (sha256: `{sha[:12]}`, chunks: {chunks})")
    else:
        lines.append("- *(no sources ingested)*")

    lines.extend(["", "## Active Events", ""])
    for ev in session_state.get("active_events", []):
        turn_label = f" [turn `{ev['turn_id']}`]" if ev.get("turn_id") else ""
        lines.append(
            f"- `#{ev['seq']}` **{ev['kind']}**{turn_label} ({ev['timestamp']})"
        )

    lines.append("")
    return "\n".join(lines)


@app.command("init")
def init_command(
    source: Annotated[
        list[Path] | None,
        typer.Option(
            "--source",
            help="Source file(s) (.md, .txt, .pdf, .docx) to snapshot, normalize, and chunk.",
        ),
    ] = None,
    sessions_dir: Annotated[
        Path,
        typer.Option("--sessions-dir", help="Directory for persistent session logs."),
    ] = Path(".reqhq_sessions"),
    strict_local: Annotated[
        bool,
        typer.Option(
            "--strict-local/--allow-cloud",
            help="Disallow all outbound cloud escalation calls.",
        ),
    ] = False,
    hardware_tier: Annotated[
        int | None,
        typer.Option(
            "--hardware-tier",
            min=0,
            max=3,
            help="Override detected hardware tier (0-3).",
        ),
    ] = None,
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", help="Optional explicit session identifier."),
    ] = None,
) -> None:
    """Initialize a new ReqHQ session and optionally ingest source documents."""
    from reqhq.ingestion import (
        SUPPORTED_EXTENSIONS,
        HierarchicalChunker,
        SnapshotStore,
        normalize_file,
    )
    from reqhq.llm.hardware_profiler import HardwareProfiler
    from reqhq.llm.model_registry import get_model_for_tier
    from reqhq.store.event_log import EventKind, SessionEventLog

    source_paths = list(source or [])
    for path in source_paths:
        if not path.is_file():
            typer.echo(f"Error: source file not found: {path}", err=True)
            raise typer.Exit(code=1)
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            typer.echo(
                f"Error: unsupported source file type '{path.suffix}' for {path}",
                err=True,
            )
            raise typer.Exit(code=1)

    profile = HardwareProfiler.detect(tier_override=hardware_tier)
    model_spec = get_model_for_tier(profile.tier)
    event_log = SessionEventLog(sessions_dir)

    metadata = {
        "strict_local": strict_local,
        "hardware_tier": int(profile.tier),
        "hardware_profile": profile.model_dump(mode="json"),
        "local_model": model_spec.model_name,
    }
    try:
        created_id = event_log.create_session(session_id=session_id, metadata=metadata)
    except (FileExistsError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    ingested_count = 0
    total_chunks = 0
    if source_paths:
        snapshot_store = SnapshotStore(sessions_dir)
        chunker = HierarchicalChunker()
        for path in source_paths:
            raw_bytes = path.read_bytes()
            snap = snapshot_store.save_snapshot(
                session_id=created_id,
                filename=path.name,
                content=raw_bytes,
                source_uri=str(path.resolve()),
                source_type="local",
            )
            doc = normalize_file(path, source_sha256=snap.sha256)
            chunk_result = chunker.chunk(doc)
            event_log.append_event(
                created_id,
                EventKind.SOURCE_INGESTED,
                payload={
                    "filename": path.name,
                    "source_uri": str(path.resolve()),
                    "sha256": doc.source_sha256,
                    "parser_name": doc.parser_name,
                    "char_count": doc.char_count,
                    "chunk_count": chunk_result.chunk_count,
                    "truncation_ratio": chunk_result.truncation_ratio,
                },
            )
            ingested_count += 1
            total_chunks += chunk_result.chunk_count

    typer.echo(f"Initialized session: {created_id}")
    typer.echo(
        f"Hardware tier: TIER_{int(profile.tier)} ({profile.gpu_name}, {profile.vram_gb} GB VRAM)"
    )
    typer.echo(f"Local model: {model_spec.model_name} ({model_spec.quantization})")
    typer.echo(f"Strict local: {strict_local}")
    if ingested_count:
        typer.echo(f"Ingested {ingested_count} source(s) into {total_chunks} chunk(s).")


@app.command("resume")
def resume_command(
    session_id: Annotated[str, typer.Argument(help="Session ID to resume.")],
    sessions_dir: Annotated[
        Path,
        typer.Option("--sessions-dir", help="Directory for persistent session logs."),
    ] = Path(".reqhq_sessions"),
) -> None:
    """Resume an existing ReqHQ session from its append-only event log."""
    from reqhq.store.event_log import SessionEventLog

    event_log = SessionEventLog(sessions_dir)
    try:
        state = event_log.resume(session_id)
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Resumed session: {state['session_id']}")
    typer.echo(
        f"Events: {len(state['active_events'])} active / {state['total_events']} total"
    )
    typer.echo(f"Active turns: {len(state['active_turns'])}")
    if state["invalidated_turns"]:
        typer.echo(f"Invalidated turns: {', '.join(state['invalidated_turns'])}")
    if state["last_turn_id"]:
        typer.echo(f"Latest turn: {state['last_turn_id']}")
    typer.echo(f"Sources ingested: {len(state['sources'])}")


@app.command("revise")
def revise_command(
    session_id: Annotated[str, typer.Argument(help="Session ID containing the turn.")],
    turn_id: Annotated[str, typer.Argument(help="Active turn ID to revise.")],
    note: Annotated[
        str,
        typer.Option("--note", help="Revision note or updated turn rationale."),
    ],
    sessions_dir: Annotated[
        Path,
        typer.Option("--sessions-dir", help="Directory for persistent session logs."),
    ] = Path(".reqhq_sessions"),
) -> None:
    """Revise an active turn and invalidate downstream turns without mutating history."""
    from reqhq.store.event_log import SessionEventLog

    event_log = SessionEventLog(sessions_dir)
    try:
        result = event_log.revise(session_id, turn_id, {"note": note})
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Revised turn '{turn_id}' in session '{session_id}'.")
    typer.echo(f"Note: {note}")
    invalidated = result.get("invalidated_turn_ids") or []
    typer.echo(
        f"Invalidated downstream turns ({len(invalidated)}): "
        + (", ".join(invalidated) if invalidated else "none")
    )


@app.command("status")
def status_command(
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", help="Optional session ID to inspect in detail."),
    ] = None,
    sessions_dir: Annotated[
        Path,
        typer.Option("--sessions-dir", help="Directory for persistent session logs."),
    ] = Path(".reqhq_sessions"),
    hardware_tier: Annotated[
        int | None,
        typer.Option(
            "--hardware-tier",
            min=0,
            max=3,
            help="Override detected hardware tier (0-3).",
        ),
    ] = None,
) -> None:
    """Show hardware profile, local model tier, cloud quota ledger, and session status."""
    from reqhq.config import ReqHQConfig
    from reqhq.llm.hardware_profiler import HardwareProfiler
    from reqhq.llm.model_registry import get_model_for_tier
    from reqhq.llm.quota_ledger import QuotaLedger
    from reqhq.store.event_log import SessionEventLog
    from reqhq.store.prompt_cache import PromptCache

    cfg = ReqHQConfig(sessions_dir=sessions_dir)
    profile = HardwareProfiler.detect(tier_override=hardware_tier)
    model_spec = get_model_for_tier(profile.tier)
    event_log = SessionEventLog(sessions_dir)
    sessions = event_log.list_sessions()

    typer.echo(
        f"Hardware: TIER_{int(profile.tier)} | {profile.gpu_name} | "
        f"{profile.vram_gb} GB VRAM | {profile.ram_gb} GB RAM | {profile.cpu_cores} cores"
    )
    typer.echo(
        f"Recommended local model: {model_spec.model_name} ({model_spec.quantization})"
    )

    quota_db = cfg.resolved_quota_db_path()
    if not quota_db.exists():
        legacy_quota_db = sessions_dir / "quota.db"
        if legacy_quota_db.exists():
            quota_db = legacy_quota_db

    if quota_db.exists():
        ledger = QuotaLedger(quota_db)
        qstatus = ledger.get_status()
        for pname, pdata in qstatus.items():
            cooldown_suffix = (
                f" | cooldown={pdata['cooldown_remaining_seconds']}s"
                if pdata.get("cooldown_active")
                else ""
            )
            typer.echo(
                f"Quota [{pname}]: RPM {pdata['rpm_used']}/{pdata['rpm_limit']} | "
                f"RPD {pdata['rpd_used']}/{pdata['rpd_limit']} | "
                f"TPM {pdata['tpm_used']}/{pdata['tpm_limit']} | "
                f"available={pdata['available']}{cooldown_suffix}"
            )
    else:
        typer.echo("Quota ledger: not yet initialized (0 cloud calls recorded).")

    cache_db = cfg.resolved_cache_db_path()
    if cache_db.exists():
        cstats = PromptCache(cache_db).stats()
        typer.echo(
            f"Prompt cache: {cstats['entries']} entries | "
            f"{cstats['hits']} hits | {cstats['misses']} misses"
        )

    typer.echo(f"Sessions ({len(sessions)}): {', '.join(sessions) if sessions else 'none'}")

    if session_id is not None:
        try:
            state = event_log.resume(session_id)
        except (FileNotFoundError, ValueError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1)
        typer.echo(
            f"Session '{session_id}': {len(state['active_events'])} active events, "
            f"{len(state['active_turns'])} active turns, {len(state['sources'])} sources"
        )


@app.command("verify")
def verify_command(
    session_id: Annotated[str, typer.Argument(help="Session ID to verify.")],
    sessions_dir: Annotated[
        Path,
        typer.Option("--sessions-dir", help="Directory for persistent session logs."),
    ] = Path(".reqhq_sessions"),
) -> None:
    """Verify session event-log parity and immutable snapshot SHA-256 integrity."""
    from reqhq.ingestion import SnapshotStore
    from reqhq.store.event_log import EventKind, SessionEventLog

    event_log = SessionEventLog(sessions_dir)
    try:
        state = event_log.resume(session_id)
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    jsonl_ok = event_log.verify_jsonl_parity(session_id)
    snapshot_store = SnapshotStore(sessions_dir)
    snapshots = snapshot_store.list_snapshots(session_id)
    failed_snapshots: list[str] = []
    for snap in snapshots:
        if not snapshot_store.verify_snapshot(snap):
            failed_snapshots.append(
                getattr(snap, "original_filename", None)
                or getattr(snap, "snapshot_id", str(snap))
            )

    passed = jsonl_ok and not failed_snapshots and state["total_events"] > 0
    event_log.append_event(
        session_id,
        EventKind.VERIFICATION_RECORDED,
        payload={
            "passed": passed,
            "jsonl_parity": jsonl_ok,
            "snapshots_checked": len(snapshots),
            "failed_snapshots": failed_snapshots,
            "active_event_count": len(state["active_events"]),
        },
    )

    typer.echo(f"Verification for session '{session_id}': {'PASSED' if passed else 'FAILED'}")
    typer.echo(f"JSONL/SQLite parity: {'ok' if jsonl_ok else 'mismatch'}")
    typer.echo(
        f"Snapshots verified: {len(snapshots) - len(failed_snapshots)}/{len(snapshots)}"
    )
    if not passed:
        raise typer.Exit(code=1)


@app.command("export")
def export_command(
    session_id: Annotated[str, typer.Argument(help="Session ID to export.")],
    format: Annotated[
        Literal["json", "md"],
        typer.Option("--format", help="Export format: json or md."),
    ] = "json",
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Optional output file path."),
    ] = None,
    sessions_dir: Annotated[
        Path,
        typer.Option("--sessions-dir", help="Directory for persistent session logs."),
    ] = Path(".reqhq_sessions"),
) -> None:
    """Export a ReqHQ session as deterministic JSON or Markdown."""
    from reqhq.ingestion import SnapshotStore
    from reqhq.store.event_log import EventKind, SessionEventLog

    event_log = SessionEventLog(sessions_dir)
    try:
        state = event_log.resume(session_id)
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    snapshot_store = SnapshotStore(sessions_dir)
    snapshots = snapshot_store.list_snapshots(session_id)

    if format == "json":
        payload = {
            **state,
            "snapshots": [
                s.model_dump(mode="json") if hasattr(s, "model_dump") else str(s)
                for s in snapshots
            ],
        }
        rendered = json.dumps(payload, indent=2, sort_keys=True)
    else:
        rendered = _render_markdown_export(state, snapshots)

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        typer.echo(f"Exported session '{session_id}' ({format}) to {output}")
    else:
        typer.echo(rendered)

    event_log.append_event(
        session_id,
        EventKind.EXPORT_COMPLETED,
        payload={
            "format": format,
            "output": str(output) if output is not None else "stdout",
        },
    )


@adr_app.command("new")
def adr_new_command(
    title: Annotated[str, typer.Argument(help="Short title for the new ADR.")],
    context: Annotated[str, typer.Option("--context", help="Decision context.")] = "",
    decision: Annotated[str, typer.Option("--decision", help="Architectural decision.")] = "",
    consequences: Annotated[
        str, typer.Option("--consequences", help="Trade-offs and consequences.")
    ] = "",
    pr_branch: Annotated[str, typer.Option("--pr-branch", help="Associated PR branch.")] = "",
    supersedes: Annotated[
        str | None, typer.Option("--supersedes", help="ADR number this supersedes.")
    ] = None,
    author: Annotated[str, typer.Option("--author", help="Author identity.")] = "cli",
    adr_dir: Annotated[
        Path, typer.Option("--adr-dir", help="Directory storing ADR Markdown files.")
    ] = Path("docs/adr"),
) -> None:
    """Create a new Proposed ADR and update the ADR index."""
    from reqhq.adr.manager import ADRManager

    manager = ADRManager(adr_dir)
    try:
        record = manager.create(
            title,
            context=context,
            decision=decision,
            consequences=consequences,
            pr_branch=pr_branch,
            supersedes=supersedes,
            author=author,
        )
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Created ADR-{record.number}: {record.title} [{record.status}]")
    typer.echo(f"Path: {record.path}")


@adr_app.command("accept")
def adr_accept_command(
    adr_id: Annotated[str, typer.Argument(help="ADR number or identifier to accept.")],
    author: Annotated[str, typer.Option("--author", help="Reviewer/approver identity.")] = "cli",
    note: Annotated[str, typer.Option("--note", help="Optional acceptance note.")] = "",
    adr_dir: Annotated[
        Path, typer.Option("--adr-dir", help="Directory storing ADR Markdown files.")
    ] = Path("docs/adr"),
) -> None:
    """Transition an ADR from Proposed to Accepted."""
    from reqhq.adr.manager import ADRManager

    manager = ADRManager(adr_dir)
    try:
        record = manager.accept(adr_id, author=author, note=note)
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Accepted ADR-{record.number}: {record.title} [{record.status}]")


@adr_app.command("deprecate")
def adr_deprecate_command(
    adr_id: Annotated[str, typer.Argument(help="ADR number or identifier to deprecate.")],
    reason: Annotated[str, typer.Option("--reason", help="Reason for deprecation.")] = "",
    author: Annotated[str, typer.Option("--author", help="Author identity.")] = "cli",
    adr_dir: Annotated[
        Path, typer.Option("--adr-dir", help="Directory storing ADR Markdown files.")
    ] = Path("docs/adr"),
) -> None:
    """Deprecate an existing ADR and archive it cleanly."""
    from reqhq.adr.manager import ADRManager

    manager = ADRManager(adr_dir)
    try:
        record = manager.deprecate(adr_id, reason=reason, author=author)
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Deprecated ADR-{record.number}: {record.title} [{record.status}]")


@adr_app.command("supersede")
def adr_supersede_command(
    old_adr_id: Annotated[str, typer.Argument(help="ADR number to supersede.")],
    new_title: Annotated[str, typer.Argument(help="Title for the replacement ADR.")],
    context: Annotated[str, typer.Option("--context", help="Decision context.")] = "",
    decision: Annotated[str, typer.Option("--decision", help="Architectural decision.")] = "",
    consequences: Annotated[
        str, typer.Option("--consequences", help="Trade-offs and consequences.")
    ] = "",
    pr_branch: Annotated[str, typer.Option("--pr-branch", help="Associated PR branch.")] = "",
    author: Annotated[str, typer.Option("--author", help="Author identity.")] = "cli",
    adr_dir: Annotated[
        Path, typer.Option("--adr-dir", help="Directory storing ADR Markdown files.")
    ] = Path("docs/adr"),
) -> None:
    """Supersede an existing ADR with a new ADR and cross-link both records."""
    from reqhq.adr.manager import ADRManager

    manager = ADRManager(adr_dir)
    try:
        old_rec, new_rec = manager.supersede(
            old_adr_id,
            new_title,
            context=context,
            decision=decision,
            consequences=consequences,
            pr_branch=pr_branch,
            author=author,
        )
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(
        f"Superseded ADR-{old_rec.number} ({old_rec.status}) with "
        f"ADR-{new_rec.number}: {new_rec.title} ({new_rec.status})"
    )


@adr_app.command("sync-index")
def adr_sync_index_command(
    adr_dir: Annotated[
        Path, typer.Option("--adr-dir", help="Directory storing ADR Markdown files.")
    ] = Path("docs/adr"),
) -> None:
    """Rebuild docs/adr/INDEX.md from ADR files on disk."""
    from reqhq.adr.manager import ADRManager

    manager = ADRManager(adr_dir)
    try:
        counts = manager.sync_index()
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    typer.echo(f"Synced ADR index at {adr_dir}: {summary}")


def main() -> None:
    """Entry point registered for console script `reqhq`."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
