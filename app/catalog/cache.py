from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol


class CatalogSource(Protocol):
    def load(self, version: str) -> "CatalogBundle": ...


class Freshness(str, Enum):
    EMPTY = "EMPTY"
    READY = "READY"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True)
class CacheStatus:
    freshness: Freshness
    active_version: str | None
    last_error: str | None = None


@dataclass(frozen=True)
class RefreshResult:
    version: str
    activated: bool
    cache_hit: bool
    error: str | None = None


def _frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class CatalogBundle:
    version: str
    systems: Mapping[str, Mapping[str, Any]]
    departments: tuple[Mapping[str, Any], ...]
    instructions: tuple[Mapping[str, Any], ...]
    role_ids: frozenset[str]

    @property
    def catalog_version(self) -> str:
        return self.version

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "CatalogBundle":
        systems: dict[str, Mapping[str, Any]] = {}
        role_ids: set[str] = set()
        for raw_system in payload.get("systems") or []:
            aliases = tuple(_frozen_mapping(item) for item in raw_system.get("aliases") or [])
            roles = tuple(_frozen_mapping(item) for item in raw_system.get("roles") or [])
            role_ids.update(str(role["id"]) for role in roles)
            system = {
                "id": str(raw_system["id"]),
                "name": str(raw_system["name"]),
                "aliases": aliases,
                "roles": roles,
            }
            systems[str(raw_system["id"])] = _frozen_mapping(system)
        return cls(
            version=str(payload["version"]),
            systems=MappingProxyType(systems),
            departments=tuple(_frozen_mapping(item) for item in payload.get("departments") or []),
            instructions=tuple(_frozen_mapping(item) for item in payload.get("instructions") or []),
            role_ids=frozenset(role_ids),
        )


class CatalogUnavailable(RuntimeError):
    pass


class CatalogCache:
    """Single-flight immutable catalog publication with last-good fallback."""

    def __init__(self, source: CatalogSource) -> None:
        self._source = source
        self._active: CatalogBundle | None = None
        self._status = CacheStatus(Freshness.EMPTY, None)
        self._lock = threading.Lock()
        self._loading: dict[str, threading.Event] = {}

    @property
    def status(self) -> CacheStatus:
        with self._lock:
            return self._status

    def get(self) -> CatalogBundle:
        with self._lock:
            if self._active is None:
                raise CatalogUnavailable("Catalog has not been loaded")
            return self._active

    def refresh(self, version: str) -> RefreshResult:
        with self._lock:
            if self._active is not None and self._active.version == version:
                return RefreshResult(version=version, activated=False, cache_hit=True)
            event = self._loading.get(version)
            if event is None:
                event = threading.Event()
                self._loading[version] = event
                is_loader = True
            else:
                is_loader = False

        if not is_loader:
            event.wait()
            with self._lock:
                active = self._active
                if active is not None and active.version == version:
                    return RefreshResult(version=version, activated=False, cache_hit=True)
                error = self._status.last_error or f"Catalog {version} was not activated"
                return RefreshResult(version=version, activated=False, cache_hit=False, error=error)

        try:
            bundle = self._source.load(version)
            if bundle.version != version:
                raise ValueError(f"Loaded catalog version {bundle.version!r}, expected {version!r}")
        except Exception as exc:
            error = str(exc)
            with self._lock:
                self._status = CacheStatus(
                    Freshness.DEGRADED if self._active is not None else Freshness.EMPTY,
                    self._active.version if self._active is not None else None,
                    error,
                )
                self._loading.pop(version, None)
                event.set()
            return RefreshResult(version=version, activated=False, cache_hit=False, error=error)

        with self._lock:
            self._active = bundle
            self._status = CacheStatus(Freshness.READY, bundle.version)
            self._loading.pop(version, None)
            event.set()
        return RefreshResult(version=version, activated=True, cache_hit=False)
