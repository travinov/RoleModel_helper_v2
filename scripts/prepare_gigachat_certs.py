#!/usr/bin/env python3
"""Copy only GigaChat TLS material from V1 into an isolated V2 install."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Mapping, Sequence


V1_CERT = "RM_GIGACHAT_CERT_FILE"
V1_KEY = "RM_GIGACHAT_KEY_FILE"
V1_CA = "RM_GIGACHAT_CA_BUNDLE"
V1_CERT_DIR = "RM_GIGACHAT_CERT_DIR"
READ_KEYS = frozenset((V1_CERT, V1_KEY, V1_CA, V1_CERT_DIR))
DESTINATIONS = {
    V1_CERT: "client.crt",
    V1_KEY: "client.key",
    V1_CA: "ca.pem",
}
ENV_DESTINATIONS = {
    V1_CERT: "RMV2_GIGACHAT_CERT_FILE",
    V1_KEY: "RMV2_GIGACHAT_KEY_FILE",
    V1_CA: "RMV2_GIGACHAT_CA_BUNDLE",
}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PreparationError(RuntimeError):
    pass


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _parse_literal_value(raw: str, *, line_number: int) -> str:
    lexer = shlex.shlex(raw, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise PreparationError(f"invalid quoting on env line {line_number}") from exc
    if len(tokens) > 1:
        raise PreparationError(
            f"env line {line_number} must contain one literal path value"
        )
    return tokens[0] if tokens else ""


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse whitelisted shell-style assignments without expansion or execution."""

    _validate_regular_file(path, description="V1 env file")
    parsed: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("export "):
                stripped = stripped[7:].lstrip()
            if "=" not in stripped:
                continue
            key, raw_value = stripped.split("=", 1)
            key = key.strip()
            if not ASSIGNMENT.fullmatch(key) or key not in READ_KEYS:
                continue
            parsed[key] = _parse_literal_value(raw_value, line_number=line_number)
    return parsed


def _validate_regular_file(path: Path, *, description: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise PreparationError(f"{description} does not exist: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PreparationError(f"{description} must be a regular non-symlink file: {path}")
    if not info.st_mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH):
        raise PreparationError(f"{description} is not readable: {path}")


def _resolve_source(raw: str, *, v1_install_dir: Path) -> Path:
    if "\x00" in raw or "\n" in raw or "\r" in raw:
        raise PreparationError("certificate path contains invalid characters")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = v1_install_dir / candidate
    return candidate.absolute()


def _select_env_file(v1_install_dir: Path, explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit.expanduser().absolute()
    for name in (".env.server", ".env"):
        candidate = v1_install_dir / name
        if candidate.exists() or candidate.is_symlink():
            return candidate
    return None


def discover_sources(
    *,
    v1_install_dir: Path,
    v1_env_file: Path | None,
) -> dict[str, Path]:
    env_path = _select_env_file(v1_install_dir, v1_env_file)
    configured = parse_env_file(env_path) if env_path is not None else {}
    has_cert = bool(configured.get(V1_CERT))
    has_key = bool(configured.get(V1_KEY))
    if has_cert != has_key:
        raise PreparationError("V1 env must configure certificate and key together")

    fallback_dir_raw = configured.get(V1_CERT_DIR)
    fallback_dir = (
        _resolve_source(fallback_dir_raw, v1_install_dir=v1_install_dir)
        if fallback_dir_raw
        else v1_install_dir / "certs" / "gigachat"
    )
    selected = {
        V1_CERT: _resolve_source(
            configured[V1_CERT], v1_install_dir=v1_install_dir
        )
        if has_cert
        else (fallback_dir / "egress_sberca.crt").absolute(),
        V1_KEY: _resolve_source(
            configured[V1_KEY], v1_install_dir=v1_install_dir
        )
        if has_key
        else (fallback_dir / "egress_sberca.key").absolute(),
    }
    configured_ca = configured.get(V1_CA)
    if configured_ca:
        selected[V1_CA] = _resolve_source(
            configured_ca, v1_install_dir=v1_install_dir
        )
    else:
        fallback_ca = (fallback_dir / "ca.pem").absolute()
        if fallback_ca.exists() or fallback_ca.is_symlink():
            selected[V1_CA] = fallback_ca

    for key, source in selected.items():
        _validate_regular_file(
            source,
            description={
                V1_CERT: "GigaChat client certificate",
                V1_KEY: "GigaChat private key",
                V1_CA: "GigaChat CA bundle",
            }[key],
        )
        selected[key] = source.resolve(strict=True)
    return selected


def _updated_env(existing: str, values: Mapping[str, str]) -> str:
    replacement_keys = frozenset(ENV_DESTINATIONS.values())
    kept: list[str] = []
    for line in existing.splitlines():
        candidate = line.lstrip()
        if candidate.startswith("export "):
            candidate = candidate[7:].lstrip()
        key = candidate.split("=", 1)[0].strip() if "=" in candidate else ""
        if key not in replacement_keys:
            kept.append(line)
    while kept and not kept[-1]:
        kept.pop()
    if kept:
        kept.append("")
    for source_key in (V1_CERT, V1_KEY, V1_CA):
        kept.append(f"{ENV_DESTINATIONS[source_key]}={values.get(source_key, '')}")
    return "\n".join(kept) + "\n"


def prepare(
    *,
    v1_install_dir: Path,
    v2_install_dir: Path,
    v1_env_file: Path | None = None,
    v2_env_file: Path | None = None,
) -> dict[str, Path]:
    v1_input = v1_install_dir.expanduser().absolute()
    v2_input = v2_install_dir.expanduser().absolute()
    if v2_input.is_symlink():
        raise PreparationError("V2 install directory must not be a symlink")
    v1_root = v1_input.resolve(strict=False)
    v2_root = v2_input.resolve(strict=False)
    if v1_root == v2_root or _is_within(v2_root, v1_root):
        raise PreparationError("V2 destination must be outside the V1 install directory")
    if not v2_root.is_dir() or v2_root.is_symlink():
        raise PreparationError(f"V2 install directory must already exist: {v2_root}")

    sources = discover_sources(
        v1_install_dir=v1_root,
        v1_env_file=v1_env_file,
    )
    payloads = {key: source.read_bytes() for key, source in sources.items()}
    cert_parent = v2_root / "certs"
    cert_dir = cert_parent / "gigachat"
    env_input = (
        v2_env_file.expanduser().absolute()
        if v2_env_file is not None
        else v2_root / ".env"
    )
    if env_input.is_symlink():
        raise PreparationError("V2 env file must not be a symlink")
    env_path = env_input.resolve(strict=False)
    if env_path.parent != v2_root:
        raise PreparationError("V2 env file must be directly inside the V2 install directory")
    if env_path.exists():
        _validate_regular_file(env_path, description="V2 env file")
    existing_env = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    destination_paths = {
        key: (cert_dir / DESTINATIONS[key]).resolve(strict=False)
        for key in payloads
    }
    env_values = {key: str(path) for key, path in destination_paths.items()}
    updated_env = _updated_env(existing_env, env_values)

    if cert_parent.is_symlink() or (cert_parent.exists() and not cert_parent.is_dir()):
        raise PreparationError(f"V2 certificate parent is unsafe: {cert_parent}")
    cert_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(cert_parent, 0o700)
    staging = Path(tempfile.mkdtemp(prefix=".gigachat-staging-", dir=str(cert_parent)))
    backup: Path | None = None
    env_temp: Path | None = None
    try:
        os.chmod(staging, 0o700)
        for key, payload in payloads.items():
            target = staging / DESTINATIONS[key]
            with target.open("xb") as stream:
                stream.write(payload)
            os.chmod(target, 0o600)

        env_fd, env_temp_raw = tempfile.mkstemp(prefix=".env.", dir=str(v2_root))
        env_temp = Path(env_temp_raw)
        try:
            with os.fdopen(env_fd, "w", encoding="utf-8") as stream:
                stream.write(updated_env)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            env_temp.unlink(missing_ok=True)
            raise
        os.chmod(env_temp, 0o600)

        if cert_dir.exists() or cert_dir.is_symlink():
            if cert_dir.is_symlink() or not cert_dir.is_dir():
                raise PreparationError(f"V2 certificate destination is unsafe: {cert_dir}")
            backup = cert_parent / f".gigachat-backup-{os.getpid()}"
            if backup.exists():
                raise PreparationError(f"temporary backup path already exists: {backup}")
            cert_dir.rename(backup)
        staging.rename(cert_dir)
        try:
            os.replace(env_temp, env_path)
            env_temp = None
        except BaseException:
            shutil.rmtree(cert_dir)
            if backup is not None:
                backup.rename(cert_dir)
                backup = None
            raise
        if backup is not None:
            shutil.rmtree(backup)
            backup = None
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if env_temp is not None:
            env_temp.unlink(missing_ok=True)
        if backup is not None and backup.exists() and not cert_dir.exists():
            backup.rename(cert_dir)
    return destination_paths


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--v1-install-dir",
        type=Path,
        default=Path.home() / "RoleModelHelper2",
    )
    parser.add_argument("--v1-env-file", type=Path)
    parser.add_argument("--v2-install-dir", type=Path, required=True)
    parser.add_argument("--v2-env-file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        copied = prepare(
            v1_install_dir=args.v1_install_dir,
            v2_install_dir=args.v2_install_dir,
            v1_env_file=args.v1_env_file,
            v2_env_file=args.v2_env_file,
        )
    except (OSError, UnicodeError, PreparationError) as exc:
        print(f"GigaChat certificate preparation failed: {exc}", file=sys.stderr)
        return 2
    print("Prepared isolated V2 GigaChat TLS files:")
    for key in (V1_CERT, V1_KEY, V1_CA):
        if key in copied:
            print(f"  {ENV_DESTINATIONS[key]}={copied[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
