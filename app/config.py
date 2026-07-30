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
    state_schema: str = "rolemodel_helper_v2"
    v1_port: int = 8000
    v1_install_dir: Path = Path.home() / "RoleModelHelper2"
    v1_service_name: str = "rolemodel-helper.service"
    v1_state_schema: str = "public"
    tls_verify: bool = True
    catalog_path: Path = Path(__file__).resolve().parents[1] / "data" / "demo_catalog.json"
    catalog_version: str = "demo-v1"
    state_database_path: Path = Path.home() / "RoleModelHelperV2" / "data" / "state.sqlite3"
    v1_state_database_path: Path = Path.home() / "RoleModelHelper2" / "data" / "state.sqlite3"
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
            state_schema=values.get("STATE_SCHEMA", "rolemodel_helper_v2"),
            v1_port=int(values.get("V1_APP_PORT", "8000")),
            v1_install_dir=Path(values.get("V1_INSTALL_DIR", str(Path.home() / "RoleModelHelper2"))).expanduser(),
            v1_service_name=values.get("V1_SERVICE_NAME", "rolemodel-helper.service"),
            v1_state_schema=values.get("V1_STATE_SCHEMA", "public"),
            tls_verify=_as_bool(values.get("TLS_VERIFY"), True),
            catalog_path=Path(
                values.get(
                    "CATALOG_PATH",
                    str(Path(__file__).resolve().parents[1] / "data" / "demo_catalog.json"),
                )
            ).expanduser(),
            catalog_version=values.get("CATALOG_VERSION", "demo-v1"),
            state_database_path=Path(
                values.get(
                    "STATE_DATABASE_PATH",
                    str(Path.home() / "RoleModelHelperV2" / "data" / "state.sqlite3"),
                )
            ).expanduser(),
            v1_state_database_path=Path(
                values.get(
                    "V1_STATE_DATABASE_PATH",
                    str(Path.home() / "RoleModelHelper2" / "data" / "state.sqlite3"),
                )
            ).expanduser(),
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
        state_path = self.state_database_path.resolve()
        v1_state_path = self.v1_state_database_path.resolve()
        v1_dir = self.v1_install_dir.resolve()
        if state_path == v1_state_path or v1_dir in state_path.parents:
            collisions.append(f"state database {self.state_database_path}")
        if collisions:
            raise IsolationError("V2 isolation collision: " + ", ".join(collisions))

    @property
    def gigachat_enabled(self) -> bool:
        if self.gigachat_access_token or self.gigachat_auth_key:
            return True
        if self.gigachat_client_id and self.gigachat_client_secret:
            return True
        return bool(self.gigachat_cert_file and self.gigachat_key_file)
