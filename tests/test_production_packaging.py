from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CERT_TOOL = ROOT / "scripts" / "prepare_gigachat_certs.py"
RELEASE_TOOL = ROOT / "scripts" / "build_release.py"
OFFLINE_RELEASE_TOOL = ROOT / "scripts" / "build_offline_release.sh"
INSTALLER = ROOT / "scripts" / "install_rolemodel_v2_server.sh"
INSTALLER_PREFLIGHT = ROOT / "scripts" / "installer_preflight.py"
RELEASE_REQUIREMENTS = ROOT / "requirements-release.txt"


class CertificatePreparationTests(unittest.TestCase):
    def test_explicit_env_paths_are_parsed_without_eval_and_copied_safely(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw).resolve()
            v1 = temp / "RoleModelHelper2"
            v2 = temp / "RoleModelHelperV2"
            source = v1 / "external certs"
            source.mkdir(parents=True)
            v2.mkdir()
            cert = source / "client.crt"
            key = source / "client.key"
            ca = source / "corporate ca.pem"
            cert.write_text("CERTIFICATE-CANARY", encoding="utf-8")
            key.write_text("KEY-CANARY", encoding="utf-8")
            ca.write_text("CA-CANARY", encoding="utf-8")
            marker = temp / "must-not-exist"
            v1_env = v1 / ".env.server"
            v1_env.write_text(
                "\n".join(
                    (
                        f'RM_GIGACHAT_CERT_FILE="{cert}"',
                        f"RM_GIGACHAT_KEY_FILE='{key}'",
                        f'RM_GIGACHAT_CA_BUNDLE="{ca}" # trusted CA',
                        f"UNRELATED=$(touch {marker})",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            v2_env = v2 / ".env"
            v2_env.write_text("KEEP_ME=value\nRMV2_TLS_VERIFY=true\n", encoding="utf-8")

            completed = subprocess.run(
                (
                    sys.executable,
                    str(CERT_TOOL),
                    "--v1-install-dir",
                    str(v1),
                    "--v1-env-file",
                    str(v1_env),
                    "--v2-install-dir",
                    str(v2),
                    "--v2-env-file",
                    str(v2_env),
                ),
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(marker.exists())
            cert_dir = v2.resolve() / "certs" / "gigachat"
            copied = {
                "client.crt": "CERTIFICATE-CANARY",
                "client.key": "KEY-CANARY",
                "ca.pem": "CA-CANARY",
            }
            for name, expected in copied.items():
                target = cert_dir / name
                self.assertEqual(target.read_text(encoding="utf-8"), expected)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(cert_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(v2_env.stat().st_mode), 0o600)
            env_text = v2_env.read_text(encoding="utf-8")
            self.assertIn("KEEP_ME=value", env_text)
            self.assertIn(f"RMV2_GIGACHAT_CERT_FILE={cert_dir / 'client.crt'}", env_text)
            self.assertIn(f"RMV2_GIGACHAT_KEY_FILE={cert_dir / 'client.key'}", env_text)
            self.assertIn(f"RMV2_GIGACHAT_CA_BUNDLE={cert_dir / 'ca.pem'}", env_text)

    def test_fallback_pair_without_optional_ca_succeeds_and_bad_pair_fails_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw).resolve()
            v1 = temp / "RoleModelHelper2"
            source = v1 / "certs" / "gigachat"
            source.mkdir(parents=True)
            (source / "egress_sberca.crt").write_text("CERT", encoding="utf-8")
            (source / "egress_sberca.key").write_text("KEY", encoding="utf-8")
            v2 = temp / "RoleModelHelperV2"
            v2.mkdir()

            ok = subprocess.run(
                (
                    sys.executable,
                    str(CERT_TOOL),
                    "--v1-install-dir",
                    str(v1),
                    "--v2-install-dir",
                    str(v2),
                ),
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertFalse((v2 / "certs" / "gigachat" / "ca.pem").exists())

            bad_v2 = temp / "bad-v2"
            bad_v2.mkdir()
            (source / "egress_sberca.key").unlink()
            failed = subprocess.run(
                (
                    sys.executable,
                    str(CERT_TOOL),
                    "--v1-install-dir",
                    str(v1),
                    "--v2-install-dir",
                    str(bad_v2),
                ),
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse((bad_v2 / "certs").exists())

    def test_symlinked_source_and_explicit_missing_ca_fail_before_destination_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw).resolve()
            v1 = temp / "RoleModelHelper2"
            source = v1 / "material"
            source.mkdir(parents=True)
            real_cert = source / "real.crt"
            real_cert.write_text("CERT", encoding="utf-8")
            linked_cert = source / "linked.crt"
            linked_cert.symlink_to(real_cert)
            key = source / "client.key"
            key.write_text("KEY", encoding="utf-8")
            missing_ca = source / "missing-ca.pem"
            env = v1 / ".env.server"
            env.write_text(
                "\n".join(
                    (
                        f"RM_GIGACHAT_CERT_FILE={linked_cert}",
                        f"RM_GIGACHAT_KEY_FILE={key}",
                        f"RM_GIGACHAT_CA_BUNDLE={missing_ca}",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            v2 = temp / "RoleModelHelperV2"
            v2.mkdir()

            completed = subprocess.run(
                (
                    sys.executable,
                    str(CERT_TOOL),
                    "--v1-install-dir",
                    str(v1),
                    "--v1-env-file",
                    str(env),
                    "--v2-install-dir",
                    str(v2),
                ),
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse((v2 / "certs").exists())
            self.assertFalse((v2 / ".env").exists())

    def test_installer_does_not_source_env_and_invokes_safe_copy_tool(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertNotIn('source "$PROJECT_DIR/.env"', text)
        self.assertNotIn("eval ", text)
        self.assertIn("prepare_gigachat_certs.py", text)
        preflight = INSTALLER_PREFLIGHT.read_text(encoding="utf-8")
        self.assertIn("app_port == 8000", preflight)

    def test_offline_installer_branch_and_cp39_build_contract(self) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('"$INSTALL_DIR/wheelhouse"', installer)
        self.assertIn("--no-index", installer)
        self.assertIn("--find-links", installer)
        self.assertIn("requirements-release.txt", installer)

        builder = OFFLINE_RELEASE_TOOL.read_text(encoding="utf-8")
        for argument in (
            "--only-binary=:all:",
            "--platform manylinux2014_x86_64",
            "--implementation cp",
            "--python-version 39",
            "--abi cp39",
            "--wheelhouse",
        ):
            self.assertIn(argument, builder)

        pins = [
            line.strip()
            for line in RELEASE_REQUIREMENTS.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertTrue(pins)
        self.assertTrue(all("==" in line for line in pins))
        self.assertTrue(all(not any(token in line for token in (">=", "<=", "~=", "!=")) for line in pins))


class DeterministicReleaseTests(unittest.TestCase):
    def test_release_is_reproducible_and_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw).resolve()
            project = temp / "project"
            project.mkdir()
            (project / "app.py").write_text("print('safe')\n", encoding="utf-8")
            (project / ".env.example").write_text("TOKEN=\n", encoding="utf-8")
            forbidden = {
                ".env": "SEEDED_" + "SECRET=top-secret",
                ".env.server": "SEEDED_" + "SECRET=server-secret",
                "certs/gigachat/client.key": "-----BEGIN " + "PRIVATE KEY-----",
                ".git/config": "SEEDED_" + "SECRET=git-secret",
                ".venv/bin/python": "SEEDED_" + "SECRET=venv-secret",
                "output/old.zip": "SEEDED_" + "SECRET=output-secret",
                "logs/app.log": "SEEDED_" + "SECRET=log-secret",
            }
            for relative, content in forbidden.items():
                path = project / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            first = temp / "first.zip"
            second = temp / "second.zip"
            for output in (first, second):
                completed = subprocess.run(
                    (
                        sys.executable,
                        str(RELEASE_TOOL),
                        "--root",
                        str(project),
                        "--output",
                        str(output),
                    ),
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            digest = hashlib.sha256(first.read_bytes()).hexdigest()
            self.assertEqual(
                first.with_suffix(".zip.sha256").read_text(encoding="ascii"),
                f"{digest}  {first.name}\n",
            )
            with zipfile.ZipFile(first) as archive:
                names = archive.namelist()
                payload = b"".join(archive.read(name) for name in names)
                self.assertEqual(names, sorted(names))
                self.assertIn("RoleModelHelperV2/.env.example", names)
                self.assertIn("RoleModelHelperV2/app.py", names)
                self.assertNotIn(b"SEEDED_" + b"SECRET", payload)
                self.assertNotIn(b"PRIVATE KEY", payload)
                self.assertTrue(
                    all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
                )

    def test_external_wheelhouse_is_injected_without_writing_wheels_to_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw).resolve()
            project = temp / "project"
            project.mkdir()
            (project / "app.py").write_text("pass\n", encoding="utf-8")
            wheelhouse = temp / "wheelhouse"
            wheelhouse.mkdir()
            wheel = wheelhouse / "demo_dependency-1.0-py3-none-any.whl"
            wheel.write_bytes(b"synthetic wheel fixture")
            output = temp / "offline.zip"

            completed = subprocess.run(
                (
                    sys.executable,
                    str(RELEASE_TOOL),
                    "--root",
                    str(project),
                    "--output",
                    str(output),
                    "--wheelhouse",
                    str(wheelhouse),
                ),
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse((project / "wheelhouse").exists())
            with zipfile.ZipFile(output) as archive:
                self.assertIn(
                    "RoleModelHelperV2/wheelhouse/" + wheel.name,
                    archive.namelist(),
                )


if __name__ == "__main__":
    unittest.main()
