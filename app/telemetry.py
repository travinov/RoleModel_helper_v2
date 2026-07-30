from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Callable, Iterator

from app.agent.models import Outcome, RouteSource, TurnDiagnostics


_COMPONENTS = ("state_read", "retrieval", "agent", "gigachat", "state_write")


class TurnTrace:
    def __init__(
        self,
        *,
        clock: Callable[[], float],
        trace_id: str,
        request_id: str,
        catalog_version: str,
        cache_hit: bool,
    ) -> None:
        self._clock = clock
        self._started = clock()
        self._trace_id = trace_id
        self._request_id = request_id
        self._catalog_version = catalog_version
        self._cache_hit = cache_hit
        self._durations = {name: 0.0 for name in _COMPONENTS}

    @classmethod
    def start(
        cls,
        *,
        clock: Callable[[], float] = time.perf_counter,
        trace_id: str,
        request_id: str,
        catalog_version: str,
        cache_hit: bool,
    ) -> "TurnTrace":
        return cls(
            clock=clock,
            trace_id=trace_id,
            request_id=request_id,
            catalog_version=catalog_version,
            cache_hit=cache_hit,
        )

    @contextmanager
    def component(self, name: str) -> Iterator[None]:
        if name not in self._durations:
            raise ValueError(f"Unknown trace component: {name}")
        started = self._clock()
        try:
            yield
        finally:
            self._durations[name] += (self._clock() - started) * 1000.0

    def finish(
        self,
        *,
        route: RouteSource,
        outcome: Outcome,
        gigachat_calls: int,
    ) -> TurnDiagnostics:
        durations = dict(self._durations)
        durations["total"] = (self._clock() - self._started) * 1000.0
        return TurnDiagnostics(
            trace_id=self._trace_id,
            request_id=self._request_id,
            route=route,
            outcome=outcome,
            catalog_version=self._catalog_version,
            cache_hit=self._cache_hit,
            gigachat_calls=gigachat_calls,
            durations_ms=durations,
        )
