from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any, Protocol

from app.agent.models import Candidate, PendingQuestion, TurnRequest, TurnResult, TurnState
from app.agent.service import AgentEngine


class StateRevisionConflict(RuntimeError):
    pass


class SessionNotFound(KeyError):
    pass


class StateStore(Protocol):
    def verify_ready(self) -> None: ...

    def create_session(self, *, session_id: str, state: dict[str, Any]) -> None: ...

    def get_state(self, session_id: str) -> dict[str, Any] | None: ...

    def get_replay(
        self,
        *,
        session_id: str,
        request_id: str,
    ) -> dict[str, Any] | None: ...

    def get_session(self, session_id: str) -> dict[str, Any]: ...

    def commit_turn(
        self,
        *,
        session_id: str,
        request_id: str,
        user_text: str,
        expected_revision: int,
        response: dict[str, Any],
        state: dict[str, Any],
        catalog_version: str,
        trace_id: str,
    ) -> dict[str, Any]: ...


class RuntimeService:
    """Coordinates agent work outside the persistence transaction."""

    def __init__(self, *, state_store: StateStore, engine: AgentEngine) -> None:
        self.state_store = state_store
        self.engine = engine

    def initialize(self) -> None:
        self.state_store.verify_ready()

    def create_session(self, session_id: str | None = None) -> dict[str, Any]:
        session_id = session_id or str(uuid.uuid4())
        state = TurnState.empty(session_id)
        self.state_store.create_session(
            session_id=session_id,
            state=self._state_payload(state),
        )
        return {"session_id": session_id, "state": self._state_payload(state), "messages": []}

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self.state_store.get_session(session_id)

    def post_message(
        self,
        *,
        session_id: str,
        request_id: str,
        text: str,
        state_revision: int,
    ) -> dict[str, Any]:
        replay = self.state_store.get_replay(
            session_id=session_id,
            request_id=request_id,
        )
        if replay is not None:
            return replay
        state_payload = self.state_store.get_state(session_id)
        if state_payload is None:
            raise SessionNotFound(session_id)
        state = self._state_from_payload(state_payload, session_id)
        if state.revision != state_revision:
            raise StateRevisionConflict(
                f"Expected state revision {state.revision}, got {state_revision}"
            )

        # Provider and retrieval work intentionally run without a database lock.
        result = self.engine.handle(
            TurnRequest(request_id=request_id, text=text, state=state)
        )
        payload = self._result_payload(result)
        return self.state_store.commit_turn(
            session_id=session_id,
            request_id=request_id,
            user_text=text,
            expected_revision=state_revision,
            response=payload,
            state=self._state_payload(result.state),
            catalog_version=result.diagnostics.catalog_version,
            trace_id=result.diagnostics.trace_id,
        )

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
