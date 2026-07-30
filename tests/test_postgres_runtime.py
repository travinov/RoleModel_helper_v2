from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import psycopg
except ModuleNotFoundError as exc:  # The default system Python has no project extras.
    raise unittest.SkipTest("psycopg is not installed for this interpreter") from exc

from app.agent.service import AgentEngine
from app.bootstrap import build_runtime
from app.catalog.cache import CatalogBundle, CatalogCache
from app.catalog.postgres import PostgresCatalogMigrator, PostgresCatalogPublisher
from app.config import Settings
from app.runtime.postgres import (
    PostgresMigrator,
    PostgresStateStore,
    SchemaNotReady,
)
from app.runtime.telemetry import PostgresTurnMetrics
from app.runtime.service import RuntimeService, StateRevisionConflict
from tests.fakes import RecordingPlanner, VersionedCatalogSource, load_catalog_mapping
from tests.postgres_harness import TemporaryPostgres


def make_engine() -> AgentEngine:
    source = VersionedCatalogSource(
        {"v42": load_catalog_mapping("catalog_v42.json")},
        bundle_factory=CatalogBundle.from_mapping,
    )
    cache = CatalogCache(source)
    cache.refresh("v42")
    return AgentEngine(
        catalog_cache=cache,
        planner=RecordingPlanner(error=AssertionError("fast path called GigaChat")),
    )


class PostgresRuntimeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.postgres = TemporaryPostgres()
        with psycopg.connect(cls.postgres.dsn) as connection:
            connection.execute("CREATE ROLE rmv2_app LOGIN")
        cls.app_dsn = (
            f"postgresql://rmv2_app@127.0.0.1:{cls.postgres.port}/postgres"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.postgres.close()

    def setUp(self) -> None:
        self.schema = f"rolemodel_v2_runtime_{self._testMethodName[-24:]}"
        self.catalog_schema = f"rolemodel_v2_catalog_{self._testMethodName[-24:]}"
        with psycopg.connect(self.postgres.dsn) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS public.v1_sentinel (
                    id INTEGER PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO public.v1_sentinel(id, value)
                VALUES (1, 'unchanged')
                ON CONFLICT (id) DO UPDATE SET value = EXCLUDED.value
                """
            )

    def test_runtime_fails_closed_before_migration(self) -> None:
        store = PostgresStateStore(dsn=self.app_dsn, schema=self.schema)

        with self.assertRaises(SchemaNotReady):
            store.verify_ready()

    def test_migration_and_dialogue_writes_stay_inside_v2_schema(self) -> None:
        before = self._sentinel()
        migrator = PostgresMigrator(
            dsn=self.postgres.dsn,
            schema=self.schema,
            v1_schema="public",
            app_role="rmv2_app",
        )

        result = migrator.migrate()

        self.assertEqual(result.current_version, 2)
        self.assertEqual(result.applied_versions, (1, 2))
        store = PostgresStateStore(dsn=self.app_dsn, schema=self.schema)
        store.verify_ready()
        runtime = RuntimeService(state_store=store, engine=make_engine())
        created = runtime.create_session(session_id="session-postgres")
        self.assertEqual(created["state"]["revision"], 0)

        first = runtime.post_message(
            session_id="session-postgres",
            request_id="req-postgres",
            text="Покажи роли в АС СберДруг",
            state_revision=0,
        )
        replay = runtime.post_message(
            session_id="session-postgres",
            request_id="req-postgres",
            text="Этот текст не должен заменить исходный",
            state_revision=0,
        )

        self.assertEqual(replay, first)
        self.assertEqual(first["state"]["revision"], 1)
        persisted = runtime.get_session("session-postgres")
        self.assertEqual(len(persisted["messages"]), 1)
        self.assertEqual(persisted["messages"][0]["catalog_version"], "v42")
        self.assertEqual(
            persisted["messages"][0]["trace_id"],
            first["diagnostics"]["trace_id"],
        )
        self.assertEqual(self._sentinel(), before)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with psycopg.connect(self.app_dsn) as connection:
                connection.execute("SELECT * FROM public.v1_sentinel").fetchall()
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with psycopg.connect(self.app_dsn) as connection:
                connection.execute(
                    "INSERT INTO public.v1_sentinel(id, value) VALUES (2, 'changed')"
                )
        with psycopg.connect(self.postgres.dsn) as connection:
            telemetry = connection.execute(
                f"""
                SELECT count(*), min(route), min(outcome),
                       min(gigachat_calls), min(total_ms), min(gigachat_ms)
                FROM "{self.schema}".turn_message
                """
            ).fetchone()
            indexes = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT indexname
                    FROM pg_indexes
                    WHERE schemaname = %s AND tablename = 'turn_message'
                    """,
                    (self.schema,),
                ).fetchall()
            }
        self.assertEqual(telemetry[0], 1)
        self.assertEqual(telemetry[1], "DETERMINISTIC")
        self.assertEqual(telemetry[2], "HANDLED")
        self.assertEqual(telemetry[3], 0)
        self.assertGreaterEqual(float(telemetry[4]), 0.0)
        self.assertEqual(float(telemetry[5]), 0.0)
        self.assertIn("turn_message_observability_idx", indexes)
        summary = PostgresTurnMetrics(
            dsn=self.app_dsn,
            schema=self.schema,
        ).summary(hours=24)
        self.assertEqual(summary["turns"], 1)
        self.assertEqual(summary["gigachat_calls"], 0)
        self.assertEqual(summary["routes"]["DETERMINISTIC"]["turns"], 1)
        self.assertGreaterEqual(summary["latency_ms"]["p95"], 0.0)
        with self.assertRaises(psycopg.errors.CheckViolation):
            with psycopg.connect(self.app_dsn) as connection:
                connection.execute(
                    f"""
                    UPDATE "{self.schema}".turn_message
                    SET total_ms = -1
                    WHERE request_id = 'req-postgres'
                    """
                )

    def test_stale_and_concurrent_revisions_write_only_one_turn(self) -> None:
        PostgresMigrator(
            dsn=self.postgres.dsn,
            schema=self.schema,
            v1_schema="public",
            app_role="rmv2_app",
        ).migrate()
        runtime = RuntimeService(
            state_store=PostgresStateStore(
                dsn=self.app_dsn,
                schema=self.schema,
            ),
            engine=make_engine(),
        )
        runtime.create_session(session_id="session-race")
        barrier = threading.Barrier(2)

        def post(request_id: str):
            barrier.wait(timeout=2)
            return runtime.post_message(
                session_id="session-race",
                request_id=request_id,
                text="Покажи роли в АС СберДруг",
                state_revision=0,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(post, f"req-race-{index}") for index in range(2)]
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(future.result(timeout=5))
                except StateRevisionConflict as exc:
                    outcomes.append(exc)

        successes = [item for item in outcomes if isinstance(item, dict)]
        conflicts = [item for item in outcomes if isinstance(item, StateRevisionConflict)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)
        persisted = runtime.get_session("session-race")
        self.assertEqual(persisted["state"]["revision"], 1)
        self.assertEqual(len(persisted["messages"]), 1)

    def test_production_bootstrap_uses_migrated_postgresql_store(self) -> None:
        PostgresMigrator(
            dsn=self.postgres.dsn,
            schema=self.schema,
            v1_schema="public",
            app_role="rmv2_app",
        ).migrate()
        settings = Settings.from_mapping(
            {
                "DATABASE_DSN": self.app_dsn,
                "STATE_SCHEMA": self.schema,
                "V1_STATE_SCHEMA": "public",
                "INSTALL_DIR": "/srv/RoleModelHelperV2",
                "V1_INSTALL_DIR": "/srv/RoleModelHelper",
                "CATALOG_PATH": str(
                    Path(__file__).resolve().parents[1] / "data" / "demo_catalog.json"
                ),
                "CATALOG_VERSION": "demo-v1",
                "CATALOG_BACKEND": "json",
            }
        )

        _, _, runtime = build_runtime(settings)
        created = runtime.create_session(session_id="session-bootstrap")
        response = runtime.post_message(
            session_id=created["session_id"],
            request_id="req-bootstrap",
            text="Покажи роли в Демо АС Доступ",
            state_revision=0,
        )

        self.assertEqual(response["diagnostics"]["route"], "DETERMINISTIC")
        self.assertEqual(response["state"]["revision"], 1)
        with psycopg.connect(self.postgres.dsn) as connection:
            count = connection.execute(
                f'SELECT count(*) FROM "{self.schema}".turn_message'
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_production_bootstrap_loads_active_postgresql_catalog(self) -> None:
        PostgresMigrator(
            dsn=self.postgres.dsn,
            schema=self.schema,
            v1_schema="public",
            app_role="rmv2_app",
        ).migrate()
        PostgresCatalogMigrator(
            dsn=self.postgres.dsn,
            schema=self.catalog_schema,
            v1_schema="public",
            reader_role="rmv2_app",
        ).migrate()
        PostgresCatalogPublisher(
            dsn=self.postgres.dsn,
            schema=self.catalog_schema,
        ).publish(
            load_catalog_mapping("catalog_v44_org.json"),
            source_sha256="sha256:bootstrap",
        )
        settings = Settings.from_mapping(
            {
                "DATABASE_DSN": self.app_dsn,
                "STATE_SCHEMA": self.schema,
                "CATALOG_SCHEMA": self.catalog_schema,
                "V1_STATE_SCHEMA": "public",
                "INSTALL_DIR": "/srv/RoleModelHelperV2",
                "V1_INSTALL_DIR": "/srv/RoleModelHelper",
            }
        )

        _, cache, runtime = build_runtime(settings)
        created = runtime.create_session(session_id="session-pg-catalog")
        response = runtime.post_message(
            session_id=created["session_id"],
            request_id="req-pg-catalog",
            text="Покажи роли в АС Заявки",
            state_revision=0,
        )

        self.assertEqual(cache.status.active_version, "v44-org")
        self.assertEqual(response["diagnostics"]["catalog_version"], "v44-org")
        self.assertEqual(response["diagnostics"]["route"], "DETERMINISTIC")

    def _sentinel(self) -> tuple[int, str]:
        with psycopg.connect(self.postgres.dsn) as connection:
            row = connection.execute(
                "SELECT id, value FROM public.v1_sentinel WHERE id = 1"
            ).fetchone()
        return int(row[0]), str(row[1])


if __name__ == "__main__":
    unittest.main()
