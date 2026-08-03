from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PostgresDeploymentContractTests(unittest.TestCase):
    def test_environment_and_installer_require_isolated_postgresql(self) -> None:
        environment = (ROOT / ".env.example").read_text(encoding="utf-8")
        installer = (
            ROOT / "scripts" / "install_rolemodel_v2_server.sh"
        ).read_text(encoding="utf-8")
        activation = (
            ROOT / "scripts" / "activate_rolemodel_v2_server.py"
        ).read_text(encoding="utf-8")

        self.assertIn("RMV2_DATABASE_DSN=", environment)
        self.assertIn("RMV2_MIGRATION_DSN=", environment)
        self.assertIn("RMV2_DATABASE_APP_ROLE=", environment)
        self.assertIn("RMV2_STATE_SCHEMA=rolemodel_v2_runtime", environment)
        self.assertIn("RMV2_CATALOG_SCHEMA=rolemodel_v2_catalog", environment)
        self.assertIn("RMV2_CATALOG_READER_ROLE=", environment)
        self.assertIn("RMV2_CATALOG_WRITER_ROLE=", environment)
        self.assertIn('"-m", "app.catalog.migrate"', activation)
        self.assertIn("UMask=0077", installer)
        self.assertIn("ProtectSystem=strict", installer)
        self.assertIn("ProtectHome=read-only", installer)
        self.assertIn("PrivateDevices=true", installer)
        self.assertIn("RestrictSUIDSGID=true", installer)
        self.assertNotIn("STATE_DATABASE_PATH", environment)
        self.assertNotIn("sqlite", environment.lower())

        self.assertIn("RMV2_DATABASE_DSN", installer)
        self.assertIn('"-m", "app.runtime.migrate"', activation)
        self.assertNotIn("STATE_DATABASE_PATH", installer)
        self.assertNotIn("sqlite", installer.lower())


if __name__ == "__main__":
    unittest.main()
