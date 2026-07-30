from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class IsolationError(ValueError):
    pass


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8001
    install_dir: Path = Path.home() / "RoleModelHelperV2"
    service_name: str = "rolemodel-helper-v2.service"
    database_dsn: str = "postgresql:///rolemodel"
    migration_dsn: str | None = None
    database_app_role: str | None = None
    state_schema: str = "rolemodel_v2_runtime"
    v1_port: int = 8000
    v1_install_dir: Path = Path.home() / "RoleModelHelper2"
    v1_service_name: str = "rolemodel-helper.service"
    v1_state_schema: str = "public"
    tls_verify: bool = True
    catalog_backend: str = "postgres"
    catalog_dsn: str | None = None
    catalog_schema: str = "rolemodel_v2_catalog"
    catalog_reader_role: str | None = None
    catalog_writer_role: str | None = None
    catalog_import_dsn: str | None = None
    v1_catalog_schema: str = "public"
    catalog_path: Path | None = None
    catalog_version: str | None = None
    catalog_refresh_seconds: float = 5.0
    gigachat_endpoint: str = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    gigachat_auth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_model: str = "GigaChat-2-Max"
    gigachat_access_token: str | None = None
    gigachat_auth_key: str | None = None
    gigachat_client_id: str | None = None
    gigachat_client_secret: str | None = None
    gigachat_cert_file: Path | None = None
    gigachat_key_file: Path | None = None
    gigachat_ca_bundle: Path | None = None
    planner_deadline_ms: int = 12_000

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "Settings":
        return cls(
            host=values.get("APP_HOST", "127.0.0.1"),
            port=int(values.get("APP_PORT", "8001")),
            install_dir=Path(values.get("INSTALL_DIR", str(Path.home() / "RoleModelHelperV2"))).expanduser(),
            service_name=values.get("SERVICE_NAME", "rolemodel-helper-v2.service"),
            database_dsn=values.get("DATABASE_DSN", "postgresql:///rolemodel"),
            migration_dsn=values.get("MIGRATION_DSN") or None,
            database_app_role=values.get("DATABASE_APP_ROLE") or None,
            state_schema=values.get("STATE_SCHEMA", "rolemodel_v2_runtime"),
            v1_port=int(values.get("V1_APP_PORT", "8000")),
            v1_install_dir=Path(values.get("V1_INSTALL_DIR", str(Path.home() / "RoleModelHelper2"))).expanduser(),
            v1_service_name=values.get("V1_SERVICE_NAME", "rolemodel-helper.service"),
            v1_state_schema=values.get("V1_STATE_SCHEMA", "public"),
            tls_verify=_as_bool(values.get("TLS_VERIFY"), True),
            catalog_backend=values.get("CATALOG_BACKEND", "postgres").strip().lower(),
            catalog_dsn=values.get("CATALOG_DSN") or None,
            catalog_schema=values.get("CATALOG_SCHEMA", "rolemodel_v2_catalog"),
            catalog_reader_role=values.get("CATALOG_READER_ROLE") or None,
            catalog_writer_role=values.get("CATALOG_WRITER_ROLE") or None,
            catalog_import_dsn=values.get("CATALOG_IMPORT_DSN") or None,
            v1_catalog_schema=values.get(
                "V1_CATALOG_SCHEMA",
                values.get("V1_STATE_SCHEMA", "public"),
            ),
            catalog_path=(
                Path(values["CATALOG_PATH"]).expanduser()
                if values.get("CATALOG_PATH")
                else None
            ),
            catalog_version=values.get("CATALOG_VERSION") or None,
            catalog_refresh_seconds=float(
                values.get("CATALOG_REFRESH_SECONDS", "5")
            ),
            gigachat_endpoint=values.get(
                "GIGACHAT_ENDPOINT",
                "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            ),
            gigachat_auth_url=values.get(
                "GIGACHAT_AUTH_URL",
                "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
            ),
            gigachat_scope=values.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
            gigachat_model=values.get("GIGACHAT_MODEL", "GigaChat-2-Max"),
            gigachat_access_token=values.get("GIGACHAT_ACCESS_TOKEN") or None,
            gigachat_auth_key=values.get("GIGACHAT_AUTH_KEY") or None,
            gigachat_client_id=values.get("GIGACHAT_CLIENT_ID") or None,
            gigachat_client_secret=values.get("GIGACHAT_CLIENT_SECRET") or None,
            gigachat_cert_file=(
                Path(values["GIGACHAT_CERT_FILE"]).expanduser()
                if values.get("GIGACHAT_CERT_FILE")
                else None
            ),
            gigachat_key_file=(
                Path(values["GIGACHAT_KEY_FILE"]).expanduser()
                if values.get("GIGACHAT_KEY_FILE")
                else None
            ),
            gigachat_ca_bundle=(
                Path(values["GIGACHAT_CA_BUNDLE"]).expanduser()
                if values.get("GIGACHAT_CA_BUNDLE")
                else None
            ),
            planner_deadline_ms=int(values.get("PLANNER_DEADLINE_MS", "12000")),
        )

    @classmethod
    def from_env(cls) -> "Settings":
        prefix = "RMV2_"
        values = {
            key[len(prefix) :]: value
            for key, value in os.environ.items()
            if key.startswith(prefix)
        }
        return cls.from_mapping(values)

    def validate_isolation(self) -> None:
        collisions: list[str] = []
        if self.port == self.v1_port:
            collisions.append(f"port {self.port}")
        if self.service_name == self.v1_service_name:
            collisions.append(f"service {self.service_name}")
        if self.install_dir.resolve() == self.v1_install_dir.resolve():
            collisions.append(f"directory {self.install_dir}")
        if self.state_schema == self.v1_state_schema:
            collisions.append(f"state schema {self.state_schema}")
        if self.catalog_schema == self.v1_state_schema:
            collisions.append(f"catalog schema {self.catalog_schema}")
        if self.catalog_schema == self.v1_catalog_schema:
            collisions.append(f"catalog schema {self.catalog_schema}")
        if self.catalog_schema == self.state_schema:
            collisions.append(
                f"catalog schema must differ from runtime schema {self.state_schema}"
            )
        if not self.database_dsn.startswith(("postgresql://", "postgres://")):
            collisions.append("database DSN must use PostgreSQL")
        if self.migration_dsn and not self.migration_dsn.startswith(
            ("postgresql://", "postgres://")
        ):
            collisions.append("migration DSN must use PostgreSQL")
        if self.catalog_backend not in {"postgres", "json"}:
            collisions.append(
                f"unsupported catalog backend {self.catalog_backend!r}"
            )
        if self.catalog_backend == "postgres":
            if not self.effective_catalog_dsn.startswith(
                ("postgresql://", "postgres://")
            ):
                collisions.append("catalog DSN must use PostgreSQL")
            if self.catalog_import_dsn and not self.catalog_import_dsn.startswith(
                ("postgresql://", "postgres://")
            ):
                collisions.append("catalog import DSN must use PostgreSQL")
        elif self.catalog_path is None or self.catalog_version is None:
            collisions.append(
                "JSON catalog requires explicit CATALOG_PATH and CATALOG_VERSION"
            )
        if self.catalog_refresh_seconds <= 0:
            collisions.append("catalog refresh interval must be positive")
        if collisions:
            raise IsolationError("V2 isolation collision: " + ", ".join(collisions))

    @property
    def effective_migration_dsn(self) -> str:
        return self.migration_dsn or self.database_dsn

    @property
    def effective_catalog_dsn(self) -> str:
        return self.catalog_dsn or self.database_dsn

    @property
    def gigachat_enabled(self) -> bool:
        if self.gigachat_access_token or self.gigachat_auth_key:
            return True
        if self.gigachat_client_id and self.gigachat_client_secret:
            return True
        return bool(self.gigachat_cert_file and self.gigachat_key_file)
