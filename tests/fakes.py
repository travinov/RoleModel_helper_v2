from __future__ import annotations

import copy
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.runtime.service import SessionNotFound, StateRevisionConflict


FIXTURES = Path(__file__).parent / "fixtures"


def load_catalog_mapping(name: str) -> dict[str, Any]:
    with (FIXTURES / name).open(encoding="utf-8") as stream:
        return json.load(stream)


class VersionedCatalogSource:
    """Small synchronous source used to exercise cache ownership and isolation."""

    def __init__(
        self,
        mappings: dict[str, dict[str, Any]],
        bundle_factory: Callable[[dict[str, Any]], Any],
    ) -> None:
        self._mappings = mappings
        self._bundle_factory = bundle_factory
        self.load_calls: list[str] = []
        self.fail_versions: set[str] = set()
        self.block_version: str | None = None
        self.load_started = threading.Event()
        self.release_load = threading.Event()
        self.active_version_value = next(iter(mappings), None)

    def active_version(self) -> str | None:
        return self.active_version_value

    def load(self, version: str) -> Any:
        self.load_calls.append(version)
        if version in self.fail_versions:
            raise RuntimeError(f"catalog {version} is unavailable")
        if version == self.block_version:
            self.load_started.set()
            if not self.release_load.wait(timeout=2):
                raise TimeoutError("test did not release blocked catalog load")
        return self._bundle_factory(self._mappings[version])


class RecordingPlanner:
    def __init__(self, response: Any = None, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def plan(self, request: Any, context: Any, *, deadline_ms: int) -> Any:
        self.calls.append(
            {
                "request": request,
                "context": context,
                "deadline_ms": deadline_ms,
            }
        )
        if self.error is not None:
            raise self.error
        return self.response


class FakeClock:
    def __init__(self, seconds: float = 0.0) -> None:
        self.seconds = seconds

    def __call__(self) -> float:
        return self.seconds

    def advance_ms(self, milliseconds: float) -> None:
        self.seconds += milliseconds / 1000.0


class InMemoryStateStore:
    """Thread-safe contract fake; production bootstrap never uses it."""

    def __init__(self) -> None:
        self._states: dict[str, dict[str, Any]] = {}
        self._turns: dict[str, list[dict[str, Any]]] = {}
        self._responses: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def verify_ready(self) -> None:
        return None

    def create_session(self, *, session_id: str, state: dict[str, Any]) -> None:
        with self._lock:
            if session_id in self._states:
                raise ValueError(f"duplicate session {session_id}")
            self._states[session_id] = copy.deepcopy(state)
            self._turns[session_id] = []

    def get_state(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._states.get(session_id)
            return copy.deepcopy(state) if state is not None else None

    def get_replay(
        self,
        *,
        session_id: str,
        request_id: str,
    ) -> dict[str, Any] | None:
        with self._lock:
            response = self._responses.get((session_id, request_id))
            return copy.deepcopy(response) if response is not None else None

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            if session_id not in self._states:
                raise SessionNotFound(session_id)
            return {
                "session_id": session_id,
                "state": copy.deepcopy(self._states[session_id]),
                "messages": copy.deepcopy(self._turns[session_id]),
            }

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
    ) -> dict[str, Any]:
        with self._lock:
            replay = self._responses.get((session_id, request_id))
            if replay is not None:
                return copy.deepcopy(replay)
            current = self._states.get(session_id)
            if current is None:
                raise SessionNotFound(session_id)
            actual_revision = int(current["revision"])
            if actual_revision != expected_revision:
                raise StateRevisionConflict(
                    f"Expected state revision {actual_revision}, got {expected_revision}"
                )
            next_revision = int(state["revision"])
            if next_revision not in {expected_revision, expected_revision + 1}:
                raise StateRevisionConflict(
                    f"Invalid next state revision {next_revision}"
                )
            stored = copy.deepcopy(response)
            self._states[session_id] = copy.deepcopy(state)
            self._responses[(session_id, request_id)] = stored
            self._turns[session_id].append(
                {
                    "request_id": request_id,
                    "user_text": user_text,
                    "assistant": copy.deepcopy(response.get("assistant")),
                    "catalog_version": catalog_version,
                    "trace_id": trace_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            return copy.deepcopy(stored)
