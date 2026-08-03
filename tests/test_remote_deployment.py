from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts" / "deploy_rolemodel_v2_remote.sh"


class MacRemoteDeploymentContractTests(unittest.TestCase):
    def test_remote_deployer_has_isolated_safe_defaults(self) -> None:
        text = DEPLOY.read_text(encoding="utf-8")
        for contract in (
            'REMOTE_DIR="${RMV2_REMOTE_DIR:-RoleModelHelperV2}"',
            'V1_REMOTE_DIR="${RMV2_V1_REMOTE_DIR:-RoleModelHelper2}"',
            'APP_PORT="${RMV2_APP_PORT:-8001}"',
            'V1_PORT="${RMV2_V1_APP_PORT:-8000}"',
            'SERVICE_NAME="${RMV2_SERVICE_NAME:-rolemodel-helper-v2.service}"',
            "RMV2_V1_STATE_SCHEMA=rolemodel_helper",
            "RMV2_V1_CATALOG_SCHEMA=rolemodel_helper",
            "wheelhouse/*.whl",
            "health",
        ):
            self.assertIn(contract, text)
        self.assertIn('[[ "$APP_PORT" == "8001" ]]', text)
        self.assertIn('[[ "$V1_PORT" == "8000" ]]', text)
        self.assertNotIn("systemctl restart rolemodel-helper.service", text)
        self.assertNotIn("systemctl stop rolemodel-helper.service", text)
        self.assertIn("systemctl --user is-active", text)
        self.assertIn("systemctl --user stop", text)
        self.assertNotIn("sudo systemctl", text)

    def test_secrets_use_hidden_input_and_stdin_not_remote_arguments(self) -> None:
        text = DEPLOY.read_text(encoding="utf-8")
        self.assertEqual(text.count("read -r -s -p"), 2)
        self.assertIn('DB_USER="${RMV2_DB_USER:-CI09479675-pg-travinov}"', text)
        self.assertIn('Пароль PostgreSQL для $DB_USER:', text)
        self.assertNotIn("rolemodel_v2_owner", text)
        self.assertNotIn("rolemodel_v2_app", text)
        self.assertNotIn("rolemodel_v2_catalog_writer", text)
        self.assertNotIn("OWNER_PASSWORD", text)
        self.assertNotIn("IMPORT_PASSWORD", text)
        self.assertIn('ssh "$SSH_TARGET" "umask 077; cat >', text)
        self.assertNotIn("eval ", text)
        self.assertNotIn("source .env", text)
        self.assertNotIn("source \"$", text)

    def test_upload_is_staged_and_preserves_runtime_state(self) -> None:
        text = DEPLOY.read_text(encoding="utf-8")
        self.assertIn("mktemp -d", text)
        self.assertIn(".env.runtime", text)
        self.assertIn("certs", text)
        self.assertIn("logs", text)
        self.assertIn("activate_rolemodel_v2_server.sh", text)
        self.assertIn("ssh -tt", text)


if __name__ == "__main__":
    unittest.main()
