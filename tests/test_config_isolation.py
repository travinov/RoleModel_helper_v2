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
        self.assertTrue(settings.tls_verify)
        settings.validate_isolation()

    def test_startup_rejects_v1_port_service_directory_and_state_schema(self) -> None:
        safe = {
            "APP_HOST": "127.0.0.1",
            "APP_PORT": "8001",
            "INSTALL_DIR": "/srv/RoleModelHelperV2",
            "SERVICE_NAME": "rolemodel-helper-v2.service",
            "STATE_SCHEMA": "rolemodel_helper_v2",
            "STATE_DATABASE_PATH": "/srv/RoleModelHelperV2/data/state.sqlite3",
            "V1_APP_PORT": "8000",
            "V1_INSTALL_DIR": "/srv/RoleModelHelper",
            "V1_SERVICE_NAME": "rolemodel-helper.service",
            "V1_STATE_SCHEMA": "public",
            "V1_STATE_DATABASE_PATH": "/srv/RoleModelHelper/data/state.sqlite3",
        }
        collisions = (
            ("port", {"APP_PORT": "8000"}),
            ("service", {"SERVICE_NAME": "rolemodel-helper.service"}),
            ("directory", {"INSTALL_DIR": "/srv/RoleModelHelper"}),
            ("schema", {"STATE_SCHEMA": "public"}),
            (
                "state database",
                {"STATE_DATABASE_PATH": "/srv/RoleModelHelper/data/state.sqlite3"},
            ),
        )

        for name, override in collisions:
            with self.subTest(name=name):
                settings = Settings.from_mapping({**safe, **override})
                with self.assertRaises(IsolationError):
                    settings.validate_isolation()


if __name__ == "__main__":
    unittest.main()
