from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable


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
