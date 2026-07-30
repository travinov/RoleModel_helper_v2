#!/usr/bin/env python3
"""Parse and validate effective V2 installer settings without shell evaluation."""

from __future__ import annotations

import argparse
import os
import pwd
import re
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Mapping, Sequence


ENV_TO_OUTPUT = {
    "RMV2_APP_PORT": "APP_PORT",
    "RMV2_INSTALL_DIR": "INSTALL_DIR",
    "RMV2_SERVICE_NAME": "SERVICE_NAME",
    "RMV2_DATABASE_DSN": "DATABASE_DSN",
    "RMV2_STATE_SCHEMA": "STATE_SCHEMA",
    "RMV2_CATALOG_SCHEMA": "CATALOG_SCHEMA",
    "RMV2_V1_INSTALL_DIR": "V1_INSTALL_DIR",
    "RMV2_V1_SERVICE_NAME": "V1_SERVICE_NAME",
    "RMV2_V1_APP_PORT": "V1_APP_PORT",
    "RMV2_V1_STATE_SCHEMA": "V1_STATE_SCHEMA",
    "RMV2_V1_CATALOG_SCHEMA": "V1_CATALOG_SCHEMA",
    "RMV2_V1_ENV_FILE": "V1_ENV_FILE",
}
SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]+\.service$")
SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")
ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PreflightError(ValueError):
    pass


def _literal(raw: str, *, line_number: int) -> str:
    lexer = shlex.shlex(raw, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise PreflightError(f"invalid quoting on V2 env line {line_number}") from exc
    if len(tokens) > 1:
        raise PreflightError(
            f"V2 env line {line_number} must contain one literal value"
        )
    value = tokens[0] if tokens else ""
    if any(character in value for character in ("\n", "\r", "\t", "\x00")):
        raise PreflightError(f"invalid character on V2 env line {line_number}")
    return value


def parse_env(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise PreflightError(f"V2 env must be a regular non-symlink file: {path}")
    parsed: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            key = key.strip()
            if not ASSIGNMENT_RE.fullmatch(key) or key not in ENV_TO_OUTPUT:
                continue
            parsed[key] = _literal(raw_value, line_number=line_number)
    return parsed


def _integer(value: str, *, name: str) -> int:
    if not value.isascii() or not value.isdecimal():
        raise PreflightError(f"{name} must be an integer")
    parsed = int(value)
    if not 1 <= parsed <= 65535:
        raise PreflightError(f"{name} must be from 1 to 65535")
    return parsed


def validate_current_user(account: str) -> str:
    if not account or len(account) > 64 or not ACCOUNT_RE.fullmatch(account):
        raise PreflightError("current account is not a safe systemd User basename")
    if account == "root":
        raise PreflightError("installer must run as a non-root account")
    return account


def effective_settings(
    *,
    env_file: Path,
    project_dir: Path,
    process_env: Mapping[str, str],
) -> dict[str, str]:
    project_input = project_dir.expanduser().absolute()
    if project_input.is_symlink() or not project_input.is_dir():
        raise PreflightError("project directory must be a real directory")
    project = project_input.resolve()
    values = {
        "RMV2_APP_PORT": "8001",
        "RMV2_INSTALL_DIR": str(Path.home() / "RoleModelHelperV2"),
        "RMV2_SERVICE_NAME": "rolemodel-helper-v2.service",
        "RMV2_DATABASE_DSN": "postgresql:///rolemodel",
        "RMV2_STATE_SCHEMA": "rolemodel_v2_runtime",
        "RMV2_CATALOG_SCHEMA": "rolemodel_v2_catalog",
        "RMV2_V1_INSTALL_DIR": str(Path.home() / "RoleModelHelper2"),
        "RMV2_V1_SERVICE_NAME": "rolemodel-helper.service",
        "RMV2_V1_APP_PORT": "8000",
        "RMV2_V1_STATE_SCHEMA": "public",
        "RMV2_V1_CATALOG_SCHEMA": "public",
        "RMV2_V1_ENV_FILE": "",
    }
    values.update(parse_env(env_file))
    values.update(
        {
            key: process_env[key]
            for key in ENV_TO_OUTPUT
            if key in process_env
        }
    )
    for key, value in values.items():
        if any(character in value for character in ("\n", "\r", "\t", "\x00")):
            raise PreflightError(f"{key} contains an unsafe character")

    app_port = _integer(values["RMV2_APP_PORT"], name="RMV2_APP_PORT")
    v1_port = _integer(values["RMV2_V1_APP_PORT"], name="RMV2_V1_APP_PORT")
    if app_port == 8000:
        raise PreflightError("port 8000 is always reserved for V1")
    if app_port == v1_port:
        raise PreflightError("V2 port collides with configured V1 port")

    install_input = Path(values["RMV2_INSTALL_DIR"]).expanduser().absolute()
    if install_input.is_symlink():
        raise PreflightError("V2 install directory must not be a symlink")
    install_dir = install_input.resolve(strict=False)
    v1_dir = Path(values["RMV2_V1_INSTALL_DIR"]).expanduser().resolve(strict=False)
    default_v1_dir = (Path.home() / "RoleModelHelper2").resolve(strict=False)
    if install_dir != project:
        raise PreflightError(f"installer must run from final V2 directory: {install_dir}")
    if install_dir in (v1_dir, default_v1_dir):
        raise PreflightError("V2 install directory collides with V1")

    service_name = values["RMV2_SERVICE_NAME"]
    if not SERVICE_NAME_RE.fullmatch(service_name):
        raise PreflightError("RMV2_SERVICE_NAME is not a safe systemd service basename")
    if service_name in (
        values["RMV2_V1_SERVICE_NAME"],
        "rolemodel-helper.service",
    ):
        raise PreflightError("V2 service name collides with V1")

    state_schema = values["RMV2_STATE_SCHEMA"]
    catalog_schema = values["RMV2_CATALOG_SCHEMA"]
    v1_schema = values["RMV2_V1_STATE_SCHEMA"]
    v1_catalog_schema = values["RMV2_V1_CATALOG_SCHEMA"]
    for name, schema in (
        ("RMV2_STATE_SCHEMA", state_schema),
        ("RMV2_CATALOG_SCHEMA", catalog_schema),
        ("RMV2_V1_STATE_SCHEMA", v1_schema),
        ("RMV2_V1_CATALOG_SCHEMA", v1_catalog_schema),
    ):
        if not SCHEMA_RE.fullmatch(schema):
            raise PreflightError(f"{name} must be a simple PostgreSQL identifier")
    if state_schema.lower() == "public" or catalog_schema.lower() == "public":
        raise PreflightError("V2 schemas must never be public")
    if (
        state_schema in (catalog_schema, v1_schema, v1_catalog_schema)
        or catalog_schema in (v1_schema, v1_catalog_schema)
    ):
        raise PreflightError("V2 schemas collide with each other or V1")

    database_dsn = values["RMV2_DATABASE_DSN"]
    if not database_dsn.startswith(("postgresql://", "postgres://")):
        raise PreflightError("RMV2_DATABASE_DSN must use PostgreSQL")

    result = {
        output_key: values[env_key]
        for env_key, output_key in ENV_TO_OUTPUT.items()
    }
    result["APP_PORT"] = str(app_port)
    result["V1_APP_PORT"] = str(v1_port)
    result["INSTALL_DIR"] = str(install_dir)
    result["V1_INSTALL_DIR"] = str(v1_dir)
    return result


def write_effective_env(
    *,
    source: Path,
    destination: Path,
    settings: Mapping[str, str],
    project_dir: Path,
) -> None:
    destination_input = destination.expanduser().absolute()
    if destination_input.is_symlink():
        raise PreflightError("effective V2 env destination must not be a symlink")
    project = project_dir.expanduser().resolve()
    if destination_input.parent.resolve() != project or destination_input.name != ".env":
        raise PreflightError("effective V2 env destination must be <project>/.env")

    known = frozenset(ENV_TO_OUTPUT)
    kept: list[str] = []
    for raw_line in source.read_text(encoding="utf-8").splitlines():
        candidate = raw_line.lstrip()
        if candidate.startswith("export "):
            candidate = candidate[7:].lstrip()
        key = candidate.split("=", 1)[0].strip() if "=" in candidate else ""
        if key not in known:
            kept.append(raw_line)
    while kept and not kept[-1]:
        kept.pop()
    if kept:
        kept.append("")
    for env_key, output_key in ENV_TO_OUTPUT.items():
        kept.append(f"{env_key}={shlex.quote(settings[output_key])}")
    content = "\n".join(kept) + "\n"

    fd, temporary_raw = tempfile.mkstemp(
        prefix=".env.",
        dir=str(destination_input.parent),
    )
    temporary = Path(temporary_raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination_input)
    finally:
        if temporary.exists():
            temporary.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--emit", action="store_true")
    parser.add_argument("--write-effective", type=Path)
    parser.add_argument("--current-user")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = effective_settings(
            env_file=args.env_file.expanduser().absolute(),
            project_dir=args.project_dir,
            process_env=os.environ,
        )
        current_user = validate_current_user(
            args.current_user
            if args.current_user is not None
            else pwd.getpwuid(os.geteuid()).pw_name
        )
        if args.write_effective is not None:
            write_effective_env(
                source=args.env_file.expanduser().absolute(),
                destination=args.write_effective,
                settings=settings,
                project_dir=args.project_dir,
            )
    except (OSError, UnicodeError, PreflightError) as exc:
        print(f"Installer preflight failed: {exc}", file=sys.stderr)
        return 2
    if args.emit:
        for key in ENV_TO_OUTPUT.values():
            print(f"{key}\t{settings[key]}")
        print(f"RUN_USER\t{current_user}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
