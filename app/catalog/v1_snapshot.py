from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.catalog.normalization import normalize_query


_CITY_PATTERN = re.compile(
    r"\b(?:г|город)\.?\s*([a-zA-Zа-яА-ЯёЁ-]+)",
    re.IGNORECASE,
)
_POSITION_ALIASES = {
    "руководитель": ("начальник",),
}


class V1SnapshotExtractionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExtractedCatalog:
    document: dict[str, Any]
    source_sha256: str
    snapshot_id: int
    counts: dict[str, int]


class V1SnapshotCatalogAdapter:
    """Read-only, repeatable-read export from one active V1 ETL snapshot."""

    def __init__(self, *, dsn: str, v1_schema: str) -> None:
        if not v1_schema.strip():
            raise ValueError("V1 schema must not be empty")
        self._dsn = dsn
        self._schema = v1_schema

    def extract(self) -> ExtractedCatalog:
        schema = sql.Identifier(self._schema)
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            snapshot = connection.execute(
                sql.SQL(
                    """
                    SELECT s.id, s.snapshot_label, s.loaded_at,
                           s.source_file, r.file_sha256
                    FROM {}.snapshot s
                    JOIN {}.etl_run r ON r.id = s.run_id
                    WHERE s.is_active = TRUE
                    ORDER BY s.loaded_at DESC, s.id DESC
                    LIMIT 1
                    """
                ).format(schema, schema)
            ).fetchone()
            if snapshot is None:
                raise V1SnapshotExtractionError(
                    f"V1 schema {self._schema!r} has no active snapshot"
                )
            snapshot_id = int(snapshot["id"])
            source_sha256 = str(snapshot["file_sha256"] or "").strip()
            if not source_sha256:
                raise V1SnapshotExtractionError(
                    f"V1 snapshot {snapshot_id} has no source SHA-256"
                )

            systems_raw = connection.execute(
                sql.SQL(
                    """
                    SELECT id, system_name_raw, ci_code
                    FROM {}.system
                    WHERE snapshot_id = %s
                    ORDER BY id
                    """
                ).format(schema),
                (snapshot_id,),
            ).fetchall()
            aliases_raw = connection.execute(
                sql.SQL(
                    """
                    SELECT system_id, alias_text, alias_class
                    FROM {}.system_alias_candidate
                    WHERE snapshot_id = %s
                    UNION ALL
                    SELECT sa.system_id, sa.alias_text, 'SAFE' AS alias_class
                    FROM {}.system_alias sa
                    JOIN {}.system s ON s.id = sa.system_id
                    WHERE s.snapshot_id = %s AND sa.is_active = TRUE
                    ORDER BY system_id, alias_text
                    """
                ).format(schema, schema, schema),
                (snapshot_id, snapshot_id),
            ).fetchall()
            department_aliases_raw = connection.execute(
                sql.SQL(
                    """
                    SELECT department_name, alias_text, alias_class
                    FROM {}.department_alias_candidate
                    WHERE snapshot_id = %s
                    ORDER BY department_name, id
                    """
                ).format(schema),
                (snapshot_id,),
            ).fetchall()
            profiles_raw = connection.execute(
                sql.SQL(
                    """
                    SELECT id, profile_code, profile_name
                    FROM {}.profile
                    WHERE snapshot_id = %s
                    ORDER BY id
                    """
                ).format(schema),
                (snapshot_id,),
            ).fetchall()
            segments_raw = connection.execute(
                sql.SQL(
                    """
                    SELECT profile_id, segment_name
                    FROM {}.profile_structure_segment
                    WHERE snapshot_id = %s
                    ORDER BY profile_id, path_order, segment_order, id
                    """
                ).format(schema),
                (snapshot_id,),
            ).fetchall()
            departments_raw = connection.execute(
                sql.SQL(
                    """
                    SELECT profile_id, department_name, department_code
                    FROM {}.profile_department
                    WHERE snapshot_id = %s
                    ORDER BY profile_id, id
                    """
                ).format(schema),
                (snapshot_id,),
            ).fetchall()
            positions_raw = connection.execute(
                sql.SQL(
                    """
                    SELECT profile_id, position_name, position_code
                    FROM {}.profile_position
                    WHERE snapshot_id = %s
                    ORDER BY profile_id, id
                    """
                ).format(schema),
                (snapshot_id,),
            ).fetchall()
            accesses_raw = connection.execute(
                sql.SQL(
                    """
                    SELECT pea.profile_id, pea.access_level,
                           e.id AS entitlement_id, e.source_col,
                           e.entitlement_type, e.entitlement_name,
                           e.system_id
                    FROM {}.profile_entitlement_access pea
                    JOIN {}.entitlement e ON e.id = pea.entitlement_id
                    WHERE pea.snapshot_id = %s
                    ORDER BY pea.profile_id, e.system_id, e.source_col,
                             pea.access_level
                    """
                ).format(schema, schema),
                (snapshot_id,),
            ).fetchall()

        return self._build_document(
            snapshot_id=snapshot_id,
            source_sha256=source_sha256,
            systems_raw=systems_raw,
            aliases_raw=aliases_raw,
            department_aliases_raw=department_aliases_raw,
            profiles_raw=profiles_raw,
            segments_raw=segments_raw,
            departments_raw=departments_raw,
            positions_raw=positions_raw,
            accesses_raw=accesses_raw,
        )

    @staticmethod
    def _build_document(
        *,
        snapshot_id: int,
        source_sha256: str,
        systems_raw: list[dict[str, Any]],
        aliases_raw: list[dict[str, Any]],
        department_aliases_raw: list[dict[str, Any]],
        profiles_raw: list[dict[str, Any]],
        segments_raw: list[dict[str, Any]],
        departments_raw: list[dict[str, Any]],
        positions_raw: list[dict[str, Any]],
        accesses_raw: list[dict[str, Any]],
    ) -> ExtractedCatalog:
        aliases_by_system_key: dict[int, dict[str, dict[str, str]]] = {}
        for row in aliases_raw:
            safety = str(row["alias_class"] or "").upper()
            if safety == "UNSAFE":
                continue
            system_id = int(row["system_id"])
            raw = str(row["alias_text"])
            normalized = normalize_query(raw)
            if not normalized:
                continue
            aliases = aliases_by_system_key.setdefault(system_id, {})
            candidate = {
                "value": raw,
                "safety": "SAFE" if safety == "SAFE" else "AMBIGUOUS",
            }
            existing = aliases.get(normalized)
            if existing is None or candidate["safety"] == "SAFE":
                aliases[normalized] = candidate
        aliases_by_system = {
            system_id: list(aliases.values())
            for system_id, aliases in aliases_by_system_key.items()
        }

        department_aliases_by_name: dict[str, dict[str, dict[str, str]]] = {}
        for row in department_aliases_raw:
            safety = str(row["alias_class"] or "").upper()
            if safety == "UNSAFE":
                continue
            name = str(row["department_name"])
            raw = str(row["alias_text"])
            normalized = normalize_query(raw)
            if not normalized:
                continue
            aliases = department_aliases_by_name.setdefault(name, {})
            aliases[normalized] = {
                "value": raw,
                "safety": "SAFE" if safety == "SAFE" else "AMBIGUOUS",
            }

        system_id_by_db: dict[int, str] = {}
        systems: list[dict[str, Any]] = []
        for row in systems_raw:
            db_id = int(row["id"])
            system_id = _stable_id(
                "system",
                str(row["ci_code"] or ""),
                str(row["system_name_raw"]),
            )
            system_id_by_db[db_id] = system_id
            systems.append(
                {
                    "id": system_id,
                    "name": str(row["system_name_raw"]),
                    "aliases": aliases_by_system.get(db_id, []),
                    "roles": [],
                }
            )
        systems_by_id = {item["id"]: item for item in systems}

        cities_by_profile: dict[int, str] = {}
        for row in segments_raw:
            profile_id = int(row["profile_id"])
            if profile_id in cities_by_profile:
                continue
            city = _extract_city(str(row["segment_name"] or ""))
            if city:
                cities_by_profile[profile_id] = city

        department_id_by_key: dict[tuple[str, str], str] = {}
        departments_by_id: dict[str, dict[str, Any]] = {}
        department_ids_by_profile: dict[int, list[str]] = {}
        for row in departments_raw:
            name = str(row["department_name"])
            code = str(row["department_code"] or "")
            city = cities_by_profile.get(int(row["profile_id"]), "")
            key = (code or normalize_query(name), normalize_query(city))
            department_id = department_id_by_key.setdefault(
                key,
                _stable_id(
                    "department",
                    f"{code}|{city}" if code else "",
                    f"{name}|{city}",
                ),
            )
            departments_by_id.setdefault(
                department_id,
                {
                    "id": department_id,
                    "name": name,
                    "city": city,
                    "aliases": list(
                        department_aliases_by_name.get(name, {}).values()
                    ),
                },
            )
            _append_unique(
                department_ids_by_profile,
                int(row["profile_id"]),
                department_id,
            )

        position_id_by_key: dict[str, str] = {}
        positions_by_id: dict[str, dict[str, Any]] = {}
        position_ids_by_profile: dict[int, list[str]] = {}
        for row in positions_raw:
            name = str(row["position_name"])
            code = str(row["position_code"] or "")
            key = code or normalize_query(name)
            position_id = position_id_by_key.setdefault(
                key,
                _stable_id("position", code, name),
            )
            normalized_name = normalize_query(name)
            aliases = [
                {"value": value, "safety": "SAFE"}
                for value in _POSITION_ALIASES.get(normalized_name, ())
            ]
            positions_by_id.setdefault(
                position_id,
                {
                    "id": position_id,
                    "name": name,
                    "aliases": aliases,
                },
            )
            _append_unique(
                position_ids_by_profile,
                int(row["profile_id"]),
                position_id,
            )

        role_id_by_access: dict[tuple[int, int, int], str] = {}
        profile_access: dict[int, dict[str, list[str]]] = {}
        for row in accesses_raw:
            system_db_id = int(row["system_id"])
            system_id = system_id_by_db.get(system_db_id)
            if system_id is None:
                raise V1SnapshotExtractionError(
                    f"Entitlement references missing system {system_db_id}"
                )
            access_level = int(row["access_level"])
            role_key = (
                system_db_id,
                int(row["entitlement_id"]),
                access_level,
            )
            role_id = role_id_by_access.setdefault(
                role_key,
                (
                    f"role:{system_id}:{int(row['source_col'])}:"
                    f"level:{access_level}"
                ),
            )
            role_name = str(row["entitlement_name"] or "").strip()
            if not role_name:
                role_name = str(row["entitlement_type"] or "Доступ")
            if not any(
                str(item["id"]) == role_id
                for item in systems_by_id[system_id]["roles"]
            ):
                systems_by_id[system_id]["roles"].append(
                    {
                        "id": role_id,
                        "name": role_name,
                        "access_level": access_level,
                    }
                )
            by_system = profile_access.setdefault(
                int(row["profile_id"]), {}
            )
            role_ids = by_system.setdefault(system_id, [])
            if role_id not in role_ids:
                role_ids.append(role_id)

        profiles: list[dict[str, Any]] = []
        for row in profiles_raw:
            profile_db_id = int(row["id"])
            profile_id = _stable_id(
                "profile",
                str(row["profile_code"] or ""),
                str(row["profile_name"]),
            )
            profiles.append(
                {
                    "id": profile_id,
                    "name": str(row["profile_name"]),
                    "city": cities_by_profile.get(profile_db_id, ""),
                    "department_ids": department_ids_by_profile.get(
                        profile_db_id, []
                    ),
                    "position_ids": position_ids_by_profile.get(profile_db_id, []),
                    "access": [
                        {
                            "system_id": system_id,
                            "role_ids": role_ids,
                        }
                        for system_id, role_ids in profile_access.get(
                            profile_db_id, {}
                        ).items()
                    ],
                }
            )

        version = (
            f"v1-snapshot-{snapshot_id}-"
            f"{normalize_query(source_sha256).replace(' ', '')[:12]}"
        )
        document = {
            "version": version,
            "departments": list(departments_by_id.values()),
            "positions": list(positions_by_id.values()),
            "systems": systems,
            "profiles": profiles,
            "instructions": [],
        }
        counts = {
            "departments": len(document["departments"]),
            "positions": len(document["positions"]),
            "systems": len(systems),
            "roles": sum(len(item["roles"]) for item in systems),
            "profiles": len(profiles),
            "instructions": 0,
        }
        return ExtractedCatalog(
            document=document,
            source_sha256=source_sha256,
            snapshot_id=snapshot_id,
            counts=counts,
        )


def _stable_id(prefix: str, code: str, fallback: str) -> str:
    normalized_code = normalize_query(code).replace(" ", "-")
    if normalized_code:
        return f"{prefix}:{normalized_code}"
    digest = hashlib.sha256(
        normalize_query(fallback).encode("utf-8")
    ).hexdigest()[:20]
    return f"{prefix}:sha256:{digest}"


def _extract_city(value: str) -> str:
    match = _CITY_PATTERN.search(value)
    if not match:
        return ""
    city = match.group(1).strip("-")
    return "-".join(part.capitalize() for part in city.split("-"))


def _append_unique(
    mapping: dict[int, list[str]],
    key: int,
    value: str,
) -> None:
    values = mapping.setdefault(key, [])
    if value not in values:
        values.append(value)
