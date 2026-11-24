"""Append-only SQLite + JSONL session event log with resume and turn revision support."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class EventKind(str, Enum):
    """Canonical lifecycle event kinds stored in SessionEventLog."""

    SESSION_INIT = "SESSION_INIT"
    SOURCE_INGESTED = "SOURCE_INGESTED"
    TURN_COMPLETED = "TURN_COMPLETED"
    TURN_REVISED = "TURN_REVISED"
    TURN_INVALIDATED = "TURN_INVALIDATED"
    ESCALATION_TRIGGERED = "ESCALATION_TRIGGERED"
    VERIFICATION_RECORDED = "VERIFICATION_RECORDED"
    EXPORT_COMPLETED = "EXPORT_COMPLETED"


class SessionEvent(BaseModel):
    """Single immutable event record in a session's append-only log."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(..., ge=1)
    event_id: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)
    timestamp: str = Field(..., min_length=1)
    kind: EventKind
    turn_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_session_id(session_id: str) -> str:
    cleaned = session_id.strip()
    if not _SESSION_ID_RE.match(cleaned) or ".." in cleaned:
        raise ValueError(
            f"Invalid session_id {session_id!r}: must match {_SESSION_ID_RE.pattern} without '..'"
        )
    return cleaned


class SessionEventLog:
    """Append-only SQLite + JSONL event store at <sessions_dir>/<session_id>/."""

    def __init__(self, sessions_dir: Path = Path(".reqhq_sessions")) -> None:
        self.sessions_dir = Path(sessions_dir)

    def _session_dir(self, session_id: str) -> Path:
        validated = _validate_session_id(session_id)
        return self.sessions_dir / validated

    def _db_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "events.db"

    def _jsonl_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "events.jsonl"

    def _require_session(self, session_id: str) -> Path:
        db_path = self._db_path(session_id)
        if not db_path.exists():
            raise FileNotFoundError(f"Session {session_id!r} does not exist at {db_path}")
        return db_path

    def _init_db(self, db_path: Path) -> None:
        with closing(sqlite3.connect(db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL UNIQUE,
                        session_id TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        turn_id TEXT,
                        payload_json TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_turn_id ON events (turn_id)"
                )

    def create_session(
        self,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Create a new session directory, SQLite DB, JSONL log, and SESSION_INIT event."""
        resolved_id = (
            f"sess-{uuid.uuid4().hex[:12]}"
            if session_id is None
            else _validate_session_id(session_id)
        )
        sdir = self._session_dir(resolved_id)
        db_path = sdir / "events.db"
        jsonl_path = sdir / "events.jsonl"
        if db_path.exists():
            raise FileExistsError(f"Session {resolved_id!r} already exists")

        sdir.mkdir(parents=True, exist_ok=True)
        self._init_db(db_path)
        jsonl_path.touch(exist_ok=True)

        self.append_event(
            resolved_id,
            EventKind.SESSION_INIT,
            payload=dict(metadata or {}),
            turn_id=None,
        )
        return resolved_id

    def append_event(
        self,
        session_id: str,
        kind: EventKind,
        payload: dict[str, Any],
        turn_id: str | None = None,
    ) -> SessionEvent:
        """Append an immutable event to both events.db and events.jsonl."""
        validated_id = _validate_session_id(session_id)
        db_path = self._require_session(validated_id)
        jsonl_path = self._jsonl_path(validated_id)
        event_kind = EventKind(kind)
        normalized_turn_id: str | None = None
        if turn_id is not None:
            normalized_turn_id = turn_id.strip()
            if not normalized_turn_id:
                raise ValueError("turn_id must not be blank or whitespace-only")
        event_id = uuid.uuid4().hex
        timestamp = _utc_now_iso()
        payload_copy = dict(payload)
        payload_json = json.dumps(payload_copy, sort_keys=True, ensure_ascii=False)

        with closing(sqlite3.connect(db_path)) as conn:
            with conn:
                cursor = conn.execute(
                    """
                    INSERT INTO events (event_id, session_id, timestamp, kind, turn_id, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        validated_id,
                        timestamp,
                        event_kind.value,
                        normalized_turn_id,
                        payload_json,
                    ),
                )
                seq = int(cursor.lastrowid or 0)

        event = SessionEvent(
            seq=seq,
            event_id=event_id,
            session_id=validated_id,
            timestamp=timestamp,
            kind=event_kind,
            turn_id=normalized_turn_id,
            payload=payload_copy,
        )
        with jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")
            fh.flush()
        return event

    def load_events(self, session_id: str) -> list[SessionEvent]:
        """Load the complete append-only event history for a session ordered by seq."""
        db_path = self._require_session(session_id)
        with closing(sqlite3.connect(db_path)) as conn:
            rows = conn.execute(
                """
                SELECT seq, event_id, session_id, timestamp, kind, turn_id, payload_json
                FROM events
                ORDER BY seq ASC
                """
            ).fetchall()

        return [
            SessionEvent(
                seq=int(row[0]),
                event_id=row[1],
                session_id=row[2],
                timestamp=row[3],
                kind=EventKind(row[4]),
                turn_id=row[5],
                payload=json.loads(row[6]),
            )
            for row in rows
        ]

    @staticmethod
    def _replay_active_state(
        events: list[SessionEvent],
    ) -> tuple[list[SessionEvent], list[str]]:
        """Replay events to compute active events and currently invalidated turn IDs."""
        active: list[SessionEvent] = []
        invalidated_turns: list[str] = []

        for event in events:
            if event.turn_id is None:
                active.append(event)
                continue

            tid = event.turn_id
            if event.kind == EventKind.TURN_INVALIDATED:
                active = [e for e in active if e.turn_id != tid]
                if tid not in invalidated_turns:
                    invalidated_turns.append(tid)
            elif event.kind == EventKind.TURN_REVISED:
                active = [e for e in active if e.turn_id != tid]
                active.append(event)
                if tid in invalidated_turns:
                    invalidated_turns.remove(tid)
            elif event.kind == EventKind.TURN_COMPLETED:
                active = [
                    e
                    for e in active
                    if not (
                        e.turn_id == tid
                        and e.kind in (EventKind.TURN_COMPLETED, EventKind.TURN_REVISED)
                    )
                ]
                active.append(event)
                if tid in invalidated_turns:
                    invalidated_turns.remove(tid)
            else:
                active.append(event)

        return active, invalidated_turns

    def get_active_events(self, session_id: str) -> list[SessionEvent]:
        """Return currently active events after applying TURN_REVISED and TURN_INVALIDATED."""
        events = self.load_events(session_id)
        active, _ = self._replay_active_state(events)
        return active

    def revise(
        self,
        session_id: str,
        turn_id: str,
        revised_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Revise a turn by appending TURN_REVISED and TURN_INVALIDATED for downstream turns.

        Append-only history is never deleted or mutated in place.
        """
        validated_id = _validate_session_id(session_id)
        normalized_turn_id = turn_id.strip()
        active_before = self.get_active_events(validated_id)
        active_turn_events = [
            e
            for e in active_before
            if e.turn_id is not None
            and e.kind in (EventKind.TURN_COMPLETED, EventKind.TURN_REVISED)
        ]

        target_index: int | None = None
        for idx, ev in enumerate(active_turn_events):
            if ev.turn_id == normalized_turn_id:
                target_index = idx
                break

        if target_index is None:
            raise ValueError(
                f"Turn {turn_id!r} is not an active turn in session {validated_id!r}"
            )

        downstream_events = active_turn_events[target_index + 1 :]
        revision_event = self.append_event(
            validated_id,
            EventKind.TURN_REVISED,
            payload=dict(revised_payload),
            turn_id=normalized_turn_id,
        )

        invalidation_events: list[SessionEvent] = []
        invalidated_turn_ids: list[str] = []
        for downstream in downstream_events:
            assert downstream.turn_id is not None
            inv_event = self.append_event(
                validated_id,
                EventKind.TURN_INVALIDATED,
                payload={
                    "reason": f"downstream_of_revised_turn:{normalized_turn_id}",
                    "caused_by_turn_id": normalized_turn_id,
                    "caused_by_seq": revision_event.seq,
                    "invalidated_seq": downstream.seq,
                },
                turn_id=downstream.turn_id,
            )
            invalidation_events.append(inv_event)
            invalidated_turn_ids.append(downstream.turn_id)

        active_after = self.get_active_events(validated_id)
        return {
            "session_id": validated_id,
            "revised_turn_id": normalized_turn_id,
            "revision_event": revision_event.model_dump(),
            "invalidated_turn_ids": invalidated_turn_ids,
            "invalidation_events": [e.model_dump() for e in invalidation_events],
            "active_event_count": len(active_after),
        }

    def resume(self, session_id: str) -> dict[str, Any]:
        """Reconstruct a resumable state snapshot for session_id."""
        validated_id = _validate_session_id(session_id)
        all_events = self.load_events(validated_id)
        active_events, invalidated_turns = self._replay_active_state(all_events)

        init_metadata: dict[str, Any] = {}
        for event in all_events:
            if event.kind == EventKind.SESSION_INIT:
                init_metadata = dict(event.payload)
                break

        active_turns = [
            event.turn_id
            for event in active_events
            if event.turn_id is not None
            and event.kind in (EventKind.TURN_COMPLETED, EventKind.TURN_REVISED)
        ]

        return {
            "session_id": validated_id,
            "total_events": len(all_events),
            "active_events": [e.model_dump() for e in active_events],
            "active_turns": active_turns,
            "invalidated_turns": invalidated_turns,
            "last_seq": all_events[-1].seq if all_events else 0,
            "last_turn_id": active_turns[-1] if active_turns else None,
            "metadata": init_metadata,
            "sources": [
                e.payload for e in active_events if e.kind == EventKind.SOURCE_INGESTED
            ],
            "verifications": [
                e.payload for e in active_events if e.kind == EventKind.VERIFICATION_RECORDED
            ],
        }

    def list_sessions(self) -> list[str]:
        """List sorted session IDs present under sessions_dir."""
        if not self.sessions_dir.exists():
            return []
        sessions: list[str] = []
        for entry in self.sessions_dir.iterdir():
            if entry.is_dir() and (entry / "events.db").is_file():
                sessions.append(entry.name)
        return sorted(sessions)

    def verify_jsonl_parity(self, session_id: str) -> bool:
        """Verify that events.jsonl matches events.db event-for-event."""
        db_events = self.load_events(session_id)
        jsonl_path = self._jsonl_path(session_id)
        if not jsonl_path.exists():
            return False
        jsonl_events: list[SessionEvent] = []
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    jsonl_events.append(SessionEvent.model_validate_json(stripped))
        return db_events == jsonl_events


__all__ = [
    "EventKind",
    "SessionEvent",
    "SessionEventLog",
]
