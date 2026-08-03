from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "scripts" / "installer_preflight.py"
INSTALLER = ROOT / "scripts" / "install_rolemodel_v2_server.sh"
RELEASE_TOOL = ROOT / "scripts" / "build_release.py"


def run_preflight(env_file: Path, project: Path, overrides: dict[str, str] | None = None):
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("RMV2_")
    }
    environment.update(overrides or {})
    return subprocess.run(
        (
            sys.executable,
            str(PREFLIGHT),
            "--env-file",
            str(env_file),
            "--project-dir",
            str(project),
            "--emit",
        ),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


class InstallerEffectiveConfigTests(unittest.TestCase):
    def _base_env(self, project: Path) -> str:
        return "\n".join(
            (
                "RMV2_APP_PORT=8001",
                f"RMV2_INSTALL_DIR={project}",
                "RMV2_SERVICE_NAME=rolemodel-helper-v2.service",
                "RMV2_DATABASE_DSN=postgresql:///rolemodel",
                "RMV2_STATE_SCHEMA=rolemodel_v2_runtime",
                "RMV2_CATALOG_SCHEMA=rolemodel_v2_catalog",
                "RMV2_V1_APP_PORT=8000",
                f"RMV2_V1_INSTALL_DIR={project.parent / 'RoleModelHelper2'}",
                "RMV2_V1_SERVICE_NAME=rolemodel-helper.service",
                "RMV2_V1_STATE_SCHEMA=public",
                "RMV2_V1_CATALOG_SCHEMA=public",
            )
        ) + "\n"

    def test_data_only_v2_env_parser_honors_override_without_executing_shell(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw).resolve()
            project = temp / "RoleModelHelperV2"
            project.mkdir()
            marker = temp / "must-not-exist"
            env_file = project / ".env"
            env_file.write_text(
                self._base_env(project)
                + f"UNRELATED=$(touch {marker})\n"
                + "RMV2_V1_ENV_FILE='/safe/path/with spaces.env' # comment\n",
                encoding="utf-8",
            )

            completed = run_preflight(
                env_file,
                project,
                {"RMV2_APP_PORT": "8002"},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(marker.exists())
            self.assertIn("APP_PORT\t8002\n", completed.stdout)
            self.assertIn(
                "V1_ENV_FILE\t/safe/path/with spaces.env\n",
                completed.stdout,
            )

            persisted = subprocess.run(
                (
                    sys.executable,
                    str(PREFLIGHT),
                    "--env-file",
                    str(env_file),
                    "--project-dir",
                    str(project),
                    "--write-effective",
                    str(env_file),
                ),
                env={
                    **{
                        key: value
                        for key, value in os.environ.items()
                        if not key.startswith("RMV2_")
                    },
                    "RMV2_APP_PORT": "8002",
                },
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(persisted.returncode, 0, persisted.stderr)
            persisted_text = env_file.read_text(encoding="utf-8")
            self.assertIn("RMV2_APP_PORT=8002", persisted_text)
            self.assertIn(f"UNRELATED=$(touch {marker})", persisted_text)
            self.assertFalse(marker.exists())

    def test_hard_port_and_public_schema_guards_ignore_v1_override_values(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw).resolve()
            project = temp / "RoleModelHelperV2"
            project.mkdir()
            cases = (
                (
                    "port",
                    "RMV2_APP_PORT=8000\nRMV2_V1_APP_PORT=9000\n",
                ),
                (
                    "state",
                    "RMV2_STATE_SCHEMA=public\nRMV2_V1_STATE_SCHEMA=v1_other\n",
                ),
                (
                    "catalog",
                    "RMV2_CATALOG_SCHEMA=public\nRMV2_V1_STATE_SCHEMA=v1_other\n",
                ),
            )
            for name, overrides in cases:
                with self.subTest(name=name):
                    env_file = project / f".env-{name}"
                    base = self._base_env(project)
                    for line in overrides.splitlines():
                        key = line.split("=", 1)[0]
                        base = "\n".join(
                            current
                            for current in base.splitlines()
                            if not current.startswith(key + "=")
                        ) + "\n"
                        base += line + "\n"
                    env_file.write_text(base, encoding="utf-8")
                    completed = run_preflight(env_file, project)
                    self.assertNotEqual(completed.returncode, 0)

    def test_service_name_is_strict_and_user_unit_path_is_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw).resolve()
            project = temp / "RoleModelHelperV2"
            project.mkdir()
            env_file = project / ".env"
            env_file.write_text(
                self._base_env(project).replace(
                    "rolemodel-helper-v2.service",
                    "../../tmp/evil.service",
                ),
                encoding="utf-8",
            )
            completed = run_preflight(env_file, project)
            self.assertNotEqual(completed.returncode, 0)

            installer = INSTALLER.read_text(encoding="utf-8")
            self.assertIn("$HOME/.config/systemd/user", installer)
            self.assertIn("SYSTEMD_DIR", installer)
            self.assertIn("UNIT_PATH", installer)
            self.assertIn("SERVICE_NAME_RE", installer)
            self.assertNotIn("/etc/systemd/system", installer)

    def test_python39_venv_preflight_precedes_target_mutation(self) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")
        version_check = installer.index("sys.version_info[:2]")
        ensurepip_check = installer.index("ensurepip")
        env_copy = installer.index('cp "$INSTALL_DIR/.env.example"')
        target_venv = installer.index('python3 -m venv "$INSTALL_DIR/.venv"')
        self.assertLess(version_check, env_copy)
        self.assertLess(ensurepip_check, env_copy)
        self.assertLess(ensurepip_check, target_venv)

    def test_root_execution_is_rejected_first_and_installer_never_uses_sudo(self) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")
        root_guard = installer.index("EUID")
        project_resolution = installer.index("PROJECT_DIR=")
        env_copy = installer.index('cp "$INSTALL_DIR/.env.example"')
        self.assertLess(root_guard, project_resolution)
        self.assertLess(root_guard, env_copy)
        self.assertIn("обычным пользователем", installer)
        self.assertIn("id -un", installer)
        self.assertNotIn("User=", installer)
        sudo_lines = [
            line.strip()
            for line in installer.splitlines()
            if line.strip().startswith("sudo ")
        ]
        self.assertEqual(sudo_lines, [])
        self.assertIn("systemctl --user daemon-reload", installer)
        self.assertIn('systemctl --user enable "$SERVICE_NAME"', installer)

        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw).resolve()
            project = temp / "RoleModelHelperV2"
            project.mkdir()
            env_file = project / ".env"
            env_file.write_text(self._base_env(project), encoding="utf-8")
            for account in ("root", "../../unsafe"):
                with self.subTest(account=account):
                    completed = subprocess.run(
                        (
                            sys.executable,
                            str(PREFLIGHT),
                            "--env-file",
                            str(env_file),
                            "--project-dir",
                            str(project),
                            "--current-user",
                            account,
                        ),
                        env={
                            key: value
                            for key, value in os.environ.items()
                            if not key.startswith("RMV2_")
                        },
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertNotEqual(completed.returncode, 0)


class ReleaseBoundaryTests(unittest.TestCase):
    def test_source_wheelhouse_is_excluded_and_only_external_wheels_are_injected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw).resolve()
            project = temp / "project"
            source_wheels = project / "wheelhouse"
            source_wheels.mkdir(parents=True)
            (project / "app.py").write_text("pass\n", encoding="utf-8")
            (source_wheels / "source_secret-1-py3-none-any.whl").write_bytes(
                b"SOURCE-WHEEL-CANARY"
            )
            external = temp / "external"
            external.mkdir()
            (external / "approved-1-py3-none-any.whl").write_bytes(b"APPROVED")
            output = temp / "release.zip"

            completed = subprocess.run(
                (
                    sys.executable,
                    str(RELEASE_TOOL),
                    "--root",
                    str(project),
                    "--output",
                    str(output),
                    "--wheelhouse",
                    str(external),
                ),
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
                payload = b"".join(archive.read(name) for name in names)
            self.assertIn(
                "RoleModelHelperV2/wheelhouse/approved-1-py3-none-any.whl",
                names,
            )
            self.assertNotIn(b"SOURCE-WHEEL-CANARY", payload)
            self.assertNotIn(
                "RoleModelHelperV2/wheelhouse/source_secret-1-py3-none-any.whl",
                names,
            )

    def test_output_checksum_and_parent_symlinks_are_rejected_without_target_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw).resolve()
            project = temp / "project"
            project.mkdir()
            (project / "app.py").write_text("pass\n", encoding="utf-8")

            output_target = temp / "output-target"
            output_target.write_bytes(b"OUTPUT-SENTINEL")
            output_link = temp / "release.zip"
            output_link.symlink_to(output_target)
            output_result = subprocess.run(
                (
                    sys.executable,
                    str(RELEASE_TOOL),
                    "--root",
                    str(project),
                    "--output",
                    str(output_link),
                ),
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(output_result.returncode, 0)
            self.assertEqual(output_target.read_bytes(), b"OUTPUT-SENTINEL")

            real_output = temp / "real.zip"
            checksum_target = temp / "checksum-target"
            checksum_target.write_bytes(b"CHECKSUM-SENTINEL")
            checksum_link = real_output.with_suffix(".zip.sha256")
            checksum_link.symlink_to(checksum_target)
            checksum_result = subprocess.run(
                (
                    sys.executable,
                    str(RELEASE_TOOL),
                    "--root",
                    str(project),
                    "--output",
                    str(real_output),
                ),
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(checksum_result.returncode, 0)
            self.assertEqual(checksum_target.read_bytes(), b"CHECKSUM-SENTINEL")

            real_parent = temp / "real-parent"
            real_parent.mkdir()
            linked_parent = temp / "linked-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            parent_result = subprocess.run(
                (
                    sys.executable,
                    str(RELEASE_TOOL),
                    "--root",
                    str(project),
                    "--output",
                    str(linked_parent / "release.zip"),
                ),
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(parent_result.returncode, 0)
            self.assertFalse((real_parent / "release.zip").exists())

    def test_nested_symlink_ancestor_is_rejected_without_target_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw).resolve()
            project = temp / "project"
            project.mkdir()
            (project / "app.py").write_text("pass\n", encoding="utf-8")
            outside = temp / "outside"
            (outside / "nested").mkdir(parents=True)
            safe = temp / "safe"
            safe.mkdir()
            (safe / "redirect").symlink_to(outside, target_is_directory=True)
            redirected_output = safe / "redirect" / "nested" / "release.zip"

            completed = subprocess.run(
                (
                    sys.executable,
                    str(RELEASE_TOOL),
                    "--root",
                    str(project),
                    "--output",
                    str(redirected_output),
                ),
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse((outside / "nested" / "release.zip").exists())
            self.assertFalse(
                (outside / "nested" / "release.zip.sha256").exists()
            )


if __name__ == "__main__":
    unittest.main()
