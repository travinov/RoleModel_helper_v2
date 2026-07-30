from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.catalog.cache import CatalogBundle
from app.catalog.normalization import normalize_query, parse_department_number


LATEST_CATALOG_SCHEMA_VERSION = 1


class CatalogValidationError(ValueError):
    pass


class CatalogSchemaNotReady(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogMigrationResult:
    current_version: int
    applied_versions: tuple[int, ...]


@dataclass(frozen=True)
class CatalogPublicationResult:
    version: str
    previous_version: str | None
    counts: Mapping[str, int]


class PostgresCatalogMigrator:
    """Creates only the isolated, versioned V2 catalog schema."""

    def __init__(
        self,
        *,
        dsn: str,
        schema: str,
        v1_schema: str,
        reader_role: str | None = None,
        writer_role: str | None = None,
    ) -> None:
        self._dsn = dsn
        self._schema = schema
        self._v1_schema = v1_schema
        self._reader_role = reader_role
        self._writer_role = writer_role

    def migrate(self) -> CatalogMigrationResult:
        if not self._schema.strip():
            raise ValueError("V2 catalog schema must not be empty")
        if self._schema == self._v1_schema:
            raise ValueError("V2 catalog schema must differ from V1 schema")

        applied: list[int] = []
        with psycopg.connect(self._dsn) as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"rolemodel-helper-v2:{self._schema}:catalog-migrations",),
            )
            schema = sql.Identifier(self._schema)
            connection.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(schema)
            )
            connection.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {}.schema_migration (
                        version INTEGER PRIMARY KEY,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                ).format(schema)
            )
            existing = {
                int(row[0])
                for row in connection.execute(
                    sql.SQL("SELECT version FROM {}.schema_migration").format(schema)
                ).fetchall()
            }
            unsupported = [
                version
                for version in existing
                if version > LATEST_CATALOG_SCHEMA_VERSION
            ]
            if unsupported:
                raise CatalogSchemaNotReady(
                    "Catalog schema is newer than this application: "
                    f"{max(unsupported)}"
                )
            if 1 not in existing:
                self._apply_version_1(connection)
                connection.execute(
                    sql.SQL(
                        "INSERT INTO {}.schema_migration(version) VALUES (1)"
                    ).format(schema)
                )
                applied.append(1)
            self._grant_permissions(connection)

        return CatalogMigrationResult(
            current_version=LATEST_CATALOG_SCHEMA_VERSION,
            applied_versions=tuple(applied),
        )

    def _apply_version_1(self, connection: psycopg.Connection[Any]) -> None:
        schema = sql.Identifier(self._schema)
        connection.execute(
            sql.SQL(
                """
                CREATE TABLE {}.catalog_release (
                    version TEXT PRIMARY KEY,
                    source_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('STAGING', 'ACTIVE', 'RETIRED')),
                    counts_json JSONB NOT NULL
                        CHECK (jsonb_typeof(counts_json) = 'object'),
                    document_json JSONB NOT NULL
                        CHECK (jsonb_typeof(document_json) = 'object'),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    activated_at TIMESTAMPTZ
                )
                """
            ).format(schema)
        )
        connection.execute(
            sql.SQL(
                """
                CREATE UNIQUE INDEX catalog_release_one_active_idx
                ON {}.catalog_release ((status))
                WHERE status = 'ACTIVE'
                """
            ).format(schema)
        )
        connection.execute(
            sql.SQL(
                """
                CREATE TABLE {}.catalog_pointer (
                    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE
                        CHECK (singleton),
                    active_version TEXT NOT NULL
                        REFERENCES {}.catalog_release(version)
                )
                """
            ).format(schema, schema)
        )
        connection.execute(
            sql.SQL(
                """
                CREATE TABLE {}.catalog_entity (
                    release_version TEXT NOT NULL
                        REFERENCES {}.catalog_release(version) ON DELETE CASCADE,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    raw_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    entity_kind TEXT NOT NULL,
                    entity_number INTEGER,
                    normalized_city TEXT NOT NULL DEFAULT '',
                    parent_id TEXT,
                    payload JSONB NOT NULL
                        CHECK (jsonb_typeof(payload) = 'object'),
                    PRIMARY KEY (release_version, entity_type, entity_id)
                )
                """
            ).format(schema, schema)
        )
        connection.execute(
            sql.SQL(
                """
                CREATE INDEX catalog_entity_lookup_idx
                ON {}.catalog_entity(
                    release_version, entity_type, entity_number, normalized_city
                )
                """
            ).format(schema)
        )
        connection.execute(
            sql.SQL(
                """
                CREATE TABLE {}.catalog_alias (
                    release_version TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    alias_raw TEXT NOT NULL,
                    alias_normalized TEXT NOT NULL,
                    safety TEXT NOT NULL
                        CHECK (safety IN ('SAFE', 'AMBIGUOUS')),
                    collision_count INTEGER NOT NULL CHECK (collision_count >= 1),
                    PRIMARY KEY (
                        release_version, entity_type, entity_id, alias_normalized
                    ),
                    FOREIGN KEY (release_version, entity_type, entity_id)
                        REFERENCES {}.catalog_entity(
                            release_version, entity_type, entity_id
                        ) ON DELETE CASCADE
                )
                """
            ).format(schema, schema)
        )
        for relation, target_column in (
            ("profile_department", "department_id"),
            ("profile_position", "position_id"),
            ("profile_system", "system_id"),
            ("profile_role", "role_id"),
        ):
            connection.execute(
                sql.SQL(
                    """
                    CREATE TABLE {}.{} (
                        release_version TEXT NOT NULL
                            REFERENCES {}.catalog_release(version)
                            ON DELETE CASCADE,
                        profile_id TEXT NOT NULL,
                        {} TEXT NOT NULL,
                        PRIMARY KEY (release_version, profile_id, {})
                    )
                    """
                ).format(
                    schema,
                    sql.Identifier(relation),
                    schema,
                    sql.Identifier(target_column),
                    sql.Identifier(target_column),
                )
            )

    def _grant_permissions(self, connection: psycopg.Connection[Any]) -> None:
        schema = sql.Identifier(self._schema)
        if self._reader_role:
            reader = sql.Identifier(self._reader_role)
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, reader)
            )
            connection.execute(
                sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                    schema, reader
                )
            )
        if self._writer_role:
            writer = sql.Identifier(self._writer_role)
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, writer)
            )
            connection.execute(
                sql.SQL(
                    """
                    GRANT SELECT, INSERT, UPDATE, DELETE
                    ON ALL TABLES IN SCHEMA {} TO {}
                    """
                ).format(schema, writer)
            )
            connection.execute(
                sql.SQL(
                    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}"
                ).format(schema, writer)
            )


class PostgresCatalogPublisher:
    """Validates and atomically activates an immutable catalog release."""

    def __init__(self, *, dsn: str, schema: str) -> None:
        self._dsn = dsn
        self._schema = schema

    def publish(
        self,
        payload: Mapping[str, Any],
        *,
        source_sha256: str,
    ) -> CatalogPublicationResult:
        document, bundle, counts = _validate_catalog(payload)
        version = bundle.version
        schema = sql.Identifier(self._schema)

        try:
            with psycopg.connect(self._dsn) as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"rolemodel-helper-v2:{self._schema}:catalog-publication",),
                )
                previous_row = connection.execute(
                    sql.SQL(
                        """
                        SELECT active_version
                        FROM {}.catalog_pointer
                        WHERE singleton = TRUE
                        FOR UPDATE
                        """
                    ).format(schema)
                ).fetchone()
                previous = str(previous_row[0]) if previous_row else None
                connection.execute(
                    sql.SQL(
                        """
                        INSERT INTO {}.catalog_release(
                            version, source_sha256, status,
                            counts_json, document_json
                        )
                        VALUES (%s, %s, 'STAGING', %s, %s)
                        """
                    ).format(schema),
                    (
                        version,
                        source_sha256,
                        Jsonb(dict(counts)),
                        Jsonb(document),
                    ),
                )
                self._insert_entities(connection, document, bundle)
                self._insert_aliases(connection, document, bundle)
                self._insert_relations(connection, document, bundle)
                if previous is not None:
                    connection.execute(
                        sql.SQL(
                            """
                            UPDATE {}.catalog_release
                            SET status = 'RETIRED'
                            WHERE version = %s AND status = 'ACTIVE'
                            """
                        ).format(schema),
                        (previous,),
                    )
                connection.execute(
                    sql.SQL(
                        """
                        UPDATE {}.catalog_release
                        SET status = 'ACTIVE', activated_at = now()
                        WHERE version = %s AND status = 'STAGING'
                        """
                    ).format(schema),
                    (version,),
                )
                connection.execute(
                    sql.SQL(
                        """
                        INSERT INTO {}.catalog_pointer(singleton, active_version)
                        VALUES (TRUE, %s)
                        ON CONFLICT (singleton)
                        DO UPDATE SET active_version = EXCLUDED.active_version
                        """
                    ).format(schema),
                    (version,),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise CatalogValidationError(
                f"Catalog version {version!r} already exists"
            ) from exc

        return CatalogPublicationResult(
            version=version,
            previous_version=previous,
            counts=counts,
        )

    def _insert_entities(
        self,
        connection: psycopg.Connection[Any],
        document: Mapping[str, Any],
        bundle: CatalogBundle,
    ) -> None:
        rows: list[tuple[Any, ...]] = []
        for department in bundle.departments:
            rows.append(
                _entity_row(
                    bundle.version,
                    "department",
                    department["id"],
                    department["name"],
                    "department",
                    department,
                    number=department["number"],
                    city=department["city"],
                )
            )
        for position in bundle.positions:
            rows.append(
                _entity_row(
                    bundle.version,
                    "position",
                    position["id"],
                    position["name"],
                    "position",
                    position,
                )
            )
        for system in bundle.systems.values():
            rows.append(
                _entity_row(
                    bundle.version,
                    "system",
                    system["id"],
                    system["name"],
                    "system",
                    system,
                )
            )
            for role in system["roles"]:
                rows.append(
                    _entity_row(
                        bundle.version,
                        "role",
                        role["id"],
                        role["name"],
                        "role",
                        role,
                        parent_id=system["id"],
                    )
                )
        for profile in bundle.profiles.values():
            rows.append(
                _entity_row(
                    bundle.version,
                    "profile",
                    profile["id"],
                    profile["name"],
                    "profile",
                    profile,
                    city=profile["city"],
                )
            )
        for instruction in document.get("instructions") or []:
            rows.append(
                _entity_row(
                    bundle.version,
                    "instruction",
                    instruction["id"],
                    instruction["title"],
                    "instruction",
                    instruction,
                    parent_id=instruction["system_id"],
                )
            )
        with connection.cursor() as cursor:
            cursor.executemany(
                sql.SQL(
                    """
                    INSERT INTO {}.catalog_entity(
                        release_version, entity_type, entity_id, raw_name,
                        normalized_name, entity_kind, entity_number,
                        normalized_city, parent_id, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """
                ).format(sql.Identifier(self._schema)),
                rows,
            )

    def _insert_aliases(
        self,
        connection: psycopg.Connection[Any],
        document: Mapping[str, Any],
        bundle: CatalogBundle,
    ) -> None:
        aliases: list[tuple[str, str, str, str, str, str]] = []
        for entity_type, entities in (
            ("department", document.get("departments") or []),
            ("position", document.get("positions") or []),
            ("system", document.get("systems") or []),
        ):
            for entity in entities:
                for alias in entity.get("aliases") or []:
                    raw = str(alias["value"]).strip()
                    normalized = normalize_query(raw)
                    aliases.append(
                        (
                            bundle.version,
                            entity_type,
                            str(entity["id"]),
                            raw,
                            normalized,
                            str(alias.get("safety") or "AMBIGUOUS").upper(),
                        )
                    )
        collision_counts = Counter(
            (entity_type, normalized)
            for _, entity_type, _, _, normalized, _ in aliases
        )
        rows = [
            (
                version,
                entity_type,
                entity_id,
                raw,
                normalized,
                "AMBIGUOUS"
                if collision_counts[(entity_type, normalized)] > 1
                else safety,
                collision_counts[(entity_type, normalized)],
            )
            for version, entity_type, entity_id, raw, normalized, safety in aliases
        ]
        if rows:
            with connection.cursor() as cursor:
                cursor.executemany(
                    sql.SQL(
                        """
                        INSERT INTO {}.catalog_alias(
                            release_version, entity_type, entity_id, alias_raw,
                            alias_normalized, safety, collision_count
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """
                    ).format(sql.Identifier(self._schema)),
                    rows,
                )

    def _insert_relations(
        self,
        connection: psycopg.Connection[Any],
        document: Mapping[str, Any],
        bundle: CatalogBundle,
    ) -> None:
        relations: dict[str, list[tuple[str, str, str]]] = {
            "profile_department": [],
            "profile_position": [],
            "profile_system": [],
            "profile_role": [],
        }
        for profile in document.get("profiles") or []:
            profile_id = str(profile["id"])
            for department_id in profile.get("department_ids") or []:
                relations["profile_department"].append(
                    (bundle.version, profile_id, str(department_id))
                )
            for position_id in profile.get("position_ids") or []:
                relations["profile_position"].append(
                    (bundle.version, profile_id, str(position_id))
                )
            for access in profile.get("access") or []:
                relations["profile_system"].append(
                    (bundle.version, profile_id, str(access["system_id"]))
                )
                for role_id in access.get("role_ids") or []:
                    relations["profile_role"].append(
                        (bundle.version, profile_id, str(role_id))
                    )
        for table, rows in relations.items():
            if not rows:
                continue
            target_column = {
                "profile_department": "department_id",
                "profile_position": "position_id",
                "profile_system": "system_id",
                "profile_role": "role_id",
            }[table]
            with connection.cursor() as cursor:
                cursor.executemany(
                    sql.SQL(
                        "INSERT INTO {}.{}(release_version, profile_id, {}) "
                        "VALUES (%s, %s, %s)"
                    ).format(
                        sql.Identifier(self._schema),
                        sql.Identifier(table),
                        sql.Identifier(target_column),
                    ),
                    rows,
                )


class PostgresCatalogSource:
    """Read-only source that reconstructs immutable bundles from releases."""

    def __init__(self, *, dsn: str, schema: str) -> None:
        self._dsn = dsn
        self._schema = schema

    def verify_ready(self) -> None:
        try:
            with psycopg.connect(self._dsn) as connection:
                row = connection.execute(
                    sql.SQL("SELECT max(version) FROM {}.schema_migration").format(
                        sql.Identifier(self._schema)
                    )
                ).fetchone()
        except (psycopg.errors.InvalidSchemaName, psycopg.errors.UndefinedTable) as exc:
            raise CatalogSchemaNotReady(
                f"PostgreSQL catalog schema {self._schema!r} is not migrated"
            ) from exc
        version = int(row[0]) if row and row[0] is not None else 0
        if version != LATEST_CATALOG_SCHEMA_VERSION:
            raise CatalogSchemaNotReady(
                f"PostgreSQL catalog schema version {version}, "
                f"expected {LATEST_CATALOG_SCHEMA_VERSION}"
            )

    def active_version(self) -> str | None:
        with psycopg.connect(self._dsn) as connection:
            row = connection.execute(
                sql.SQL(
                    """
                    SELECT active_version
                    FROM {}.catalog_pointer
                    WHERE singleton = TRUE
                    """
                ).format(sql.Identifier(self._schema))
            ).fetchone()
        return str(row[0]) if row else None

    def load(self, version: str) -> CatalogBundle:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            row = connection.execute(
                sql.SQL(
                    """
                    SELECT document_json
                    FROM {}.catalog_release
                    WHERE version = %s AND status IN ('ACTIVE', 'RETIRED')
                    """
                ).format(sql.Identifier(self._schema)),
                (version,),
            ).fetchone()
        if row is None:
            raise KeyError(version)
        return CatalogBundle.from_mapping(dict(row["document_json"]))


def _entity_row(
    version: str,
    entity_type: str,
    entity_id: Any,
    raw_name: Any,
    entity_kind: str,
    payload: Mapping[str, Any],
    *,
    number: int | None = None,
    city: Any = "",
    parent_id: Any = None,
) -> tuple[Any, ...]:
    raw = str(raw_name)
    return (
        version,
        entity_type,
        str(entity_id),
        raw,
        normalize_query(raw),
        entity_kind,
        number,
        normalize_query(str(city or "")),
        str(parent_id) if parent_id is not None else None,
        Jsonb(_plain_value(payload)),
    )


def _plain_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_plain_value(item) for item in value)
    return value


def _validate_catalog(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], CatalogBundle, dict[str, int]]:
    document = _plain_value(payload)
    if not isinstance(document, dict):
        raise CatalogValidationError("Catalog document must be an object")
    version = str(document.get("version") or "").strip()
    if not version:
        raise CatalogValidationError("Catalog version is required")
    document["version"] = version

    collections = {
        "departments": document.get("departments") or [],
        "positions": document.get("positions") or [],
        "systems": document.get("systems") or [],
        "profiles": document.get("profiles") or [],
        "instructions": document.get("instructions") or [],
    }
    for name, items in collections.items():
        if not isinstance(items, list):
            raise CatalogValidationError(f"{name} must be a list")
        _validate_unique_required(items, name)

    department_ids = _ids(collections["departments"])
    position_ids = _ids(collections["positions"])
    system_ids = _ids(collections["systems"])
    role_to_system: dict[str, str] = {}
    role_count = 0
    for system in collections["systems"]:
        roles = system.get("roles") or []
        if not isinstance(roles, list):
            raise CatalogValidationError(
                f"roles for system {system['id']!r} must be a list"
            )
        _validate_unique_required(roles, f"roles of system {system['id']}")
        for role in roles:
            role_id = str(role["id"])
            if role_id in role_to_system:
                raise CatalogValidationError(f"Duplicate role id {role_id!r}")
            role_to_system[role_id] = str(system["id"])
            role_count += 1

    for profile in collections["profiles"]:
        profile_id = str(profile["id"])
        _require_references(
            profile.get("department_ids") or [],
            department_ids,
            f"profile {profile_id} department",
        )
        _require_references(
            profile.get("position_ids") or [],
            position_ids,
            f"profile {profile_id} position",
        )
        for access in profile.get("access") or []:
            system_id = str(access.get("system_id") or "")
            if system_id not in system_ids:
                raise CatalogValidationError(
                    f"profile {profile_id} references unknown system {system_id!r}"
                )
            for raw_role_id in access.get("role_ids") or []:
                role_id = str(raw_role_id)
                if role_to_system.get(role_id) != system_id:
                    raise CatalogValidationError(
                        f"profile {profile_id} role {role_id!r} "
                        f"does not belong to system {system_id!r}"
                    )
    for instruction in collections["instructions"]:
        system_id = str(instruction.get("system_id") or "")
        if system_id not in system_ids:
            raise CatalogValidationError(
                f"instruction {instruction['id']} references unknown "
                f"system {system_id!r}"
            )

    _validate_aliases(
        (
            ("departments", collections["departments"]),
            ("positions", collections["positions"]),
            ("systems", collections["systems"]),
        )
    )
    try:
        bundle = CatalogBundle.from_mapping(document)
    except (KeyError, TypeError, ValueError) as exc:
        raise CatalogValidationError(f"Invalid catalog document: {exc}") from exc
    counts = {
        "departments": len(collections["departments"]),
        "positions": len(collections["positions"]),
        "systems": len(collections["systems"]),
        "roles": role_count,
        "profiles": len(collections["profiles"]),
        "instructions": len(collections["instructions"]),
    }
    return document, bundle, counts


def _validate_unique_required(
    items: Iterable[Mapping[str, Any]],
    collection_name: str,
) -> None:
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise CatalogValidationError(f"{collection_name} entries must be objects")
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            raise CatalogValidationError(f"{collection_name} id is required")
        display_name = item.get("name")
        if collection_name == "instructions":
            display_name = item.get("title")
        if not str(display_name or "").strip():
            raise CatalogValidationError(
                f"{collection_name} {item_id!r} display name is required"
            )
        if item_id in seen:
            raise CatalogValidationError(
                f"Duplicate id {item_id!r} in {collection_name}"
            )
        seen.add(item_id)


def _ids(items: Iterable[Mapping[str, Any]]) -> set[str]:
    return {str(item["id"]) for item in items}


def _require_references(
    values: Iterable[Any],
    allowed: set[str],
    label: str,
) -> None:
    for value in values:
        normalized = str(value)
        if normalized not in allowed:
            raise CatalogValidationError(
                f"{label} references unknown id {normalized!r}"
            )


def _validate_aliases(
    collections: Iterable[tuple[str, Iterable[Mapping[str, Any]]]],
) -> None:
    for collection_name, items in collections:
        for item in items:
            for alias in item.get("aliases") or []:
                if not isinstance(alias, Mapping):
                    raise CatalogValidationError(
                        f"{collection_name} aliases must be objects"
                    )
                value = str(alias.get("value") or "").strip()
                if not normalize_query(value):
                    raise CatalogValidationError(
                        f"{collection_name} alias value is required"
                    )
                safety = str(alias.get("safety") or "AMBIGUOUS").upper()
                if safety not in {"SAFE", "AMBIGUOUS"}:
                    raise CatalogValidationError(
                        f"Unsupported alias safety {safety!r}"
                    )
