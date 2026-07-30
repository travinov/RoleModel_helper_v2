from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.agent.models import Candidate, PendingQuestion, TurnRequest, TurnResult, TurnState
from app.agent.service import AgentEngine


class StateRevisionConflict(RuntimeError):
    pass


class SessionNotFound(KeyError):
    pass


class RuntimeService:
    """Single-process V2 state store with atomic, idempotent turns."""

    def __init__(self, *, database_path: Path, engine: AgentEngine) -> None:
        self.database_path = Path(database_path)
        self.engine = engine
        self._session_locks_guard = threading.Lock()
        self._session_locks: dict[str, threading.Lock] = {}

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS session_state (
                    session_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS turn_message (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES session_state(session_id) ON DELETE CASCADE,
                    request_id TEXT NOT NULL,
                    user_text TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    catalog_version TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(session_id, request_id)
                );
                """
            )

    def create_session(self, session_id: str | None = None) -> dict[str, Any]:
        session_id = session_id or str(uuid.uuid4())
        state = TurnState.empty(session_id)
        encoded = json.dumps(self._state_payload(state), ensure_ascii=False)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO session_state(session_id, state_json) VALUES (?, ?)",
                (session_id, encoded),
            )
        return {"session_id": session_id, "state": self._state_payload(state), "messages": []}

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            session = connection.execute(
                "SELECT state_json FROM session_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise SessionNotFound(session_id)
            rows = connection.execute(
                """
                SELECT request_id, user_text, response_json, catalog_version, trace_id, created_at
                FROM turn_message
                WHERE session_id = ?
                ORDER BY id
                """,
                (session_id,),
            ).fetchall()
        messages = []
        for row in rows:
            response = json.loads(row["response_json"])
            messages.append(
                {
                    "request_id": row["request_id"],
                    "user_text": row["user_text"],
                    "assistant": response.get("assistant"),
                    "catalog_version": row["catalog_version"],
                    "trace_id": row["trace_id"],
                    "created_at": row["created_at"],
                }
            )
        return {
            "session_id": session_id,
            "state": json.loads(session["state_json"]),
            "messages": messages,
        }

    def post_message(
        self,
        *,
        session_id: str,
        request_id: str,
        text: str,
        state_revision: int,
    ) -> dict[str, Any]:
        with self._session_lock(session_id):
            with self._connect() as read_connection:
                replay = read_connection.execute(
                    """
                    SELECT response_json
                    FROM turn_message
                    WHERE session_id = ? AND request_id = ?
                    """,
                    (session_id, request_id),
                ).fetchone()
                if replay is not None:
                    return json.loads(replay["response_json"])
                row = read_connection.execute(
                    "SELECT state_json FROM session_state WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            if row is None:
                raise SessionNotFound(session_id)
            state = self._state_from_payload(json.loads(row["state_json"]), session_id)
            if state.revision != state_revision:
                raise StateRevisionConflict(
                    f"Expected state revision {state.revision}, got {state_revision}"
                )

            # Provider/retrieval work runs without a global SQLite write transaction.
            result = self.engine.handle(
                TurnRequest(request_id=request_id, text=text, state=state)
            )
            payload = self._result_payload(result)
            encoded = json.dumps(payload, ensure_ascii=False)

            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                replay = connection.execute(
                    """
                    SELECT response_json
                    FROM turn_message
                    WHERE session_id = ? AND request_id = ?
                    """,
                    (session_id, request_id),
                ).fetchone()
                if replay is not None:
                    connection.rollback()
                    return json.loads(replay["response_json"])
                row = connection.execute(
                    "SELECT state_json FROM session_state WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise SessionNotFound(session_id)
                latest_state = self._state_from_payload(
                    json.loads(row["state_json"]),
                    session_id,
                )
                if latest_state.revision != state_revision:
                    raise StateRevisionConflict(
                        f"Expected state revision {latest_state.revision}, got {state_revision}"
                    )
                connection.execute(
                    """
                    INSERT INTO turn_message(
                        session_id, request_id, user_text, response_json, catalog_version, trace_id
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        request_id,
                        text,
                        encoded,
                        result.diagnostics.catalog_version,
                        result.diagnostics.trace_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE session_state
                    SET state_json = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE session_id = ?
                    """,
                    (
                        json.dumps(self._state_payload(result.state), ensure_ascii=False),
                        session_id,
                    ),
                )
                connection.commit()
                return payload
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _session_lock(self, session_id: str) -> threading.Lock:
        with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    @staticmethod
    def _state_payload(state: TurnState) -> dict[str, Any]:
        pending = None
        if state.pending_question is not None:
            pending = {
                "topic": state.pending_question.topic,
                "kind": state.pending_question.kind,
                "options": [asdict(option) for option in state.pending_question.options],
                "page": state.pending_question.page,
            }
        return {
            "revision": state.revision,
            "intent": state.intent,
            "phase": state.phase,
            "slots": dict(state.slots),
            "pending_question": pending,
        }

    @staticmethod
    def _state_from_payload(payload: dict[str, Any], session_id: str | None = None) -> TurnState:
        pending_payload = payload.get("pending_question")
        pending = None
        if pending_payload:
            pending = PendingQuestion(
                topic=str(pending_payload["topic"]),
                kind=str(pending_payload["kind"]),
                options=tuple(
                    Candidate(
                        id=str(item["id"]),
                        label=str(item["label"]),
                        confidence=float(item["confidence"]),
                    )
                    for item in pending_payload.get("options") or []
                ),
                page=int(pending_payload.get("page") or 0),
            )
        resolved_session_id = session_id or str(payload.get("session_id") or "")
        return TurnState(
            session_id=resolved_session_id,
            revision=int(payload["revision"]),
            intent=payload.get("intent"),
            phase=str(payload.get("phase") or "READY"),
            slots=dict(payload.get("slots") or {}),
            pending_question=pending,
        )

    def _result_payload(self, result: TurnResult) -> dict[str, Any]:
        answer = result.answer
        failure = result.failure
        assistant_text = (
            answer.text
            if answer is not None
            else failure.user_message
            if failure is not None
            else "Не удалось сформировать ответ."
        )
        return {
            "request_id": result.diagnostics.request_id,
            "session_id": result.state.session_id,
            "assistant": {
                "text": assistant_text,
                "answer_type": answer.answer_type if answer is not None else "ERROR",
                "facts": dict(answer.facts) if answer is not None else {},
            },
            "state": self._state_payload(result.state),
            "failure": (
                {
                    "code": failure.code,
                    "user_message": failure.user_message,
                    "retryable": failure.retryable,
                }
                if failure is not None
                else None
            ),
            "diagnostics": {
                "trace_id": result.diagnostics.trace_id,
                "route": result.diagnostics.route.value,
                "outcome": result.diagnostics.outcome.value,
                "catalog_version": result.diagnostics.catalog_version,
                "cache_hit": result.diagnostics.cache_hit,
                "gigachat_calls": result.diagnostics.gigachat_calls,
                "durations_ms": dict(result.diagnostics.durations_ms),
            },
        }
