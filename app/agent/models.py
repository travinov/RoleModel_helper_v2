from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class RouteSource(str, Enum):
    DETERMINISTIC = "DETERMINISTIC"
    GIGACHAT_FALLBACK = "GIGACHAT_FALLBACK"


class Outcome(str, Enum):
    HANDLED = "HANDLED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"


@dataclass(frozen=True)
class Candidate:
    id: str
    label: str
    confidence: float


@dataclass(frozen=True)
class PendingQuestion:
    topic: str
    kind: str
    options: tuple[Candidate, ...] = ()
    page: int = 0


@dataclass(frozen=True)
class TurnState:
    session_id: str
    revision: int
    intent: str | None = None
    phase: str = "READY"
    slots: Mapping[str, str] = field(default_factory=dict)
    pending_question: PendingQuestion | None = None

    @classmethod
    def empty(cls, session_id: str) -> "TurnState":
        return cls(session_id=session_id, revision=0, slots={})


@dataclass(frozen=True)
class TurnRequest:
    request_id: str
    text: str
    state: TurnState


@dataclass(frozen=True)
class Plan:
    catalog_version: str
    intent: str
    action: str
    slots: Mapping[str, str] = field(default_factory=dict)
    confidence: float = 0.0


@dataclass(frozen=True)
class Answer:
    text: str
    answer_type: str
    facts: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Failure:
    code: str
    user_message: str
    retryable: bool = False


@dataclass(frozen=True)
class TurnDiagnostics:
    trace_id: str
    request_id: str
    route: RouteSource
    outcome: Outcome
    catalog_version: str
    cache_hit: bool
    gigachat_calls: int
    durations_ms: Mapping[str, float]


@dataclass(frozen=True)
class TurnResult:
    route: RouteSource
    outcome: Outcome
    state: TurnState
    answer: Answer | None
    diagnostics: TurnDiagnostics
    failure: Failure | None = None
