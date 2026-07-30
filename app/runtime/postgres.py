from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.runtime.service import SessionNotFound, StateRevisionConflict


LATEST_SCHEMA_VERSION = 2


class SchemaNotReady(RuntimeError):
    pass


@dataclass(frozen=True)
class MigrationResult:
    current_version: int
    applied_versions: tuple[int, ...]


class PostgresMigrator:
    """Runs isolated V2 DDL with a transaction-scoped advisory lock."""

    def __init__(
        self,
        *,
        dsn: str,
        schema: str,
        v1_schema: str,
        app_role: str | None = None,
    ) -> None:
        self._dsn = dsn
        self._schema = schema
        self._v1_schema = v1_schema
        self._app_role = app_role

    def migrate(self) -> MigrationResult:
        if not self._schema.strip():
            raise ValueError("V2 runtime schema must not be empty")
        if self._schema == self._v1_schema:
            raise ValueError("V2 runtime schema must differ from V1 schema")

        applied_now: list[int] = []
        with psycopg.connect(self._dsn) as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"rolemodel-helper-v2:{self._schema}:migrations",),
            )
            connection.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    sql.Identifier(self._schema)
                )
            )
            connection.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {}.schema_migration (
                        version INTEGER PRIMARY KEY,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                ).format(sql.Identifier(self._schema))
            )
            existing = {
                int(row[0])
                for row in connection.execute(
                    sql.SQL("SELECT version FROM {}.schema_migration").format(
                        sql.Identifier(self._schema)
                    )
                ).fetchall()
            }
            unsupported = [version for version in existing if version > LATEST_SCHEMA_VERSION]
            if unsupported:
                raise SchemaNotReady(
                    f"Database schema is newer than this application: {max(unsupported)}"
                )
            if 1 not in existing:
                self._apply_version_1(connection)
                connection.execute(
                    sql.SQL(
                        "INSERT INTO {}.schema_migration(version) VALUES (1)"
                    ).format(sql.Identifier(self._schema))
                )
                applied_now.append(1)
            if 2 not in existing:
                self._apply_version_2(connection)
                connection.execute(
                    sql.SQL(
                        "INSERT INTO {}.schema_migration(version) VALUES (2)"
                    ).format(sql.Identifier(self._schema))
                )
                applied_now.append(2)
            if self._app_role:
                self._grant_runtime_permissions(connection, self._app_role)

        return MigrationResult(
            current_version=LATEST_SCHEMA_VERSION,
            applied_versions=tuple(applied_now),
        )

    def _grant_runtime_permissions(
        self,
        connection: psycopg.Connection[Any],
        app_role: str,
    ) -> None:
        schema = sql.Identifier(self._schema)
        role = sql.Identifier(app_role)
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, role)
        )
        connection.execute(
            sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}"
            ).format(schema, role)
        )
        connection.execute(
            sql.SQL(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}"
            ).format(schema, role)
        )

    def _apply_version_1(self, connection: psycopg.Connection[Any]) -> None:
        schema = sql.Identifier(self._schema)
        connection.execute(
            sql.SQL(
                """
                CREATE TABLE {}.session_state (
                    session_id TEXT PRIMARY KEY,
                    revision BIGINT NOT NULL CHECK (revision >= 0),
                    state_json JSONB NOT NULL
                        CHECK (jsonb_typeof(state_json) = 'object'),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            ).format(schema)
        )
        connection.execute(
            sql.SQL(
                """
                CREATE TABLE {}.turn_message (
                    id BIGSERIAL PRIMARY KEY,
                    session_id TEXT NOT NULL
                        REFERENCES {}.session_state(session_id) ON DELETE CASCADE,
                    request_id TEXT NOT NULL,
                    user_text TEXT NOT NULL,
                    response_json JSONB NOT NULL
                        CHECK (jsonb_typeof(response_json) = 'object'),
                    catalog_version TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (session_id, request_id)
                )
                """
            ).format(schema, schema)
        )
        connection.execute(
            sql.SQL(
                """
                CREATE INDEX turn_message_session_created_idx
                ON {}.turn_message(session_id, created_at, id)
                """
            ).format(schema)
        )

    def _apply_version_2(self, connection: psycopg.Connection[Any]) -> None:
        schema = sql.Identifier(self._schema)
        connection.execute(
            sql.SQL(
                """
                ALTER TABLE {}.turn_message
                    ADD COLUMN route TEXT,
                    ADD COLUMN outcome TEXT,
                    ADD COLUMN gigachat_calls SMALLINT,
                    ADD COLUMN total_ms DOUBLE PRECISION,
                    ADD COLUMN gigachat_ms DOUBLE PRECISION
                """
            ).format(schema)
        )
        connection.execute(
            sql.SQL(
                """
                UPDATE {}.turn_message
                SET route = coalesce(
                        response_json #>> '{{diagnostics,route}}', 'DETERMINISTIC'
                    ),
                    outcome = coalesce(
                        response_json #>> '{{diagnostics,outcome}}', 'HANDLED'
                    ),
                    gigachat_calls = coalesce(
                        (response_json #>> '{{diagnostics,gigachat_calls}}')::SMALLINT,
                        0
                    ),
                    total_ms = coalesce(
                        (response_json #>> '{{diagnostics,durations_ms,total}}')::DOUBLE PRECISION,
                        0
                    ),
                    gigachat_ms = coalesce(
                        (response_json #>> '{{diagnostics,durations_ms,gigachat}}')::DOUBLE PRECISION,
                        0
                    )
                """
            ).format(schema)
        )
        connection.execute(
            sql.SQL(
                """
                ALTER TABLE {}.turn_message
                    ALTER COLUMN route SET NOT NULL,
                    ALTER COLUMN outcome SET NOT NULL,
                    ALTER COLUMN gigachat_calls SET NOT NULL,
                    ALTER COLUMN total_ms SET NOT NULL,
                    ALTER COLUMN gigachat_ms SET NOT NULL,
                    ADD CONSTRAINT turn_message_route_check
                        CHECK (route IN ('DETERMINISTIC', 'GIGACHAT_FALLBACK')),
                    ADD CONSTRAINT turn_message_outcome_check
                        CHECK (
                            outcome IN (
                                'HANDLED',
                                'NEEDS_CLARIFICATION',
                                'PROVIDER_FAILURE'
                            )
                        ),
                    ADD CONSTRAINT turn_message_gigachat_calls_check
                        CHECK (gigachat_calls BETWEEN 0 AND 1),
                    ADD CONSTRAINT turn_message_total_ms_check
                        CHECK (total_ms >= 0),
                    ADD CONSTRAINT turn_message_gigachat_ms_check
                        CHECK (gigachat_ms >= 0)
                """
            ).format(schema)
        )
        connection.execute(
            sql.SQL(
                """
                CREATE INDEX turn_message_observability_idx
                ON {}.turn_message(created_at, route, outcome)
                """
            ).format(schema)
        )


class PostgresStateStore:
    """PostgreSQL persistence for V2 state; all identifiers remain schema-qualified."""

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
            raise SchemaNotReady(
                f"PostgreSQL schema {self._schema!r} is not migrated"
            ) from exc
        version = int(row[0]) if row and row[0] is not None else 0
        if version != LATEST_SCHEMA_VERSION:
            raise SchemaNotReady(
                f"PostgreSQL schema version {version}, expected {LATEST_SCHEMA_VERSION}"
            )

    def create_session(self, *, session_id: str, state: dict[str, Any]) -> None:
        with psycopg.connect(self._dsn) as connection:
            connection.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.session_state(session_id, revision, state_json)
                    VALUES (%s, %s, %s)
                    """
                ).format(sql.Identifier(self._schema)),
                (session_id, int(state["revision"]), Jsonb(state)),
            )

    def get_state(self, session_id: str) -> dict[str, Any] | None:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            row = connection.execute(
                sql.SQL(
                    "SELECT state_json FROM {}.session_state WHERE session_id = %s"
                ).format(sql.Identifier(self._schema)),
                (session_id,),
            ).fetchone()
        return dict(row["state_json"]) if row is not None else None

    def get_replay(self, *, session_id: str, request_id: str) -> dict[str, Any] | None:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            row = connection.execute(
                sql.SQL(
                    """
                    SELECT response_json
                    FROM {}.turn_message
                    WHERE session_id = %s AND request_id = %s
                    """
                ).format(sql.Identifier(self._schema)),
                (session_id, request_id),
            ).fetchone()
        return dict(row["response_json"]) if row is not None else None

    def get_session(self, session_id: str) -> dict[str, Any]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            session = connection.execute(
                sql.SQL(
                    "SELECT state_json FROM {}.session_state WHERE session_id = %s"
                ).format(sql.Identifier(self._schema)),
                (session_id,),
            ).fetchone()
            if session is None:
                raise SessionNotFound(session_id)
            rows = connection.execute(
                sql.SQL(
                    """
                    SELECT request_id, user_text, response_json,
                           catalog_version, trace_id, created_at
                    FROM {}.turn_message
                    WHERE session_id = %s
                    ORDER BY id
                    """
                ).format(sql.Identifier(self._schema)),
                (session_id,),
            ).fetchall()
        messages = []
        for row in rows:
            response = dict(row["response_json"])
            messages.append(
                {
                    "request_id": row["request_id"],
                    "user_text": row["user_text"],
                    "assistant": response.get("assistant"),
                    "catalog_version": row["catalog_version"],
                    "trace_id": row["trace_id"],
                    "created_at": row["created_at"].isoformat(),
                }
            )
        return {
            "session_id": session_id,
            "state": dict(session["state_json"]),
            "messages": messages,
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
        schema = sql.Identifier(self._schema)
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            session = connection.execute(
                sql.SQL(
                    """
                    SELECT revision
                    FROM {}.session_state
                    WHERE session_id = %s
                    FOR UPDATE
                    """
                ).format(schema),
                (session_id,),
            ).fetchone()
            if session is None:
                raise SessionNotFound(session_id)

            replay = connection.execute(
                sql.SQL(
                    """
                    SELECT response_json
                    FROM {}.turn_message
                    WHERE session_id = %s AND request_id = %s
                    """
                ).format(schema),
                (session_id, request_id),
            ).fetchone()
            if replay is not None:
                return dict(replay["response_json"])

            actual_revision = int(session["revision"])
            if actual_revision != expected_revision:
                raise StateRevisionConflict(
                    f"Expected state revision {actual_revision}, got {expected_revision}"
                )
            next_revision = int(state["revision"])
            if next_revision not in {expected_revision, expected_revision + 1}:
                raise StateRevisionConflict(
                    "Invalid next state revision "
                    f"{next_revision}, expected {expected_revision} or {expected_revision + 1}"
                )

            diagnostics = dict(response.get("diagnostics") or {})
            durations = dict(diagnostics.get("durations_ms") or {})
            connection.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.turn_message(
                        session_id, request_id, user_text, response_json,
                        catalog_version, trace_id, route, outcome,
                        gigachat_calls, total_ms, gigachat_ms
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """
                ).format(schema),
                (
                    session_id,
                    request_id,
                    user_text,
                    Jsonb(response),
                    catalog_version,
                    trace_id,
                    str(diagnostics["route"]),
                    str(diagnostics["outcome"]),
                    int(diagnostics["gigachat_calls"]),
                    float(durations["total"]),
                    float(durations["gigachat"]),
                ),
            )
            updated = connection.execute(
                sql.SQL(
                    """
                    UPDATE {}.session_state
                    SET revision = %s, state_json = %s, updated_at = now()
                    WHERE session_id = %s AND revision = %s
                    """
                ).format(schema),
                (
                    next_revision,
                    Jsonb(state),
                    session_id,
                    expected_revision,
                ),
            )
            if updated.rowcount != 1:
                raise StateRevisionConflict(
                    f"Expected state revision {expected_revision} changed during commit"
                )
        return response
