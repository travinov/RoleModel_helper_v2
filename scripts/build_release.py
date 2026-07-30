#!/usr/bin/env python3
"""Build a deterministic, secret-free RoleModel Helper V2 release ZIP."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Sequence


ARCHIVE_ROOT = "RoleModelHelperV2"
EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".playwright-cli",
        "certs",
        "output",
        "release",
        "wheelhouse",
        "dist",
        "build",
        "logs",
        "log",
        "backups",
        "dumps",
    }
)
SECRET_SUFFIXES = frozenset(
    {".crt", ".cer", ".key", ".pem", ".p12", ".pfx", ".db", ".sqlite", ".sqlite3", ".dump"}
)
PRIVATE_KEY_MARKERS = (
    b"-----BEGIN " + b"PRIVATE KEY-----",
    b"-----BEGIN RSA " + b"PRIVATE KEY-----",
    b"-----BEGIN EC " + b"PRIVATE KEY-----",
    b"-----BEGIN OPENSSH " + b"PRIVATE KEY-----",
)


class ReleaseError(RuntimeError):
    pass


def _excluded(relative: Path) -> bool:
    if any(part in EXCLUDED_PARTS for part in relative.parts):
        return True
    name = relative.name
    if name == ".DS_Store":
        return True
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    return relative.suffix.lower() in SECRET_SUFFIXES or name.endswith("~")


def _files(root: Path) -> list[Path]:
    selected: list[Path] = []
    for current_raw, directories, filenames in os.walk(root, followlinks=False):
        current = Path(current_raw)
        relative_current = current.relative_to(root)
        kept_directories: list[str] = []
        for name in directories:
            relative = relative_current / name
            candidate = current / name
            if _excluded(relative):
                continue
            if candidate.is_symlink():
                raise ReleaseError(f"refusing symlink in release tree: {relative}")
            kept_directories.append(name)
        directories[:] = kept_directories
        for name in filenames:
            relative = relative_current / name
            candidate = current / name
            if _excluded(relative):
                continue
            if candidate.is_symlink():
                raise ReleaseError(f"refusing symlink in release tree: {relative}")
            info = candidate.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ReleaseError(f"refusing non-regular release input: {relative}")
            selected.append(relative)
    return sorted(selected, key=lambda item: item.as_posix())


def _validated_payload(path: Path, relative: Path) -> bytes:
    payload = path.read_bytes()
    for marker in PRIVATE_KEY_MARKERS:
        if marker in payload:
            raise ReleaseError(f"private-key marker found in release input: {relative}")
    return payload


def _wheel_files(wheelhouse: Path | None) -> list[Path]:
    if wheelhouse is None:
        return []
    lexical = wheelhouse.expanduser().absolute()
    if lexical.is_symlink() or not lexical.is_dir():
        raise ReleaseError(f"wheelhouse must be a non-symlink directory: {lexical}")
    wheels: list[Path] = []
    for candidate in sorted(lexical.iterdir(), key=lambda item: item.name):
        if candidate.is_symlink() or not candidate.is_file():
            raise ReleaseError(f"wheelhouse contains unsafe entry: {candidate.name}")
        if candidate.suffix != ".whl":
            raise ReleaseError(f"wheelhouse contains non-wheel entry: {candidate.name}")
        wheels.append(candidate)
    if not wheels:
        raise ReleaseError("wheelhouse contains no wheels")
    return wheels


def _reject_symlink_ancestors(path: Path, *, description: str) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ReleaseError(
                f"{description} path contains symlink ancestor: {candidate}"
            )


def _prepare_output_path(output: Path) -> tuple[Path, Path]:
    lexical = output.expanduser().absolute()
    parent = lexical.parent
    checksum = lexical.with_suffix(lexical.suffix + ".sha256")
    _reject_symlink_ancestors(lexical, description="output")
    _reject_symlink_ancestors(checksum, description="checksum")
    if parent.is_symlink():
        raise ReleaseError(f"output parent must not be a symlink: {parent}")
    if parent.exists() and not parent.is_dir():
        raise ReleaseError(f"output parent is not a directory: {parent}")
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise ReleaseError(f"output parent is unsafe: {parent}")
    for description, candidate in (("output", lexical), ("checksum", checksum)):
        if candidate.is_symlink():
            raise ReleaseError(f"{description} destination must not be a symlink: {candidate}")
        if candidate.exists() and not candidate.is_file():
            raise ReleaseError(f"{description} destination must be a regular file: {candidate}")
    return lexical, checksum


def _atomic_text(path: Path, text: str) -> None:
    fd, temporary_raw = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_raw)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_release(*, root: Path, output: Path, wheelhouse: Path | None = None) -> str:
    root = root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ReleaseError(f"release root is not a directory: {root}")
    output, checksum = _prepare_output_path(output)
    files = _files(root)
    wheels = _wheel_files(wheelhouse)
    if not files:
        raise ReleaseError("release tree has no eligible files")

    fd, temporary_raw = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=str(output.parent),
    )
    os.close(fd)
    temporary = Path(temporary_raw)
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for relative in files:
                source = root / relative
                payload = _validated_payload(source, relative)
                archive_name = f"{ARCHIVE_ROOT}/{relative.as_posix()}"
                info = zipfile.ZipInfo(archive_name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                executable = bool(source.stat().st_mode & stat.S_IXUSR)
                mode = 0o100755 if executable else 0o100644
                info.external_attr = mode << 16
                archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
            for wheel in wheels:
                payload = _validated_payload(wheel, Path("wheelhouse") / wheel.name)
                archive_name = f"{ARCHIVE_ROOT}/wheelhouse/{wheel.name}"
                info = zipfile.ZipInfo(archive_name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()

    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    _atomic_text(checksum, f"{digest}  {output.name}\n")
    return digest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "output"
        / "RoleModel_helper_v2.zip",
    )
    parser.add_argument("--wheelhouse", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        digest = build_release(
            root=args.root,
            output=args.output,
            wheelhouse=args.wheelhouse,
        )
    except (OSError, ReleaseError, zipfile.BadZipFile) as exc:
        print(f"Release build failed: {exc}", file=sys.stderr)
        return 2
    resolved = args.output.expanduser().resolve(strict=False)
    print(f"Built {resolved}")
    print(f"SHA-256 {digest}")
    print(f"Checksum {resolved.with_suffix(resolved.suffix + '.sha256')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
