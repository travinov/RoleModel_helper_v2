from __future__ import annotations

import unittest

try:
    import psycopg
except ModuleNotFoundError as exc:
    raise unittest.SkipTest("psycopg is not installed for this interpreter") from exc

from app.catalog.postgres import (
    PostgresCatalogMigrator,
    PostgresCatalogSource,
)
from app.catalog.publish import publish_active_v1
from app.catalog.v1_snapshot import V1SnapshotCatalogAdapter
from app.config import Settings
from app.tools.catalog import CatalogSearchTools, ToolStatus
from tests.postgres_harness import TemporaryPostgres


class V1SnapshotAdapterIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.postgres = TemporaryPostgres()
        cls.v1_schema = "v1_rolemodel_fixture"
        cls.v2_schema = "rolemodel_v2_catalog_import"
        with psycopg.connect(cls.postgres.dsn) as connection:
            connection.execute("CREATE ROLE rmv2_importer LOGIN")
            connection.execute("CREATE ROLE rmv2_runtime_reader LOGIN")
            connection.execute(f'CREATE SCHEMA "{cls.v1_schema}"')
            connection.execute(
                f"""
                CREATE TABLE "{cls.v1_schema}".etl_run (
                    id BIGINT PRIMARY KEY,
                    file_sha256 TEXT NOT NULL
                );
                CREATE TABLE "{cls.v1_schema}".snapshot (
                    id BIGINT PRIMARY KEY,
                    run_id BIGINT NOT NULL,
                    snapshot_label TEXT,
                    is_active BOOLEAN NOT NULL,
                    loaded_at TIMESTAMPTZ NOT NULL,
                    source_file TEXT NOT NULL
                );
                CREATE TABLE "{cls.v1_schema}".profile (
                    id BIGINT PRIMARY KEY,
                    snapshot_id BIGINT NOT NULL,
                    profile_code TEXT NOT NULL,
                    profile_name TEXT NOT NULL
                );
                CREATE TABLE "{cls.v1_schema}".profile_structure_segment (
                    id BIGINT PRIMARY KEY,
                    snapshot_id BIGINT NOT NULL,
                    profile_id BIGINT NOT NULL,
                    path_order INTEGER NOT NULL,
                    segment_order INTEGER NOT NULL,
                    segment_name TEXT NOT NULL
                );
                CREATE TABLE "{cls.v1_schema}".profile_department (
                    id BIGINT PRIMARY KEY,
                    snapshot_id BIGINT NOT NULL,
                    profile_id BIGINT NOT NULL,
                    department_name TEXT NOT NULL,
                    department_code TEXT
                );
                CREATE TABLE "{cls.v1_schema}".profile_position (
                    id BIGINT PRIMARY KEY,
                    snapshot_id BIGINT NOT NULL,
                    profile_id BIGINT NOT NULL,
                    position_name TEXT NOT NULL,
                    position_code TEXT
                );
                CREATE TABLE "{cls.v1_schema}".system (
                    id BIGINT PRIMARY KEY,
                    snapshot_id BIGINT NOT NULL,
                    system_name_raw TEXT NOT NULL,
                    ci_code TEXT
                );
                CREATE TABLE "{cls.v1_schema}".system_alias_candidate (
                    id BIGINT PRIMARY KEY,
                    snapshot_id BIGINT NOT NULL,
                    system_id BIGINT NOT NULL,
                    alias_text TEXT NOT NULL,
                    alias_class TEXT NOT NULL
                );
                CREATE TABLE "{cls.v1_schema}".system_alias (
                    id BIGINT PRIMARY KEY,
                    system_id BIGINT NOT NULL,
                    alias_text TEXT NOT NULL,
                    is_active BOOLEAN NOT NULL
                );
                CREATE TABLE "{cls.v1_schema}".department_alias_candidate (
                    id BIGINT PRIMARY KEY,
                    snapshot_id BIGINT NOT NULL,
                    department_name TEXT NOT NULL,
                    alias_text TEXT NOT NULL,
                    alias_class TEXT NOT NULL
                );
                CREATE TABLE "{cls.v1_schema}".entitlement (
                    id BIGINT PRIMARY KEY,
                    snapshot_id BIGINT NOT NULL,
                    system_id BIGINT NOT NULL,
                    source_col INTEGER NOT NULL,
                    entitlement_type TEXT NOT NULL,
                    entitlement_name TEXT NOT NULL
                );
                CREATE TABLE "{cls.v1_schema}".profile_entitlement_access (
                    id BIGINT PRIMARY KEY,
                    snapshot_id BIGINT NOT NULL,
                    profile_id BIGINT NOT NULL,
                    entitlement_id BIGINT NOT NULL,
                    access_level SMALLINT NOT NULL
                );
                """
            )
            connection.execute(
                f"""
                INSERT INTO "{cls.v1_schema}".etl_run VALUES
                    (1, 'aabbccddeeff00112233445566778899');
                INSERT INTO "{cls.v1_schema}".snapshot VALUES
                    (10, 1, 'prod-44', TRUE, now(), 'role-model.xlsx');
                INSERT INTO "{cls.v1_schema}".profile VALUES
                    (100, 10, 'P-HEAD-2', 'Руководитель отдела кредитования');
                INSERT INTO "{cls.v1_schema}".profile_structure_segment VALUES
                    (1, 10, 100, 1, 1, 'Территориальный банк, г. Самара');
                INSERT INTO "{cls.v1_schema}".profile_department VALUES
                    (1, 10, 100,
                     'Отдел кредитования корпоративных клиентов номер 2', 'D-2');
                INSERT INTO "{cls.v1_schema}".profile_position VALUES
                    (1, 10, 100, 'Руководитель', 'POS-HEAD');
                INSERT INTO "{cls.v1_schema}".system VALUES
                    (200, 10, 'АС Заявки', 'CI-ACCESS');
                INSERT INTO "{cls.v1_schema}".system_alias_candidate VALUES
                    (1, 10, 200, 'заявки', 'SAFE');
                INSERT INTO "{cls.v1_schema}".system_alias VALUES
                    (1, 200, 'заявки', TRUE),
                    (2, 200, 'система заявок', TRUE);
                INSERT INTO "{cls.v1_schema}".department_alias_candidate VALUES
                    (1, 10,
                     'Отдел кредитования корпоративных клиентов номер 2',
                     'ОККК 2', 'SAFE');
                INSERT INTO "{cls.v1_schema}".entitlement VALUES
                    (300, 10, 200, 15, 'ROLE', 'Согласование заявок');
                INSERT INTO "{cls.v1_schema}".profile_entitlement_access VALUES
                    (1, 10, 100, 300, 2);
                """
            )
            connection.execute(
                f'GRANT USAGE ON SCHEMA "{cls.v1_schema}" TO rmv2_importer'
            )
            connection.execute(
                f'GRANT SELECT ON ALL TABLES IN SCHEMA "{cls.v1_schema}" '
                "TO rmv2_importer"
            )
        PostgresCatalogMigrator(
            dsn=cls.postgres.dsn,
            schema=cls.v2_schema,
            v1_schema=cls.v1_schema,
            reader_role="rmv2_runtime_reader",
            writer_role="rmv2_importer",
        ).migrate()
        cls.importer_dsn = (
            f"postgresql://rmv2_importer@127.0.0.1:{cls.postgres.port}/postgres"
        )
        cls.reader_dsn = (
            "postgresql://rmv2_runtime_reader@127.0.0.1:"
            f"{cls.postgres.port}/postgres"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.postgres.close()

    def test_active_v1_snapshot_is_extracted_and_published_as_v2_release(self) -> None:
        extracted = V1SnapshotCatalogAdapter(
            dsn=self.importer_dsn,
            v1_schema=self.v1_schema,
        ).extract()

        self.assertEqual(extracted.snapshot_id, 10)
        self.assertEqual(
            extracted.source_sha256,
            "aabbccddeeff00112233445566778899",
        )
        self.assertEqual(extracted.document["version"], "v1-snapshot-10-aabbccddeeff")
        self.assertEqual(extracted.document["departments"][0]["city"], "Самара")
        settings = Settings.from_mapping(
            {
                "DATABASE_DSN": self.reader_dsn,
                "CATALOG_DSN": self.reader_dsn,
                "CATALOG_IMPORT_DSN": self.importer_dsn,
                "CATALOG_SCHEMA": self.v2_schema,
                "V1_STATE_SCHEMA": self.v1_schema,
                "V1_CATALOG_SCHEMA": self.v1_schema,
                "STATE_SCHEMA": "rolemodel_v2_runtime_import_test",
            }
        )
        source = PostgresCatalogSource(
            dsn=self.reader_dsn,
            schema=self.v2_schema,
        )
        dry_run = publish_active_v1(settings, dry_run=True)
        self.assertEqual(dry_run["status"], "validated")
        self.assertIsNone(source.active_version())
        publication = publish_active_v1(settings, dry_run=False)
        self.assertEqual(publication["status"], "published")

        bundle = source.load(source.active_version())
        tools = CatalogSearchTools(bundle)
        department = tools.search_departments("отдел 2", city="Самара")
        position = tools.search_positions(
            "начальник",
            city="Самара",
            department_id=department.candidates[0].id,
        )
        profiles = tools.resolve_profiles(
            city="Самара",
            department_id=department.candidates[0].id,
            position_id=position.candidates[0].id,
        )
        systems = tools.search_systems(
            profile_ids=[item.id for item in profiles.candidates]
        )
        roles = tools.search_roles(
            system_id=systems.candidates[0].id,
            profile_ids=[item.id for item in profiles.candidates],
        )

        self.assertEqual(department.status, ToolStatus.FOUND)
        self.assertEqual(
            tools.search_departments("ОККК 2", city="Самара").status,
            ToolStatus.FOUND,
        )
        self.assertEqual(position.status, ToolStatus.FOUND)
        self.assertEqual(profiles.status, ToolStatus.FOUND)
        self.assertEqual(systems.candidates[0].label, "АС Заявки")
        self.assertEqual(roles.candidates[0].label, "Согласование заявок")
        self.assertEqual(roles.candidates[0].context["access_level"], 2)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with psycopg.connect(self.reader_dsn) as connection:
                connection.execute(
                    f'SELECT * FROM "{self.v1_schema}".snapshot'
                ).fetchall()


if __name__ == "__main__":
    unittest.main()
