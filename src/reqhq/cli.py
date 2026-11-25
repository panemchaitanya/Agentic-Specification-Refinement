"""Typer CLI for ReqHQ v2: session lifecycle, elicitation, formal verification, export, and ADRs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Any, Literal

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
benchmark_app = typer.Typer(
    name="benchmark",
    help="Run cross-domain / gap-injection benchmarks and compute inter-rater kappa.",
    no_args_is_help=True,
)
eval_app = typer.Typer(
    name="eval",
    help="Run Phase 4 Layers 1-4 evaluation, statistical analysis, reports, and release verification.",
    no_args_is_help=True,
)
app.add_typer(adr_app, name="adr")
app.add_typer(benchmark_app, name="benchmark")
app.add_typer(eval_app, name="eval")


def _build_router(
    *,
    sessions_dir: Path,
    strict_local: bool = False,
    local_model: str = "qwen3:4b",
    mock: bool = False,
) -> Any:
    """Construct a local-first ProviderRouter (offline MockProvider when mock=True)."""
    from reqhq.config import ReqHQConfig
    from reqhq.llm import MockProvider, OllamaLocalProvider, ProviderRouter

    if mock:
        return ProviderRouter(
            local_provider=MockProvider(default_response=""),
            strict_local=strict_local,
            enable_cache=False,
        )

    cfg = ReqHQConfig.from_env()
    cfg_sessions = ReqHQConfig(
        sessions_dir=sessions_dir,
        strict_local=strict_local or cfg.strict_local,
        ollama_base_url=cfg.ollama_base_url,
        alloy_jar_path=cfg.alloy_jar_path,
        alloy_compat=cfg.alloy_compat,
    )
    return ProviderRouter(
        local_provider=OllamaLocalProvider(
            base_url=cfg_sessions.ollama_base_url,
            default_model=local_model,
        ),
        strict_local=cfg_sessions.strict_local,
    )


def _build_alloy_analyzer(*, mock_alloy: bool = False) -> Any:
    """Return MockAlloyAnalyzer when mock_alloy=True, else HeadlessAlloyRunner."""
    from reqhq.alloy import HeadlessAlloyRunner, MockAlloyAnalyzer
    from reqhq.config import ReqHQConfig

    if mock_alloy:
        return MockAlloyAnalyzer()
    cfg = ReqHQConfig.from_env()
    return HeadlessAlloyRunner(jar_path=cfg.alloy_jar_path)


def _parse_answers_file(answers_file: Path) -> list[list[dict[str, str]]]:
    """Load scripted turn answers from a JSON file into per-turn answer batches."""
    raw = json.loads(answers_file.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        if "turns" in raw and isinstance(raw["turns"], list):
            raw = raw["turns"]
        elif "answers" in raw and isinstance(raw["answers"], list):
            raw = [raw["answers"]]
        else:
            return [[{"question_id": str(k), "answer": str(v)} for k, v in raw.items()]]

    if not isinstance(raw, list):
        raise ValueError("Answers file must contain a JSON list or object")

    if not raw:
        return []

    # List of turns (each turn is a list or dict) vs single flat list of answer objects/strings.
    if all(isinstance(item, (list, dict)) and not ("question_id" in item or "answer" in item) for item in raw):
        batches: list[list[dict[str, str]]] = []
        for turn in raw:
            if isinstance(turn, dict):
                batches.append([{"question_id": str(k), "answer": str(v)} for k, v in turn.items()])
            else:
                batch: list[dict[str, str]] = []
                for entry in turn:
                    if isinstance(entry, str):
                        batch.append({"answer": entry})
                    elif isinstance(entry, dict):
                        batch.append({str(k): str(v) for k, v in entry.items()})
                batches.append(batch)
        return batches

    single_batch: list[dict[str, str]] = []
    for entry in raw:
        if isinstance(entry, str):
            single_batch.append({"answer": entry})
        elif isinstance(entry, dict):
            single_batch.append({str(k): str(v) for k, v in entry.items()})
    return [single_batch]


def _bind_answers_to_questions(
    raw_batch: list[dict[str, str]],
    pending_questions: list[Any],
) -> list[dict[str, str]]:
    """Resolve positional answers against pending questions while preserving explicit IDs."""
    bound: list[dict[str, str]] = []
    for idx, entry in enumerate(raw_batch):
        answer_text = str(entry.get("answer", "")).strip()
        if not answer_text:
            continue
        qid = str(entry.get("question_id", "")).strip()
        if not qid and idx < len(pending_questions):
            qid = pending_questions[idx].question_id
        if qid:
            bound.append({"question_id": qid, "answer": answer_text})
    return bound


def _load_or_seed_elicitation(
    *,
    session_id: str,
    sessions_dir: Path,
    session_state: dict[str, Any],
    engine: Any,
) -> Any:
    """Load persisted ElicitationState or initialize it from ingested chunks/snapshots/prompt."""
    from reqhq.ingestion import (
        ChunkingResult,
        GlobalGlossary,
        HierarchicalChunker,
        SnapshotStore,
        normalize_file,
    )

    try:
        return engine.load_state(session_id)
    except FileNotFoundError:
        pass

    chunks_file = sessions_dir / session_id / "chunking_results.json"
    chunk_results: list[ChunkingResult] = []
    if chunks_file.is_file():
        loaded = json.loads(chunks_file.read_text(encoding="utf-8"))
        chunk_results = [ChunkingResult.model_validate(item) for item in loaded]
    else:
        snapshot_store = SnapshotStore(sessions_dir)
        snapshots = snapshot_store.list_snapshots(session_id)
        if snapshots:
            chunker = HierarchicalChunker()
            for snap in snapshots:
                if snap.snapshot_path.is_file():
                    doc = normalize_file(snap.snapshot_path, source_sha256=snap.sha256)
                    chunk_results.append(chunker.chunk(doc))

    if chunk_results:
        if len(chunk_results) == 1:
            return engine.start_from_chunks(
                session_id,
                chunk_results[0],
                source_uri=str(chunk_results[0].source_path),
            )
        merged_glossary = GlobalGlossary()
        merged_chunks = []
        total_chars = 0
        covered_chars = 0
        for cr in chunk_results:
            merged_glossary = merged_glossary.merge(cr.glossary)
            total_chars += cr.total_chars
            covered_chars += cr.covered_chars
            for chunk in cr.chunks:
                merged_chunks.append(
                    chunk.model_copy(
                        update={
                            "chunk_index": len(merged_chunks),
                            "chunk_id": f"chunk-{len(merged_chunks):04d}",
                        }
                    )
                )
        combined = ChunkingResult(
            source_sha256=chunk_results[0].source_sha256,
            source_path=chunk_results[0].source_path,
            total_chars=total_chars,
            covered_chars=covered_chars,
            truncation_ratio=0.0 if total_chars == 0 else (total_chars - covered_chars) / total_chars,
            chunk_count=len(merged_chunks),
            max_chunk_chars=chunk_results[0].max_chunk_chars,
            overlap_chars=chunk_results[0].overlap_chars,
            glossary=merged_glossary,
            chunks=merged_chunks,
        )
        return engine.start_from_chunks(session_id, combined, source_uri=str(combined.source_path))

    prompt = str((session_state.get("metadata") or {}).get("prompt") or "").strip()
    if prompt:
        return engine.start_from_prompt(session_id, prompt, source_uri="cli://prompt")

    raise ValueError(
        f"Session '{session_id}' has no prompt or ingested source documents. "
        "Re-initialize with --prompt or --source."
    )


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
    prompt: Annotated[
        str | None,
        typer.Option(
            "--prompt",
            help="Optional 1-liner or short problem prompt to seed Stage 3 elicitation.",
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
    """Initialize a new ReqHQ session and optionally ingest source documents or a prompt."""
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

    cleaned_prompt = prompt.strip() if prompt is not None else None
    if prompt is not None and not cleaned_prompt:
        typer.echo("Error: --prompt must not be empty.", err=True)
        raise typer.Exit(code=1)

    profile = HardwareProfiler.detect(tier_override=hardware_tier)
    model_spec = get_model_for_tier(profile.tier)
    event_log = SessionEventLog(sessions_dir)

    metadata: dict[str, Any] = {
        "strict_local": strict_local,
        "hardware_tier": int(profile.tier),
        "hardware_profile": profile.model_dump(mode="json"),
        "local_model": model_spec.model_name,
    }
    if cleaned_prompt:
        metadata["prompt"] = cleaned_prompt

    try:
        created_id = event_log.create_session(session_id=session_id, metadata=metadata)
    except (FileExistsError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    if cleaned_prompt:
        prompt_sha = hashlib.sha256(cleaned_prompt.encode("utf-8")).hexdigest()
        event_log.append_event(
            created_id,
            EventKind.SOURCE_INGESTED,
            payload={
                "filename": "cli_prompt.txt",
                "source_uri": "cli://prompt",
                "sha256": prompt_sha,
                "parser_name": "cli_prompt",
                "char_count": len(cleaned_prompt),
                "chunk_count": 1,
                "truncation_ratio": 0.0,
                "prompt": cleaned_prompt,
            },
        )

    ingested_count = 0
    total_chunks = 0
    chunk_dumps: list[dict[str, Any]] = []
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
            chunk_dumps.append(chunk_result.model_dump(mode="json"))
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

        chunks_file = sessions_dir / created_id / "chunking_results.json"
        chunks_file.write_text(json.dumps(chunk_dumps, indent=2), encoding="utf-8")

    typer.echo(f"Initialized session: {created_id}")
    typer.echo(
        f"Hardware tier: TIER_{int(profile.tier)} ({profile.gpu_name}, {profile.vram_gb} GB VRAM)"
    )
    typer.echo(f"Local model: {model_spec.model_name} ({model_spec.quantization})")
    typer.echo(f"Strict local: {strict_local}")
    if cleaned_prompt:
        typer.echo(f"Prompt ingested ({len(cleaned_prompt)} chars).")
    if ingested_count:
        typer.echo(f"Ingested {ingested_count} source(s) into {total_chunks} chunk(s).")


@app.command("elicit")
def elicit_command(
    session_id: Annotated[str, typer.Argument(help="Session ID to run elicitation on.")],
    answers_file: Annotated[
        Path | None,
        typer.Option(
            "--answers-file",
            help="Optional JSON file with scripted turn answers for non-interactive runs.",
        ),
    ] = None,
    max_turns: Annotated[
        int,
        typer.Option("--max-turns", min=1, max=20, help="Maximum elicitation turns to execute."),
    ] = 3,
    mock: Annotated[
        bool,
        typer.Option("--mock", help="Use offline MockProvider for deterministic local runs."),
    ] = False,
    sessions_dir: Annotated[
        Path,
        typer.Option("--sessions-dir", help="Directory for persistent session logs."),
    ] = Path(".reqhq_sessions"),
) -> None:
    """Run Stage 3 iterative elicitation with 5 sequential local critics and delta convergence."""
    from reqhq.elicitation import ElicitationEngine
    from reqhq.store.event_log import SessionEventLog

    event_log = SessionEventLog(sessions_dir)
    try:
        session_state = event_log.resume(session_id)
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    scripted_batches: list[list[dict[str, str]]] | None = None
    if answers_file is not None:
        if not answers_file.is_file():
            typer.echo(f"Error: answers file not found: {answers_file}", err=True)
            raise typer.Exit(code=1)
        try:
            scripted_batches = _parse_answers_file(answers_file)
        except Exception as exc:
            typer.echo(f"Error: invalid answers file: {exc}", err=True)
            raise typer.Exit(code=1)

    meta = session_state.get("metadata") or {}
    router = _build_router(
        sessions_dir=sessions_dir,
        strict_local=bool(meta.get("strict_local", False)),
        local_model=str(meta.get("local_model", "qwen3:4b")),
        mock=mock,
    )
    engine = ElicitationEngine(router, event_log)

    try:
        elicit_state = _load_or_seed_elicitation(
            session_id=session_id,
            sessions_dir=sessions_dir,
            session_state=session_state,
            engine=engine,
        )
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    turns_run = 0
    if elicit_state.turn_count == 0:
        elicit_state = engine.run_turn(elicit_state)
        turns_run += 1

    batch_idx = 0
    while not elicit_state.converged and turns_run < max_turns and elicit_state.pending_questions:
        if scripted_batches is not None:
            if batch_idx >= len(scripted_batches):
                break
            bound_answers = _bind_answers_to_questions(
                scripted_batches[batch_idx],
                elicit_state.pending_questions,
            )
            batch_idx += 1
        else:
            bound_answers = []
            typer.echo(
                f"Turn {elicit_state.turn_count + 1} — "
                f"{len(elicit_state.pending_questions)} question(s) (leave blank to stop):"
            )
            for q in elicit_state.pending_questions:
                typer.echo(f"  [{q.severity.value.upper()}] ({q.question_id}) {q.prompt}")
                response_text = typer.prompt("Answer", default="", show_default=False).strip()
                if response_text.lower() in {"", "quit", "exit"}:
                    break
                bound_answers.append({"question_id": q.question_id, "answer": response_text})

        if not bound_answers:
            break
        elicit_state = engine.run_turn(elicit_state, answers=bound_answers)
        turns_run += 1

    typer.echo(
        f"Elicitation session '{session_id}': turns={elicit_state.turn_count} | "
        f"requirements={len(elicit_state.graph.nodes)} | "
        f"findings={len(elicit_state.findings)} | "
        f"pending_questions={len(elicit_state.pending_questions)} | "
        f"converged={elicit_state.converged}"
    )
    for q in elicit_state.pending_questions:
        typer.echo(f"  Pending [{q.severity.value}] {q.question_id}: {q.prompt}")


@app.command("finalize")
def finalize_command(
    session_id: Annotated[str, typer.Argument(help="Session ID to finalize.")],
    accept_defaults: Annotated[
        bool,
        typer.Option(
            "--accept-defaults",
            help="Convert remaining BLOCKER/MAJOR findings into unconfirmed Assumption ADRs.",
        ),
    ] = False,
    mock: Annotated[
        bool,
        typer.Option("--mock", help="Use offline MockProvider when constructing engine."),
    ] = False,
    sessions_dir: Annotated[
        Path,
        typer.Option("--sessions-dir", help="Directory for persistent session logs."),
    ] = Path(".reqhq_sessions"),
) -> None:
    """Finalize Stage 3 elicitation once converged, or record explicit defaults as assumptions."""
    from reqhq.elicitation import ConvergenceGateError, ElicitationEngine
    from reqhq.store.event_log import SessionEventLog

    event_log = SessionEventLog(sessions_dir)
    try:
        session_state = event_log.resume(session_id)
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    meta = session_state.get("metadata") or {}
    router = _build_router(
        sessions_dir=sessions_dir,
        strict_local=bool(meta.get("strict_local", False)),
        local_model=str(meta.get("local_model", "qwen3:4b")),
        mock=mock,
    )
    engine = ElicitationEngine(router, event_log)

    try:
        elicit_state = engine.load_state(session_id)
        finalized = engine.finalize(elicit_state, accept_defaults=accept_defaults)
    except (FileNotFoundError, ConvergenceGateError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(
        f"Finalized session '{session_id}': converged={finalized.converged} | "
        f"accepted_defaults={finalized.accepted_defaults} | "
        f"assumptions={len(finalized.assumptions)}"
    )


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
    answers_file: Annotated[
        Path | None,
        typer.Option(
            "--answers-file",
            help="Optional JSON file with replacement answers for Stage 3 delta re-evaluation.",
        ),
    ] = None,
    mock: Annotated[
        bool,
        typer.Option("--mock", help="Use offline MockProvider for Stage 3 delta re-evaluation."),
    ] = False,
    sessions_dir: Annotated[
        Path,
        typer.Option("--sessions-dir", help="Directory for persistent session logs."),
    ] = Path(".reqhq_sessions"),
) -> None:
    """Revise an active turn and invalidate downstream turns without mutating history."""
    from reqhq.store.event_log import SessionEventLog

    event_log = SessionEventLog(sessions_dir)
    elicit_state_path = sessions_dir / session_id / "elicitation_state.json"

    if elicit_state_path.is_file():
        from reqhq.elicitation import ElicitationEngine

        try:
            session_state = event_log.resume(session_id)
            meta = session_state.get("metadata") or {}
            router = _build_router(
                sessions_dir=sessions_dir,
                strict_local=bool(meta.get("strict_local", False)),
                local_model=str(meta.get("local_model", "qwen3:4b")),
                mock=mock,
            )
            engine = ElicitationEngine(router, event_log)
            elicit_state = engine.load_state(session_id)

            updated_answers: list[dict[str, str]] = []
            if answers_file is not None:
                if not answers_file.is_file():
                    typer.echo(f"Error: answers file not found: {answers_file}", err=True)
                    raise typer.Exit(code=1)
                batches = _parse_answers_file(answers_file)
                if batches:
                    updated_answers = _bind_answers_to_questions(
                        batches[0], elicit_state.pending_questions
                    )
            else:
                turn_answers = [a for a in elicit_state.answers if a.turn_id == turn_id]
                if turn_answers:
                    updated_answers = [
                        {"question_id": turn_answers[0].question_id, "answer": note}
                    ]

            state_before = event_log.resume(session_id)
            updated_elicit = engine.apply_revision(
                elicit_state,
                turn_id,
                updated_answers,
                note=note,
            )
            state_after = event_log.resume(session_id)
            prior_invalidated = set(state_before.get("invalidated_turns", []))
            invalidated = [
                tid
                for tid in state_after.get("invalidated_turns", [])
                if tid not in prior_invalidated
            ]
        except typer.Exit:
            raise
        except (FileNotFoundError, ValueError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1)

        typer.echo(f"Revised turn '{turn_id}' in session '{session_id}'.")
        typer.echo(f"Note: {note}")
        typer.echo(
            f"Invalidated downstream turns ({len(invalidated)}): "
            + (", ".join(invalidated) if invalidated else "none")
        )
        typer.echo(
            f"Delta re-evaluation complete: pending_questions={len(updated_elicit.pending_questions)} | "
            f"converged={updated_elicit.converged}"
        )
        return

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
    alloy_compat: Annotated[
        Literal["alloy6", "legacy"],
        typer.Option("--alloy-compat", help="Alloy behavioral mode: alloy6 or legacy."),
    ] = "alloy6",
    mock_alloy: Annotated[
        bool,
        typer.Option("--mock-alloy", help="Use offline MockAlloyAnalyzer instead of Java JAR."),
    ] = False,
    max_scope: Annotated[
        int,
        typer.Option("--max-scope", min=1, max=8, help="Bounded Alloy verification scope."),
    ] = 4,
    sessions_dir: Annotated[
        Path,
        typer.Option("--sessions-dir", help="Directory for persistent session logs."),
    ] = Path(".reqhq_sessions"),
) -> None:
    """Verify session event-log parity, snapshot SHA-256 integrity, and Alloy formal gate."""
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

    elicit_state_path = sessions_dir / session_id / "elicitation_state.json"
    alloy_outcome: Any = None
    if elicit_state_path.is_file():
        from reqhq.alloy import (
            AlloyCompatMode,
            AlloyRunStatus,
            AlloySpecEmitter,
            AlloyVerificationController,
        )
        from reqhq.elicitation import ElicitationState

        elicit_state = ElicitationState.model_validate_json(
            elicit_state_path.read_text(encoding="utf-8")
        )
        meta = state.get("metadata") or {}
        router = _build_router(
            sessions_dir=sessions_dir,
            strict_local=bool(meta.get("strict_local", False)),
            local_model=str(meta.get("local_model", "qwen3:4b")),
            mock=mock_alloy,
        )
        analyzer = _build_alloy_analyzer(mock_alloy=mock_alloy)
        compat_enum = AlloyCompatMode(alloy_compat)
        spec_md = AlloySpecEmitter().emit_spec_md(
            elicit_state.graph,
            compat=compat_enum,
            max_scope=max_scope,
        )
        controller = AlloyVerificationController(
            router,
            analyzer,
            max_scope=max_scope,
        )
        alloy_outcome = controller.verify_spec(
            spec_md,
            elicit_state.graph,
            compat=compat_enum,
            event_log=event_log,
            session_id=session_id,
        )
        alloy_ok = (
            alloy_outcome.status == AlloyRunStatus.VERIFIED
            and alloy_outcome.vacuity_checked
        )
        passed = passed and alloy_ok

    typer.echo(f"Verification for session '{session_id}': {'PASSED' if passed else 'FAILED'}")
    typer.echo(f"JSONL/SQLite parity: {'ok' if jsonl_ok else 'mismatch'}")
    typer.echo(
        f"Snapshots verified: {len(snapshots) - len(failed_snapshots)}/{len(snapshots)}"
    )
    if alloy_outcome is not None:
        typer.echo(
            f"Alloy verification ({alloy_outcome.compat_mode.value}): "
            f"{alloy_outcome.status.value} | vacuity_checked={alloy_outcome.vacuity_checked} | "
            f"repairs={alloy_outcome.repair_attempts}"
        )
    if not passed:
        raise typer.Exit(code=1)


@app.command("export")
def export_command(
    session_id: Annotated[str, typer.Argument(help="Session ID to export.")],
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Export full Phase 2 synthesis bundle (Spec.md, ADRs/, Design.md, Process_and_Training.md, traceability_matrix.json) into DIR.",
        ),
    ] = None,
    alloy_compat: Annotated[
        Literal["alloy6", "legacy"],
        typer.Option("--alloy-compat", help="Alloy behavioral mode for Spec.md: alloy6 or legacy."),
    ] = "alloy6",
    mock_alloy: Annotated[
        bool,
        typer.Option("--mock-alloy", help="Use offline MockAlloyAnalyzer during bundle export."),
    ] = False,
    mock: Annotated[
        bool,
        typer.Option("--mock", help="Use offline MockProvider for socio-technical triage."),
    ] = False,
    format: Annotated[
        Literal["json", "md"],
        typer.Option("--format", help="Legacy session export format: json or md."),
    ] = "json",
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Optional output file path for legacy json/md export."),
    ] = None,
    sessions_dir: Annotated[
        Path,
        typer.Option("--sessions-dir", help="Directory for persistent session logs."),
    ] = Path(".reqhq_sessions"),
) -> None:
    """Export a ReqHQ session (Phase 1 json/md or Phase 2 full synthesis bundle via --out)."""
    from reqhq.ingestion import SnapshotStore
    from reqhq.store.event_log import EventKind, SessionEventLog

    event_log = SessionEventLog(sessions_dir)
    try:
        state = event_log.resume(session_id)
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    if out is not None:
        from reqhq.alloy import (
            AlloyCompatMode,
            AlloyRunStatus,
            AlloySpecEmitter,
            AlloyVerificationController,
        )
        from reqhq.elicitation import ElicitationState
        from reqhq.synthesis import (
            SocioTechnicalTriager,
            build_assumption_adrs,
            build_routing_adrs,
            write_synthesis_bundle,
        )

        elicit_state_path = sessions_dir / session_id / "elicitation_state.json"
        if not elicit_state_path.is_file():
            typer.echo(
                f"Error: no elicitation state found for session '{session_id}'. "
                "Run `reqhq elicit` first.",
                err=True,
            )
            raise typer.Exit(code=1)

        elicit_state = ElicitationState.model_validate_json(
            elicit_state_path.read_text(encoding="utf-8")
        )
        meta = state.get("metadata") or {}
        router = _build_router(
            sessions_dir=sessions_dir,
            strict_local=bool(meta.get("strict_local", False)),
            local_model=str(meta.get("local_model", "qwen3:4b")),
            mock=mock or mock_alloy,
        )
        plan = SocioTechnicalTriager(router).triage(
            elicit_state.graph,
            session_id=session_id,
        )
        preview_adrs = [
            *build_routing_adrs(elicit_state.graph, plan),
            *build_assumption_adrs(elicit_state.assumptions),
        ]

        compat_enum = AlloyCompatMode(alloy_compat)
        raw_spec_md = AlloySpecEmitter().emit_spec_md(
            elicit_state.graph,
            triage_plan=plan,
            adrs=preview_adrs,
            compat=compat_enum,
            max_scope=4,
        )
        analyzer = _build_alloy_analyzer(mock_alloy=mock_alloy)
        outcome = AlloyVerificationController(router, analyzer, max_scope=4).verify_spec(
            raw_spec_md,
            elicit_state.graph,
            compat=compat_enum,
            event_log=event_log,
            session_id=session_id,
        )
        try:
            bundle = write_synthesis_bundle(
                out,
                session_id=session_id,
                graph=elicit_state.graph,
                plan=plan,
                assumptions=elicit_state.assumptions,
                alloy_refs=outcome.req_to_alloy_refs,
                spec_markdown=outcome.spec_markdown,
            )
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1)

        event_log.append_event(
            session_id,
            EventKind.EXPORT_COMPLETED,
            payload={
                "format": "synthesis_bundle",
                "output": str(bundle.out_dir),
                "alloy_compat": compat_enum.value,
                "alloy_status": outcome.status.value,
                "alloy_verified": outcome.status == AlloyRunStatus.VERIFIED,
                "adr_count": len(bundle.adr_files),
                "orphaned_req_ids": bundle.orphaned_req_ids,
            },
        )
        typer.echo(f"Exported synthesis bundle for '{session_id}' to {bundle.out_dir}")
        typer.echo(
            f"Artifacts: Spec.md ({compat_enum.value}, {outcome.status.value}), "
            f"{len(bundle.adr_files)} ADR(s), Design.md, Process_and_Training.md, "
            f"traceability_matrix.json (orphans={len(bundle.orphaned_req_ids)})"
        )
        return

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


@app.command("pbt-export")
def pbt_export_command(
    session_id: Annotated[str, typer.Argument(help="Session ID to export PBT guardrails for.")],
    out: Annotated[
        Path,
        typer.Option("--out", help="Output directory for PBT_Spec.yaml, guardrails/, tests/properties/, and pbt_lock.sha256."),
    ],
    mock: Annotated[
        bool,
        typer.Option("--mock", help="Use offline MockProvider for deterministic local execution."),
    ] = False,
    sessions_dir: Annotated[
        Path,
        typer.Option("--sessions-dir", help="Directory for persistent session logs."),
    ] = Path(".reqhq_sessions"),
) -> None:
    """Emit strict PBT_Spec.yaml, Pydantic runtime guardrails, locked Hypothesis tests, and SHA-256 lock."""
    from reqhq.alloy import AlloyCompatMode, AlloySpecEmitter
    from reqhq.elicitation import ElicitationState
    from reqhq.pbt import PBTSpecEmitter, export_pbt_bundle, verify_contract_lock
    from reqhq.store.event_log import EventKind, SessionEventLog
    from reqhq.synthesis import (
        SocioTechnicalTriager,
        build_assumption_adrs,
        build_routing_adrs,
        emit_design_md,
    )

    event_log = SessionEventLog(sessions_dir)
    try:
        state = event_log.resume(session_id)
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    elicit_state_path = sessions_dir / session_id / "elicitation_state.json"
    if not elicit_state_path.is_file():
        typer.echo(
            f"Error: no elicitation state found for session '{session_id}'. "
            "Run `reqhq elicit` first.",
            err=True,
        )
        raise typer.Exit(code=1)

    elicit_state = ElicitationState.model_validate_json(
        elicit_state_path.read_text(encoding="utf-8")
    )
    meta = state.get("metadata") or {}
    router = _build_router(
        sessions_dir=sessions_dir,
        strict_local=bool(meta.get("strict_local", False)),
        local_model=str(meta.get("local_model", "qwen3:4b")),
        mock=mock,
    )
    plan = SocioTechnicalTriager(router).triage(elicit_state.graph, session_id=session_id)
    all_adrs = [
        *build_routing_adrs(elicit_state.graph, plan),
        *build_assumption_adrs(elicit_state.assumptions),
    ]
    design_md = emit_design_md(elicit_state.graph, plan, all_adrs)
    spec_md = AlloySpecEmitter().emit_spec_md(
        elicit_state.graph,
        triage_plan=plan,
        adrs=all_adrs,
        compat=AlloyCompatMode.ALLOY6,
        max_scope=4,
    )
    pbt_doc = PBTSpecEmitter(router=None if mock else router).emit_contract(
        elicit_state.graph,
        session_id=session_id,
        spec_markdown=spec_md,
        adrs=all_adrs,
        design_markdown=design_md,
    )
    resolved_out = out.resolve()
    parts_lower = [part.lower() for part in resolved_out.parts]
    for idx in range(len(parts_lower) - 1):
        if parts_lower[idx] == "docs" and parts_lower[idx + 1] == "adr":
            typer.echo(
                f"Error: Refusing to write target-system PBT bundle into repo Nygard ADR directory: {resolved_out}",
                err=True,
            )
            raise typer.Exit(code=1)

    bundle = export_pbt_bundle(pbt_doc, out)
    verify_contract_lock(out)

    event_log.append_event(
        session_id,
        EventKind.EXPORT_COMPLETED,
        payload={
            "format": "pbt_bundle",
            "output": str(bundle.out_dir),
            "property_count": len(pbt_doc.properties),
            "guardrail_files": len(bundle.guardrail_files),
            "test_files": len(bundle.test_files),
            "lock_file": str(bundle.lock_file),
        },
    )
    typer.echo(f"Exported PBT guardrail bundle for '{session_id}' to {bundle.out_dir}")
    typer.echo(
        f"Artifacts: PBT_Spec.yaml ({len(pbt_doc.properties)} properties), "
        f"{len(bundle.guardrail_files)} guardrail file(s), "
        f"{len(bundle.test_files)} PBT test file(s), pbt_lock.sha256 (verified)"
    )


@app.command("handoff")
def handoff_command(
    session_id: Annotated[str, typer.Argument(help="Session ID to package for Antigravity handoff.")],
    out: Annotated[
        Path,
        typer.Option("--out", help="Output directory for the full Contract-First Antigravity handoff bundle."),
    ],
    alloy_compat: Annotated[
        Literal["alloy6", "legacy"],
        typer.Option("--alloy-compat", help="Alloy behavioral mode for Spec.md: alloy6 or legacy."),
    ] = "alloy6",
    mock: Annotated[
        bool,
        typer.Option("--mock", help="Use offline MockProvider for deterministic local runs."),
    ] = False,
    mock_alloy: Annotated[
        bool,
        typer.Option("--mock-alloy", help="Use offline MockAlloyAnalyzer during handoff bundling."),
    ] = False,
    sessions_dir: Annotated[
        Path,
        typer.Option("--sessions-dir", help="Directory for persistent session logs."),
    ] = Path(".reqhq_sessions"),
) -> None:
    """Build a complete Contract-First Antigravity handoff bundle with locked PBT guardrails."""
    from reqhq.alloy import (
        AlloyCompatMode,
        AlloySpecEmitter,
        AlloyVerificationController,
    )
    from reqhq.elicitation import ElicitationState
    from reqhq.harness import build_handoff_bundle
    from reqhq.pbt import PBTSpecEmitter
    from reqhq.store.event_log import EventKind, SessionEventLog
    from reqhq.synthesis import (
        SocioTechnicalTriager,
        build_assumption_adrs,
        build_routing_adrs,
        emit_design_md,
    )

    event_log = SessionEventLog(sessions_dir)
    try:
        state = event_log.resume(session_id)
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    elicit_state_path = sessions_dir / session_id / "elicitation_state.json"
    if not elicit_state_path.is_file():
        typer.echo(
            f"Error: no elicitation state found for session '{session_id}'. "
            "Run `reqhq elicit` first.",
            err=True,
        )
        raise typer.Exit(code=1)

    elicit_state = ElicitationState.model_validate_json(
        elicit_state_path.read_text(encoding="utf-8")
    )
    meta = state.get("metadata") or {}
    use_mock_llm = mock or mock_alloy
    router = _build_router(
        sessions_dir=sessions_dir,
        strict_local=bool(meta.get("strict_local", False)),
        local_model=str(meta.get("local_model", "qwen3:4b")),
        mock=use_mock_llm,
    )
    plan = SocioTechnicalTriager(router).triage(elicit_state.graph, session_id=session_id)
    preview_adrs = [
        *build_routing_adrs(elicit_state.graph, plan),
        *build_assumption_adrs(elicit_state.assumptions),
    ]
    design_md = emit_design_md(elicit_state.graph, plan, preview_adrs)

    compat_enum = AlloyCompatMode(alloy_compat)
    raw_spec_md = AlloySpecEmitter().emit_spec_md(
        elicit_state.graph,
        triage_plan=plan,
        adrs=preview_adrs,
        compat=compat_enum,
        max_scope=4,
    )
    analyzer = _build_alloy_analyzer(mock_alloy=mock_alloy)
    outcome = AlloyVerificationController(router, analyzer, max_scope=4).verify_spec(
        raw_spec_md,
        elicit_state.graph,
        compat=compat_enum,
        event_log=event_log,
        session_id=session_id,
    )
    pbt_doc = PBTSpecEmitter(router=None if use_mock_llm else router).emit_contract(
        elicit_state.graph,
        session_id=session_id,
        spec_markdown=outcome.spec_markdown,
        adrs=preview_adrs,
        design_markdown=design_md,
    )
    try:
        manifest = build_handoff_bundle(
            out,
            session_id=session_id,
            graph=elicit_state.graph,
            plan=plan,
            spec_markdown=outcome.spec_markdown,
            pbt_doc=pbt_doc,
            assumptions=elicit_state.assumptions,
            alloy_refs=outcome.req_to_alloy_refs,
        )
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    event_log.append_event(
        session_id,
        EventKind.EXPORT_COMPLETED,
        payload={
            "format": "handoff_bundle",
            "output": str(manifest.out_dir),
            "alloy_compat": compat_enum.value,
            "alloy_status": outcome.status.value,
            "adr_count": len(manifest.adr_files),
            "property_count": len(pbt_doc.properties),
            "lock_file": str(manifest.lock_file),
        },
    )
    typer.echo(f"Exported Antigravity handoff bundle for '{session_id}' to {manifest.out_dir}")
    typer.echo(
        f"Artifacts: HANDOFF_INSTRUCTIONS.md, Spec.md ({compat_enum.value}, {outcome.status.value}), "
        f"{len(manifest.adr_files)} ADR(s), PBT_Spec.yaml ({len(pbt_doc.properties)} properties), "
        f"pbt_lock.sha256"
    )


@benchmark_app.command("run")
def benchmark_run_command(
    suite: Annotated[
        Literal["cross_domain", "gap_injection"],
        typer.Option("--suite", help="Benchmark suite to run: cross_domain or gap_injection."),
    ] = "cross_domain",
    out: Annotated[
        Path,
        typer.Option("--out", help="Directory where deterministic benchmark results JSON is written."),
    ] = Path("benchmark_out"),
    seed: Annotated[
        int,
        typer.Option("--seed", help="Deterministic random seed."),
    ] = 42,
    mock: Annotated[
        bool,
        typer.Option("--mock", help="Use offline MockProvider for deterministic local runs."),
    ] = True,
) -> None:
    """Run the 16-case cross-domain or gap-injection oracle benchmark suite."""
    from reqhq.benchmark import (
        load_cross_domain_suite,
        run_cross_domain_benchmark,
        run_gap_injection_benchmark,
    )

    loaded_suite = load_cross_domain_suite()
    if suite == "cross_domain":
        router = _build_router(
            sessions_dir=out,
            strict_local=True,
            mock=mock,
        )
        report = run_cross_domain_benchmark(loaded_suite, router=router, seed=seed)
        metric_line = (
            f"route_accuracy={report['overall_route_accuracy']:.4f} "
            f"({report['matched_routes']}/{report['total_routes']})"
        )
    else:
        report = run_gap_injection_benchmark(loaded_suite, seed=seed)
        metric_line = (
            f"mean_gap_recall={report['mean_gap_recall']:.4f} | "
            f"mean_f1={report['mean_f1']:.4f} | "
            f"leakage_verified={report['all_leakage_verified']}"
        )

    out.mkdir(parents=True, exist_ok=True)
    report_path = out / f"{suite}_results.json"
    with report_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(report, indent=2, sort_keys=True) + "\n")

    typer.echo(
        f"Benchmark '{suite}' complete: cases={report['total_cases']} | {metric_line}"
    )
    typer.echo(f"Report: {report_path}")


@benchmark_app.command("kappa")
def benchmark_kappa_command(
    file_a: Annotated[Path, typer.Argument(help="First annotator JSON/JSONL file.")],
    file_b: Annotated[Path, typer.Argument(help="Second annotator JSON/JSONL file.")],
) -> None:
    """Compute fail-closed dual-annotator Cohen's kappa from two annotation files."""
    from reqhq.benchmark import compute_kappa_from_files

    try:
        report = compute_kappa_from_files(file_a, file_b)
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(
        f"Cohen's kappa: {report.kappa:.4f} | "
        f"observed={report.observed_agreement:.4f} | "
        f"expected={report.expected_agreement:.4f} | "
        f"agreed={report.agreed_count}/{report.shared_count}"
    )
    if report.disagreed_ids:
        typer.echo(f"Disagreed IDs: {', '.join(report.disagreed_ids)}")


def _parse_layers_option(raw_layers: str) -> list[int]:
    """Parse a comma-separated layer specification (subset of 1,2,3,4)."""
    tokens = [tok.strip() for tok in raw_layers.split(",") if tok.strip()]
    if not tokens:
        raise ValueError("layers must specify at least one layer in {1, 2, 3, 4}")
    parsed: list[int] = []
    for tok in tokens:
        if tok not in ("1", "2", "3", "4"):
            raise ValueError(f"Invalid layer {tok!r}: expected integers in {{1, 2, 3, 4}}")
        val = int(tok)
        if val not in parsed:
            parsed.append(val)
    return sorted(parsed)


def _guard_eval_out_dir(out: Path) -> None:
    """Refuse writing evaluation outputs into the repository Nygard ADR namespace."""
    resolved_out = out.resolve()
    parts_lower = [part.lower() for part in resolved_out.parts]
    for idx in range(len(parts_lower) - 1):
        if parts_lower[idx] == "docs" and parts_lower[idx + 1] == "adr":
            raise ValueError(
                f"Refusing to write evaluation output into repo Nygard ADR directory: {resolved_out}"
            )


@eval_app.command("run")
def eval_run_command(
    layers: Annotated[
        str,
        typer.Option("--layers", help="Comma-separated evaluation layers to execute (subset of 1,2,3,4)."),
    ] = "1,2,3,4",
    out: Annotated[
        Path,
        typer.Option("--out", help="Output directory for checkpoint.json, layer*.jsonl, and eval_manifest.json."),
    ] = Path("eval_out"),
    mode: Annotated[
        Literal["mock", "live"],
        typer.Option("--mode", help="Execution mode: mock (offline CI dry-run) or live."),
    ] = "mock",
    replicates: Annotated[
        int,
        typer.Option("--replicates", help="Number of seeded replicates per Layer 3 condition."),
    ] = 2,
    seed: Annotated[
        int,
        typer.Option("--seed", help="Deterministic random seed."),
    ] = 42,
    max_tasks: Annotated[
        int | None,
        typer.Option("--max-tasks", help="Optional limit on Layer 3 coding tasks."),
    ] = None,
) -> None:
    """Run the Phase 4 Layers 1-4 evaluation orchestrator with atomic checkpointing."""
    from reqhq.evaluation import (
        EvalOrchestrator,
        EvalRunConfig,
        LiveExecutionUnavailableError,
        RunMode,
    )

    try:
        _guard_eval_out_dir(out)
        parsed_layers = _parse_layers_option(layers)
        config = EvalRunConfig(
            out_dir=out,
            layers=parsed_layers,
            mode=RunMode(mode),
            replicates=replicates,
            seed=seed,
            max_tasks=max_tasks,
        )
        manifest = EvalOrchestrator(config).run_all()
    except (FileNotFoundError, ValueError, LiveExecutionUnavailableError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    counts = ", ".join(f"{k}={v}" for k, v in sorted(manifest.record_counts.items()))
    typer.echo(
        f"Evaluation complete: run_id={manifest.run_id} | mode={manifest.run_mode.value} | "
        f"layers={manifest.layers_completed} | records=({counts})"
    )
    typer.echo(f"Manifest: {out / 'eval_manifest.json'}")


@eval_app.command("resume")
def eval_resume_command(
    out: Annotated[
        Path,
        typer.Option("--out", help="Existing evaluation directory containing checkpoint.json."),
    ],
) -> None:
    """Resume an interrupted Phase 4 evaluation run with zero duplicate records."""
    from reqhq.evaluation import EvalOrchestrator, LiveExecutionUnavailableError

    try:
        manifest = EvalOrchestrator.resume(out)
    except (FileNotFoundError, ValueError, LiveExecutionUnavailableError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    counts = ", ".join(f"{k}={v}" for k, v in sorted(manifest.record_counts.items()))
    typer.echo(
        f"Evaluation resumed: run_id={manifest.run_id} | mode={manifest.run_mode.value} | "
        f"layers={manifest.layers_completed} | records=({counts})"
    )


@eval_app.command("analyze")
def eval_analyze_command(
    in_dir: Annotated[
        Path,
        typer.Option("--in", help="Input evaluation directory containing eval_manifest.json and layer*.jsonl."),
    ],
    out: Annotated[
        Path,
        typer.Option("--out", help="Output directory for analysis_summary.json and 4 SVG charts."),
    ],
    strict_live: Annotated[
        bool,
        typer.Option("--strict-live", help="Fail closed with non-zero exit if run is mock, incomplete, or tampered."),
    ] = False,
    seed: Annotated[
        int,
        typer.Option("--seed", help="Deterministic bootstrap seed."),
    ] = 42,
    resamples: Annotated[
        int,
        typer.Option("--resamples", help="Number of paired bootstrap resamples."),
    ] = 2000,
) -> None:
    """Run pure-Python H1-H4 statistical evaluation, Pareto SVG charts, and 7-category error taxonomy."""
    from reqhq.analysis import MockRunQuarantineError, analyze_evaluation

    try:
        _guard_eval_out_dir(out)
        summary = analyze_evaluation(
            in_dir,
            out,
            strict_live=strict_live,
            seed=seed,
            n_resamples=resamples,
        )
    except (FileNotFoundError, ValueError, MockRunQuarantineError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    verdicts = ", ".join(f"{h.hypothesis_id}={h.status.value}" for h in summary.hypotheses)
    typer.echo(
        f"Analysis complete: status={summary.verdict_status} | quarantined={summary.quarantined} | "
        f"svgs={len(summary.svg_files)} | {verdicts}"
    )
    typer.echo(f"Summary: {out / 'analysis_summary.json'}")


@eval_app.command("report")
def eval_report_command(
    in_dir: Annotated[
        Path,
        typer.Option("--in", help="Input analysis directory containing analysis_summary.json and SVGs."),
    ],
    out: Annotated[
        Path,
        typer.Option("--out", help="Output directory for FINAL_REPORT.md and/or tables_latex.tex."),
    ],
    fmt: Annotated[
        Literal["md", "tex", "both"],
        typer.Option("--format", help="Output report format: md, tex, or both."),
    ] = "both",
) -> None:
    """Generate FINAL_REPORT.md and Overleaf-ready booktabs tables_latex.tex from analysis_summary.json."""
    from reqhq.reporting import generate_final_report

    try:
        _guard_eval_out_dir(out)
        result = generate_final_report(in_dir, out, fmt=fmt)
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(
        f"Report generated: status={result.verdict_status} | quarantined={result.quarantined} | "
        f"md={result.report_md} | tex={result.tables_tex}"
    )


@eval_app.command("package")
def eval_package_command(
    in_dir: Annotated[
        Path,
        typer.Option("--in", help="Root directory containing eval logs, analysis summary, SVGs, and reports."),
    ],
    out: Annotated[
        Path,
        typer.Option("--out", help="Output directory for the tamper-evident release bundle."),
    ],
) -> None:
    """Package evaluation logs, charts, and reports with SHA-256 artifacts_manifest.json."""
    from reqhq.reporting import package_release_bundle

    try:
        _guard_eval_out_dir(out)
        manifest = package_release_bundle(in_dir, out)
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(
        f"Packaged release bundle: run_id={manifest.run_id} | status={manifest.verdict_status} | "
        f"artifacts={len(manifest.artifacts)}"
    )
    typer.echo(f"Manifest: {out / 'artifacts_manifest.json'}")


@eval_app.command("verify-bundle")
def eval_verify_bundle_command(
    manifest_path: Annotated[
        Path,
        typer.Argument(help="Path to artifacts_manifest.json to verify."),
    ],
) -> None:
    """Cryptographically verify every tracked file in a release bundle against artifacts_manifest.json."""
    from reqhq.reporting import verify_bundle

    result = verify_bundle(manifest_path, raise_on_error=False)
    if not result.valid:
        typer.echo(
            f"Bundle verification FAILED: checked={result.checked_count} | "
            f"errors={'; '.join(result.errors)}",
            err=True,
        )
        raise typer.Exit(code=1)

    typer.echo(f"Bundle verification PASSED: checked={result.checked_count}")


def main() -> None:
    """Entry point registered for console script `reqhq`."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
