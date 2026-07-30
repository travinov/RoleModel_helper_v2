from __future__ import annotations

import copy
import unittest
from pathlib import Path

try:
    import psycopg
except ModuleNotFoundError as exc:
    raise unittest.SkipTest("psycopg is not installed for this interpreter") from exc

from app.catalog.postgres import (
    CatalogValidationError,
    PostgresCatalogMigrator,
    PostgresCatalogPublisher,
    PostgresCatalogSource,
)
from app.tools.catalog import CatalogSearchTools, ToolStatus
from tests.fakes import load_catalog_mapping
from tests.postgres_harness import TemporaryPostgres


class PostgresCatalogIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.postgres = TemporaryPostgres()
        with psycopg.connect(cls.postgres.dsn) as connection:
            connection.execute("CREATE ROLE rmv2_catalog_reader LOGIN")
            connection.execute("CREATE ROLE rmv2_catalog_writer LOGIN")
            connection.execute(
                """
                CREATE TABLE public.v1_catalog_sentinel (
                    id INTEGER PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO public.v1_catalog_sentinel VALUES (1, 'unchanged')"
            )
        cls.reader_dsn = (
            "postgresql://rmv2_catalog_reader@127.0.0.1:"
            f"{cls.postgres.port}/postgres"
        )
        cls.writer_dsn = (
            "postgresql://rmv2_catalog_writer@127.0.0.1:"
            f"{cls.postgres.port}/postgres"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.postgres.close()

    def setUp(self) -> None:
        self.schema = f"rolemodel_v2_catalog_{self._testMethodName[-24:]}"
        PostgresCatalogMigrator(
            dsn=self.postgres.dsn,
            schema=self.schema,
            v1_schema="public",
            reader_role="rmv2_catalog_reader",
            writer_role="rmv2_catalog_writer",
        ).migrate()
        self.publisher = PostgresCatalogPublisher(
            dsn=self.writer_dsn,
            schema=self.schema,
        )
        self.source = PostgresCatalogSource(
            dsn=self.reader_dsn,
            schema=self.schema,
        )

    def test_publish_round_trip_and_search_use_normalized_active_release(self) -> None:
        mapping = load_catalog_mapping("catalog_v44_org.json")

        publication = self.publisher.publish(
            mapping,
            source_sha256="sha256:v44-org",
        )

        self.assertEqual(publication.version, "v44-org")
        self.assertEqual(publication.previous_version, None)
        self.assertEqual(self.source.active_version(), "v44-org")
        bundle = self.source.load("v44-org")
        tools = CatalogSearchTools(bundle)
        department = tools.search_departments("второй отдел", city="Самара")
        self.assertEqual(department.status, ToolStatus.FOUND)
        self.assertEqual(
            department.candidates[0].id,
            "department-samara-credit-2",
        )
        stored_department = next(
            item
            for item in bundle.departments
            if item["id"] == "department-samara-credit-2"
        )
        self.assertEqual(stored_department["number"], 2)
        self.assertEqual(
            stored_department["name"],
            "Отдел кредитования корпоративных клиентов номер 2",
        )

        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with psycopg.connect(self.reader_dsn) as connection:
                connection.execute(
                    f"""
                    INSERT INTO "{self.schema}".catalog_release(
                        version, source_sha256, status, counts_json, document_json
                    )
                    VALUES ('forbidden', 'x', 'STAGING', '{{}}', '{{}}')
                    """
                )
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with psycopg.connect(self.writer_dsn) as connection:
                connection.execute(
                    "UPDATE public.v1_catalog_sentinel SET value = 'changed'"
                )
        self.assertEqual(self._sentinel(), (1, "unchanged"))

    def test_invalid_release_preserves_last_good_active_version(self) -> None:
        good = load_catalog_mapping("catalog_v44_org.json")
        self.publisher.publish(good, source_sha256="sha256:good")
        invalid = copy.deepcopy(good)
        invalid["version"] = "v45-invalid"
        invalid["profiles"][0]["department_ids"] = ["missing-department"]

        with self.assertRaises(CatalogValidationError):
            self.publisher.publish(invalid, source_sha256="sha256:invalid")

        self.assertEqual(self.source.active_version(), "v44-org")
        with self.assertRaises(KeyError):
            self.source.load("v45-invalid")

    def test_second_valid_release_atomically_retires_previous(self) -> None:
        first = load_catalog_mapping("catalog_v44_org.json")
        self.publisher.publish(first, source_sha256="sha256:first")
        second = copy.deepcopy(first)
        second["version"] = "v45-org"
        second["departments"][0]["name"] = (
            "Отдел кредитования корпоративных клиентов №2"
        )

        publication = self.publisher.publish(
            second,
            source_sha256="sha256:second",
        )

        self.assertEqual(publication.previous_version, "v44-org")
        self.assertEqual(self.source.active_version(), "v45-org")
        with psycopg.connect(self.postgres.dsn) as connection:
            rows = connection.execute(
                f"""
                SELECT version, status
                FROM "{self.schema}".catalog_release
                ORDER BY version
                """
            ).fetchall()
        self.assertEqual(rows, [("v44-org", "RETIRED"), ("v45-org", "ACTIVE")])

    def _sentinel(self) -> tuple[int, str]:
        with psycopg.connect(self.postgres.dsn) as connection:
            row = connection.execute(
                "SELECT id, value FROM public.v1_catalog_sentinel"
            ).fetchone()
        return int(row[0]), str(row[1])


if __name__ == "__main__":
    unittest.main()
