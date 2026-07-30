from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

try:
    import psycopg
except ModuleNotFoundError as exc:
    raise unittest.SkipTest("psycopg is not installed for this interpreter") from exc

from app.api import create_app
from app.bootstrap import build_runtime
from app.catalog.postgres import PostgresCatalogMigrator, PostgresCatalogPublisher
from app.config import Settings
from app.runtime.postgres import PostgresMigrator
from app.runtime.telemetry import PostgresTurnMetrics
from tests.fakes import load_catalog_mapping
from tests.postgres_harness import TemporaryPostgres


class EndToEndReplayTests(unittest.TestCase):
    def test_http_postgres_catalog_and_dialogue_replay_are_isolated(self) -> None:
        postgres = TemporaryPostgres()
        try:
            runtime_schema = "rolemodel_v2_runtime_replay"
            catalog_schema = "rolemodel_v2_catalog_replay"
            with psycopg.connect(postgres.dsn) as connection:
                connection.execute("CREATE ROLE rmv2_replay_app LOGIN")
                connection.execute(
                    """
                    CREATE TABLE public.v1_replay_sentinel (
                        id INTEGER PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO public.v1_replay_sentinel VALUES (1, 'unchanged')"
                )
            app_dsn = (
                f"postgresql://rmv2_replay_app@127.0.0.1:"
                f"{postgres.port}/postgres"
            )
            PostgresMigrator(
                dsn=postgres.dsn,
                schema=runtime_schema,
                v1_schema="public",
                app_role="rmv2_replay_app",
            ).migrate()
            PostgresCatalogMigrator(
                dsn=postgres.dsn,
                schema=catalog_schema,
                v1_schema="public",
                reader_role="rmv2_replay_app",
            ).migrate()
            PostgresCatalogPublisher(
                dsn=postgres.dsn,
                schema=catalog_schema,
            ).publish(
                load_catalog_mapping("catalog_v44_org.json"),
                source_sha256="sha256:replay-v44",
            )
            settings = Settings.from_mapping(
                {
                    "DATABASE_DSN": app_dsn,
                    "CATALOG_DSN": app_dsn,
                    "STATE_SCHEMA": runtime_schema,
                    "CATALOG_SCHEMA": catalog_schema,
                    "V1_STATE_SCHEMA": "public",
                    "V1_CATALOG_SCHEMA": "public",
                    "INSTALL_DIR": "/srv/RoleModelHelperV2Replay",
                    "V1_INSTALL_DIR": "/srv/RoleModelHelper",
                }
            )
            _, cache, runtime = build_runtime(settings)
            client = TestClient(
                create_app(
                    message_service=runtime,
                    catalog_cache=cache,
                    settings=settings,
                )
            )

            health = client.get("/api/v2/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["catalog_version"], "v44-org")
            session = client.post("/api/v2/sessions").json()
            first = client.post(
                f"/api/v2/sessions/{session['session_id']}/messages",
                json={
                    "request_id": "replay-1",
                    "text": "Какие роли у руководителя отдела 2?",
                    "state_revision": 0,
                },
            )
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()["diagnostics"]["gigachat_calls"], 0)
            self.assertEqual(first.json()["state"]["phase"], "AWAITING_SELECTION")
            options = first.json()["state"]["pending_question"]["options"]
            samara_choice = next(
                index
                for index, option in enumerate(options, start=1)
                if "Самара" in option["label"]
            )
            second = client.post(
                f"/api/v2/sessions/{session['session_id']}/messages",
                json={
                    "request_id": "replay-2",
                    "text": str(samara_choice),
                    "state_revision": 1,
                },
            )
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(
                second.json()["assistant"]["answer_type"],
                "PROFILE_ACCESS",
            )
            self.assertEqual(
                second.json()["assistant"]["facts"]["role_ids"],
                ["access-approver"],
            )
            replay = client.post(
                f"/api/v2/sessions/{session['session_id']}/messages",
                json={
                    "request_id": "replay-2",
                    "text": "другой текст",
                    "state_revision": 1,
                },
            )
            self.assertEqual(replay.json(), second.json())

            metrics = PostgresTurnMetrics(
                dsn=app_dsn,
                schema=runtime_schema,
            ).summary(hours=1)
            self.assertEqual(metrics["turns"], 2)
            self.assertEqual(metrics["gigachat_calls"], 0)
            with psycopg.connect(postgres.dsn) as connection:
                sentinel = connection.execute(
                    "SELECT id, value FROM public.v1_replay_sentinel"
                ).fetchone()
            self.assertEqual(sentinel, (1, "unchanged"))
        finally:
            postgres.close()


if __name__ == "__main__":
    unittest.main()
