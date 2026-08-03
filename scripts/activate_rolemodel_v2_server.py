#!/usr/bin/env python3
"""Safely migrate, publish, start, and verify an installed V2 service."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Mapping, NamedTuple, Sequence


SERVICE_RE = re.compile(r"^[A-Za-z0-9_.@-]+\.service$")
SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REQUIRED_DSN_KEYS = (
    "RMV2_DATABASE_DSN",
    "RMV2_MIGRATION_DSN",
    "RMV2_CATALOG_DSN",
    "RMV2_CATALOG_IMPORT_DSN",
)
PRIVILEGED_RUNTIME_KEYS = frozenset(
    {
        "RMV2_MIGRATION_DSN",
        "RMV2_CATALOG_IMPORT_DSN",
        "RMV2_CATALOG_WRITER_ROLE",
    }
)


class ActivationError(RuntimeError):
    pass


class CommandResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


class ActivationConfig(NamedTuple):
    project_dir: Path
    python: Path
    service_name: str
    app_port: int
    v1_port: int
    environment: Mapping[str, str]


def _literal(raw: str, *, line_number: int) -> str:
    lexer = shlex.shlex(raw, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise ActivationError(f"Некорректные кавычки в .env, строка {line_number}") from exc
    if len(tokens) > 1:
        raise ActivationError(
            f"Значение в .env, строка {line_number}, должно быть одним литералом"
        )
    value = tokens[0] if tokens else ""
    if any(character in value for character in ("\x00", "\n", "\r", "\t")):
        raise ActivationError(f"Недопустимый символ в .env, строка {line_number}")
    return value


def parse_env_file(path: Path) -> dict[str, str]:
    lexical = path.expanduser().absolute()
    if lexical.is_symlink() or not lexical.is_file():
        raise ActivationError(f".env должен быть обычным файлом: {lexical}")
    parsed: dict[str, str] = {}
    with lexical.open("r", encoding="utf-8") as stream:
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
            if not ASSIGNMENT_RE.fullmatch(key) or not key.startswith("RMV2_"):
                continue
            parsed[key] = _literal(raw_value, line_number=line_number)
    return parsed


def _port(values: Mapping[str, str], key: str, default: str) -> int:
    raw = values.get(key, default)
    if not raw.isascii() or not raw.isdecimal():
        raise ActivationError(f"{key} должен быть целым портом")
    port = int(raw)
    if not 1 <= port <= 65535:
        raise ActivationError(f"{key} должен находиться в диапазоне 1..65535")
    return port


def _regular_file(path: Path, description: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ActivationError(f"Не найден {description}: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ActivationError(f"{description} должен быть обычным файлом: {path}")


def load_configuration(project_dir: Path, env_file: Path) -> ActivationConfig:
    project_input = project_dir.expanduser().absolute()
    if project_input.is_symlink() or not project_input.is_dir():
        raise ActivationError(f"Каталог V2 небезопасен: {project_input}")
    project = project_input.resolve()
    values = parse_env_file(env_file)

    app_port = _port(values, "RMV2_APP_PORT", "8001")
    v1_port = _port(values, "RMV2_V1_APP_PORT", "8000")
    if app_port != 8001:
        raise ActivationError("Production activation ожидает RMV2_APP_PORT=8001")
    if v1_port != 8000 or app_port == v1_port:
        raise ActivationError("Порты V1/V2 должны быть 8000/8001")

    service = values.get("RMV2_SERVICE_NAME", "rolemodel-helper-v2.service")
    if not SERVICE_RE.fullmatch(service) or service == "rolemodel-helper.service":
        raise ActivationError("Некорректное или пересекающееся имя V2 service")

    state_schema = values.get("RMV2_STATE_SCHEMA", "")
    catalog_schema = values.get("RMV2_CATALOG_SCHEMA", "")
    for key, schema in (
        ("RMV2_STATE_SCHEMA", state_schema),
        ("RMV2_CATALOG_SCHEMA", catalog_schema),
    ):
        if not SCHEMA_RE.fullmatch(schema) or schema.lower() == "public":
            raise ActivationError(f"{key} должен быть отдельной непубличной схемой")
    if state_schema == catalog_schema:
        raise ActivationError("Runtime и catalog схемы V2 должны различаться")

    for key in REQUIRED_DSN_KEYS:
        dsn = values.get(key, "").strip()
        if not dsn:
            raise ActivationError(f"Заполните {key} в .env")
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ActivationError(f"{key} должен использовать PostgreSQL DSN")

    if values.get("RMV2_TLS_VERIFY", "true").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise ActivationError("Для production требуется RMV2_TLS_VERIFY=true")

    cert_raw = values.get("RMV2_GIGACHAT_CERT_FILE", "").strip()
    key_raw = values.get("RMV2_GIGACHAT_KEY_FILE", "").strip()
    if bool(cert_raw) != bool(key_raw):
        raise ActivationError("GigaChat certificate и key должны быть заданы вместе")
    if cert_raw and key_raw:
        _regular_file(Path(cert_raw).expanduser(), "GigaChat certificate")
        _regular_file(Path(key_raw).expanduser(), "GigaChat private key")
    ca_raw = values.get("RMV2_GIGACHAT_CA_BUNDLE", "").strip()
    if ca_raw:
        _regular_file(Path(ca_raw).expanduser(), "GigaChat CA bundle")

    python = project / ".venv" / "bin" / "python"
    _regular_file(python, "Python V2 virtual environment")
    return ActivationConfig(
        project_dir=project,
        python=python,
        service_name=service,
        app_port=app_port,
        v1_port=v1_port,
        environment=values,
    )


def default_execute(
    argv: Sequence[str | Path],
    *,
    capture: bool = False,
    environment: Mapping[str, str] | None = None,
) -> CommandResult:
    completed = subprocess.run(
        [str(item) for item in argv],
        cwd=None,
        env=dict(environment) if environment is not None else None,
        check=False,
        text=True,
        capture_output=capture,
    )
    return CommandResult(
        completed.returncode,
        completed.stdout or "",
        completed.stderr or "",
    )


def default_health_check(port: int) -> Mapping[str, object] | None:
    version = "v1" if port == 8000 else "v2"
    url = f"http://127.0.0.1:{port}/api/{version}/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else None
    except (OSError, UnicodeError, ValueError, urllib.error.URLError):
        return None


def _run(
    execute: Callable[..., CommandResult],
    argv: Sequence[str | Path],
    *,
    environment: Mapping[str, str],
    capture: bool = False,
) -> CommandResult:
    result = execute(argv, capture=capture, environment=environment)
    if result.returncode != 0:
        command = " ".join(str(item) for item in argv)
        detail = result.stderr.strip() or result.stdout.strip() or "без деталей"
        raise ActivationError(f"Команда завершилась ошибкой: {command}\n{detail}")
    return result


def _dry_run_payload(stdout: str) -> dict[str, object]:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise ActivationError("Dry-run не вернул отчёт")
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise ActivationError("Dry-run вернул некорректный JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") != "validated":
        raise ActivationError("Dry-run не подтвердил валидность каталога")
    return payload


def write_runtime_env(
    project_dir: Path,
    values: Mapping[str, str],
) -> Path:
    project_input = project_dir.expanduser().absolute()
    if project_input.is_symlink() or not project_input.is_dir():
        raise ActivationError(f"Каталог V2 небезопасен: {project_input}")
    project = project_input.resolve()
    destination = project / ".env.runtime"
    if destination.is_symlink():
        raise ActivationError(".env.runtime не должен быть symlink")

    lines = [
        f"{key}={shlex.quote(value)}"
        for key, value in sorted(values.items())
        if key.startswith("RMV2_") and key not in PRIVILEGED_RUNTIME_KEYS
    ]
    content = "\n".join(lines) + "\n"
    fd, temporary_raw = tempfile.mkstemp(
        prefix=".env.runtime.",
        dir=str(project),
    )
    temporary = Path(temporary_raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def activate(
    config: ActivationConfig,
    *,
    execute: Callable[..., CommandResult] = default_execute,
    health_check: Callable[[int], Mapping[str, object] | None] = default_health_check,
    confirm: Callable[[str], str] = input,
    emit: Callable[[str], None] = print,
    health_attempts: int = 10,
    sleep: Callable[[float], None] = time.sleep,
) -> Mapping[str, object]:
    environment = dict(os.environ)
    environment.update(config.environment)
    python = str(config.python)

    emit("1/7 Проверяю V1 на порту 8000")
    if health_check(config.v1_port) is None:
        raise ActivationError("V1 health недоступен; V2 не изменялась")

    emit("2/7 Создаю/обновляю только runtime-схему V2")
    _run(
        execute,
        [python, "-m", "app.runtime.migrate"],
        environment=environment,
    )
    emit("3/7 Создаю/обновляю только catalog-схему V2")
    _run(
        execute,
        [python, "-m", "app.catalog.migrate"],
        environment=environment,
    )

    emit("4/7 Проверяю активный снимок V1 без записи")
    report = _dry_run_payload(
        _run(
            execute,
            [python, "-m", "app.catalog.publish", "--dry-run"],
            environment=environment,
            capture=True,
        ).stdout
    )
    emit(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    counts = report.get("counts")
    if isinstance(counts, dict) and int(counts.get("instructions") or 0) == 0:
        emit(
            "ПРЕДУПРЕЖДЕНИЕ: статические инструкции не найдены; "
            "роли будут доступны, инструкции нужно импортировать отдельно."
        )

    answer = confirm(
        "Для публикации проверенного снимка введите PUBLISH: "
    ).strip()
    if answer != "PUBLISH":
        raise ActivationError("Публикация отменена пользователем после dry-run")

    emit("5/7 Публикую проверенный снимок в отдельный каталог V2")
    _run(
        execute,
        [python, "-m", "app.catalog.publish"],
        environment=environment,
    )
    write_runtime_env(config.project_dir, config.environment)
    emit("6/7 Перезапускаю только V2 service")
    _run(
        execute,
        ["systemctl", "--user", "restart", config.service_name],
        environment=environment,
    )

    v2_health: Mapping[str, object] | None = None
    for attempt in range(max(1, health_attempts)):
        v2_health = health_check(config.app_port)
        if v2_health is not None and v2_health.get("catalog_ready", True) is not False:
            break
        if attempt + 1 < health_attempts:
            sleep(2)
    if v2_health is None or v2_health.get("catalog_ready", True) is False:
        execute(
            ["systemctl", "--user", "stop", config.service_name],
            capture=False,
            environment=environment,
        )
        raise ActivationError(
            "V2 health не поднялся; остановлен только V2 service"
        )

    emit("7/7 Повторно проверяю V1 после запуска V2")
    if health_check(config.v1_port) is None:
        execute(
            ["systemctl", "--user", "stop", config.service_name],
            capture=False,
            environment=environment,
        )
        raise ActivationError(
            "V1 health пропал после запуска; остановлен только V2 service"
        )
    emit("Активация завершена: V1 и V2 доступны одновременно.")
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--env-file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print(
            "Запускайте activation от обычного пользователя, не через sudo.",
            file=sys.stderr,
        )
        return 2
    args = _parser().parse_args(argv)
    project = args.project_dir.expanduser().absolute()
    env_file = args.env_file or project / ".env"
    try:
        config = load_configuration(project, env_file)
        os.chdir(config.project_dir)
        activate(config)
    except (ActivationError, OSError, UnicodeError) as exc:
        print(f"Активация V2 остановлена: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
