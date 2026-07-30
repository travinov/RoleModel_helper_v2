from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from app.catalog.normalization import normalize_query, parse_department_number


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
    positions: tuple[Mapping[str, Any], ...]
    profiles: Mapping[str, Mapping[str, Any]]
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
        departments = []
        for raw_department in payload.get("departments") or []:
            department = dict(raw_department)
            department["id"] = str(raw_department["id"])
            department["name"] = str(raw_department["name"])
            department["city"] = str(raw_department.get("city") or "")
            department["normalized_name"] = normalize_query(department["name"])
            department["normalized_city"] = normalize_query(department["city"])
            department["number"] = parse_department_number(department["name"])
            department["aliases"] = tuple(
                _frozen_mapping(item)
                for item in raw_department.get("aliases") or []
            )
            departments.append(_frozen_mapping(department))
        positions = []
        for raw_position in payload.get("positions") or []:
            position = dict(raw_position)
            position["id"] = str(raw_position["id"])
            position["name"] = str(raw_position["name"])
            position["normalized_name"] = normalize_query(position["name"])
            position["aliases"] = tuple(
                _frozen_mapping(item)
                for item in raw_position.get("aliases") or []
            )
            positions.append(_frozen_mapping(position))
        profiles: dict[str, Mapping[str, Any]] = {}
        for raw_profile in payload.get("profiles") or []:
            profile = dict(raw_profile)
            profile["id"] = str(raw_profile["id"])
            profile["name"] = str(raw_profile.get("name") or raw_profile["id"])
            profile["city"] = str(raw_profile.get("city") or "")
            profile["normalized_city"] = normalize_query(profile["city"])
            profile["department_ids"] = tuple(
                str(value) for value in raw_profile.get("department_ids") or []
            )
            profile["position_ids"] = tuple(
                str(value) for value in raw_profile.get("position_ids") or []
            )
            profile["access"] = tuple(
                _frozen_mapping(
                    {
                        **dict(item),
                        "system_id": str(item["system_id"]),
                        "role_ids": tuple(
                            str(value) for value in item.get("role_ids") or []
                        ),
                    }
                )
                for item in raw_profile.get("access") or []
            )
            profiles[profile["id"]] = _frozen_mapping(profile)
        return cls(
            version=str(payload["version"]),
            systems=MappingProxyType(systems),
            departments=tuple(departments),
            positions=tuple(positions),
            profiles=MappingProxyType(profiles),
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
        self._refresh_thread: threading.Thread | None = None
        self._stop_refresh = threading.Event()

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
                self._status = CacheStatus(Freshness.READY, version)
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

    def refresh_active(self) -> RefreshResult:
        active_version = getattr(self._source, "active_version", None)
        if not callable(active_version):
            raise RuntimeError("Catalog source has no active version pointer")
        try:
            version = active_version()
        except Exception as exc:
            self._record_source_error(str(exc))
            raise
        if not version:
            error = "Catalog source has no active release"
            self._record_source_error(error)
            return RefreshResult(
                version="",
                activated=False,
                cache_hit=False,
                error=error,
            )
        return self.refresh(str(version))

    def start_auto_refresh(self, interval_seconds: float) -> None:
        interval = max(float(interval_seconds), 0.1)
        with self._lock:
            if self._refresh_thread is not None:
                return
            if not callable(getattr(self._source, "active_version", None)):
                return
            self._stop_refresh.clear()
            thread = threading.Thread(
                target=self._auto_refresh_loop,
                args=(interval,),
                name="rolemodel-v2-catalog-refresh",
                daemon=True,
            )
            self._refresh_thread = thread
        thread.start()

    def stop_auto_refresh(self) -> None:
        with self._lock:
            thread = self._refresh_thread
            self._refresh_thread = None
            self._stop_refresh.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _auto_refresh_loop(self, interval_seconds: float) -> None:
        while not self._stop_refresh.wait(interval_seconds):
            try:
                self.refresh_active()
            except Exception:
                continue

    def _record_source_error(self, error: str) -> None:
        with self._lock:
            self._status = CacheStatus(
                Freshness.DEGRADED if self._active is not None else Freshness.EMPTY,
                self._active.version if self._active is not None else None,
                error,
            )
