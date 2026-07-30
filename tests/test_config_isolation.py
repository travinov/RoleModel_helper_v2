from __future__ import annotations

import unittest
from pathlib import Path

from app.config import IsolationError, Settings


class ConfigIsolationTests(unittest.TestCase):
    def test_v2_safe_defaults_are_separate_and_tls_verification_is_enabled(self) -> None:
        settings = Settings.from_mapping({})

        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 8001)
        self.assertEqual(settings.install_dir, Path.home() / "RoleModelHelperV2")
        self.assertEqual(settings.service_name, "rolemodel-helper-v2.service")
        self.assertNotEqual(settings.state_schema, settings.v1_state_schema)
        self.assertEqual(settings.state_schema, "rolemodel_v2_runtime")
        self.assertEqual(settings.catalog_backend, "postgres")
        self.assertEqual(settings.catalog_schema, "rolemodel_v2_catalog")
        self.assertEqual(settings.effective_catalog_dsn, settings.database_dsn)
        self.assertIsNone(settings.catalog_version)
        self.assertEqual(settings.database_dsn, "postgresql:///rolemodel")
        self.assertTrue(settings.tls_verify)
        settings.validate_isolation()

    def test_startup_rejects_v1_port_service_directory_and_state_schema(self) -> None:
        safe = {
            "APP_HOST": "127.0.0.1",
            "APP_PORT": "8001",
            "INSTALL_DIR": "/srv/RoleModelHelperV2",
            "SERVICE_NAME": "rolemodel-helper-v2.service",
            "DATABASE_DSN": "postgresql://rmv2@localhost/rolemodel",
            "STATE_SCHEMA": "rolemodel_v2_runtime",
            "V1_APP_PORT": "8000",
            "V1_INSTALL_DIR": "/srv/RoleModelHelper",
            "V1_SERVICE_NAME": "rolemodel-helper.service",
            "V1_STATE_SCHEMA": "public",
        }
        collisions = (
            ("port", {"APP_PORT": "8000"}),
            ("service", {"SERVICE_NAME": "rolemodel-helper.service"}),
            ("directory", {"INSTALL_DIR": "/srv/RoleModelHelper"}),
            ("schema", {"STATE_SCHEMA": "public"}),
            ("catalog schema", {"CATALOG_SCHEMA": "public"}),
            (
                "catalog/runtime schema",
                {"CATALOG_SCHEMA": "rolemodel_v2_runtime"},
            ),
        )

        for name, override in collisions:
            with self.subTest(name=name):
                settings = Settings.from_mapping({**safe, **override})
                with self.assertRaises(IsolationError):
                    settings.validate_isolation()

    def test_json_catalog_requires_explicit_backend_and_path(self) -> None:
        with self.assertRaises(IsolationError):
            Settings.from_mapping({"CATALOG_BACKEND": "json"}).validate_isolation()

        settings = Settings.from_mapping(
            {
                "CATALOG_BACKEND": "json",
                "CATALOG_PATH": "/tmp/catalog.json",
                "CATALOG_VERSION": "fixture-v1",
            }
        )
        settings.validate_isolation()

    def test_production_configuration_rejects_non_postgresql_dsn(self) -> None:
        for value in ("", "sqlite:///state.sqlite3", "mysql://localhost/rolemodel"):
            with self.subTest(value=value):
                settings = Settings.from_mapping({"DATABASE_DSN": value})
                with self.assertRaises(IsolationError):
                    settings.validate_isolation()


if __name__ == "__main__":
    unittest.main()
