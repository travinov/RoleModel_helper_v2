from __future__ import annotations

import importlib.util
import json
import stat
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "activate_rolemodel_v2_server.py"
WRAPPER_PATH = ROOT / "scripts" / "activate_rolemodel_v2_server.sh"
INSTALLER_PATH = ROOT / "scripts" / "install_rolemodel_v2_server.sh"


def load_module():
    spec = importlib.util.spec_from_file_location("activate_rolemodel_v2_server", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("activation module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GuidedActivationTests(unittest.TestCase):
    def test_installer_hands_off_to_single_activation_command(self) -> None:
        wrapper = WRAPPER_PATH.read_text(encoding="utf-8")
        installer = INSTALLER_PATH.read_text(encoding="utf-8")

        self.assertIn("activate_rolemodel_v2_server.py", wrapper)
        self.assertNotIn("source ", wrapper)
        self.assertIn("bash scripts/activate_rolemodel_v2_server.sh", installer)
        self.assertIn("EnvironmentFile=$INSTALL_DIR/.env.runtime", installer)
        self.assertNotIn("sudo systemctl start", installer)

    def test_runtime_env_excludes_privileged_activation_credentials(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw)
            values = {
                "RMV2_DATABASE_DSN": "postgresql://runtime@db/rolemodel",
                "RMV2_CATALOG_DSN": "postgresql://reader@db/rolemodel",
                "RMV2_MIGRATION_DSN": "postgresql://owner:secret@db/rolemodel",
                "RMV2_CATALOG_IMPORT_DSN": "postgresql://writer:secret@db/rolemodel",
                "RMV2_CATALOG_WRITER_ROLE": "rolemodel_v2_catalog_writer",
                "RMV2_GIGACHAT_CERT_FILE": "/srv/v2/client.crt",
            }

            runtime_env = module.write_runtime_env(project, values)

            text = runtime_env.read_text(encoding="utf-8")
            self.assertIn("RMV2_DATABASE_DSN=", text)
            self.assertIn("RMV2_CATALOG_DSN=", text)
            self.assertIn("RMV2_GIGACHAT_CERT_FILE=", text)
            self.assertNotIn("RMV2_MIGRATION_DSN", text)
            self.assertNotIn("RMV2_CATALOG_IMPORT_DSN", text)
            self.assertNotIn("RMV2_CATALOG_WRITER_ROLE", text)
            self.assertEqual(stat.S_IMODE(runtime_env.stat().st_mode), 0o600)

    def test_env_is_parsed_as_data_without_shell_execution(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            marker = temp / "executed"
            env_file = temp / ".env"
            env_file.write_text(
                "\n".join(
                    (
                        "RMV2_APP_PORT=8001",
                        "RMV2_DATABASE_DSN=postgresql:///rolemodel",
                        f"UNRELATED=$(touch {marker})",
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            parsed = module.parse_env_file(env_file)

            self.assertEqual(parsed["RMV2_APP_PORT"], "8001")
            self.assertEqual(parsed["RMV2_DATABASE_DSN"], "postgresql:///rolemodel")
            self.assertFalse(marker.exists())

    def test_missing_required_database_setting_fails_before_activation(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw)
            (project / ".venv" / "bin").mkdir(parents=True)
            (project / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
            env_file = project / ".env"
            env_file.write_text(
                "\n".join(
                    (
                        "RMV2_APP_PORT=8001",
                        "RMV2_SERVICE_NAME=rolemodel-helper-v2.service",
                        "RMV2_DATABASE_DSN=postgresql:///rolemodel",
                        "RMV2_MIGRATION_DSN=postgresql:///rolemodel",
                        "RMV2_CATALOG_DSN=postgresql:///rolemodel",
                        "RMV2_STATE_SCHEMA=rolemodel_v2_runtime",
                        "RMV2_CATALOG_SCHEMA=rolemodel_v2_catalog",
                        "RMV2_V1_APP_PORT=8000",
                        "RMV2_TLS_VERIFY=true",
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(module.ActivationError, "CATALOG_IMPORT_DSN"):
                module.load_configuration(project, env_file)

    def _config(self, module, project: Path):
        python = project / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
        return module.ActivationConfig(
            project_dir=project,
            python=python,
            service_name="rolemodel-helper-v2.service",
            app_port=8001,
            v1_port=8000,
            environment={"RMV2_APP_PORT": "8001"},
        )

    def test_successful_activation_preserves_order_and_warns_about_instructions(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as raw:
            config = self._config(module, Path(raw))
            calls: list[tuple[str, ...]] = []
            health_calls: list[int] = []
            output: list[str] = []
            dry_run = json.dumps(
                {
                    "status": "validated",
                    "dry_run": True,
                    "version": "v44",
                    "source_sha256": "abc123",
                    "counts": {"profiles": 10, "instructions": 0},
                }
            )

            def execute(argv, *, capture=False, environment=None):
                calls.append(tuple(str(item) for item in argv))
                stdout = dry_run if "--dry-run" in argv else ""
                return module.CommandResult(0, stdout, "")

            def health(port):
                health_calls.append(port)
                return {"status": "ok"}

            module.activate(
                config,
                execute=execute,
                health_check=health,
                confirm=lambda prompt: "PUBLISH",
                emit=output.append,
            )

            command_text = [" ".join(call) for call in calls]
            self.assertIn("app.runtime.migrate", command_text[0])
            self.assertIn("app.catalog.migrate", command_text[1])
            self.assertIn("app.catalog.publish --dry-run", command_text[2])
            self.assertIn("app.catalog.publish", command_text[3])
            self.assertEqual(calls[4], ("sudo", "systemctl", "restart", config.service_name))
            self.assertEqual(health_calls, [8000, 8001, 8000])
            self.assertTrue(any("инструкц" in line.lower() for line in output))

    def test_declined_confirmation_does_not_publish_or_touch_systemd(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as raw:
            config = self._config(module, Path(raw))
            calls: list[tuple[str, ...]] = []
            dry_run = json.dumps(
                {
                    "status": "validated",
                    "dry_run": True,
                    "version": "v44",
                    "source_sha256": "abc123",
                    "counts": {"profiles": 10, "instructions": 1},
                }
            )

            def execute(argv, *, capture=False, environment=None):
                calls.append(tuple(str(item) for item in argv))
                return module.CommandResult(0, dry_run if "--dry-run" in argv else "", "")

            with self.assertRaisesRegex(module.ActivationError, "отменена"):
                module.activate(
                    config,
                    execute=execute,
                    health_check=lambda port: {"status": "ok"},
                    confirm=lambda prompt: "NO",
                    emit=lambda line: None,
                )

            joined = [" ".join(call) for call in calls]
            self.assertFalse(any("systemctl" in line for line in joined))
            self.assertFalse(any(line.endswith("app.catalog.publish") for line in joined))

    def test_failed_v2_health_stops_only_v2_service(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as raw:
            config = self._config(module, Path(raw))
            calls: list[tuple[str, ...]] = []
            dry_run = json.dumps(
                {
                    "status": "validated",
                    "dry_run": True,
                    "version": "v44",
                    "source_sha256": "abc123",
                    "counts": {"profiles": 10, "instructions": 1},
                }
            )

            def execute(argv, *, capture=False, environment=None):
                calls.append(tuple(str(item) for item in argv))
                return module.CommandResult(0, dry_run if "--dry-run" in argv else "", "")

            health_results = iter(({"status": "ok"}, None))
            with self.assertRaisesRegex(module.ActivationError, "health"):
                module.activate(
                    config,
                    execute=execute,
                    health_check=lambda port: next(health_results),
                    confirm=lambda prompt: "PUBLISH",
                    emit=lambda line: None,
                    health_attempts=1,
                    sleep=lambda seconds: None,
                )

            self.assertIn(
                ("sudo", "systemctl", "stop", config.service_name),
                calls,
            )
            self.assertFalse(any("rolemodel-helper.service" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
