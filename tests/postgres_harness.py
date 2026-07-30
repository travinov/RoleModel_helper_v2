from __future__ import annotations

import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path


class TemporaryPostgres:
    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.data = self.root / "data"
        self.socket_dir = self.root / "socket"
        self.socket_dir.mkdir()
        self.port = self._free_port()
        self.initdb = shutil.which("initdb")
        self.pg_ctl = shutil.which("pg_ctl")
        if not self.initdb or not self.pg_ctl:
            raise unittest.SkipTest("PostgreSQL server binaries are unavailable")
        subprocess.run(
            [
                self.initdb,
                "-D",
                str(self.data),
                "-A",
                "trust",
                "-U",
                "postgres",
                "--no-locale",
                "--encoding=UTF8",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                self.pg_ctl,
                "-D",
                str(self.data),
                "-l",
                str(self.root / "postgres.log"),
                "-o",
                f"-F -p {self.port} -k {self.socket_dir}",
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.dsn = f"postgresql://postgres@127.0.0.1:{self.port}/postgres"

    def close(self) -> None:
        if self.pg_ctl and self.data.exists():
            subprocess.run(
                [self.pg_ctl, "-D", str(self.data), "-m", "fast", "-w", "stop"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        self._temporary.cleanup()

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])
