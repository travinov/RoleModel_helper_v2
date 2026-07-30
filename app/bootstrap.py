from __future__ import annotations

from app.agent.service import AgentEngine
from app.api import create_app
from app.catalog.cache import CatalogCache
from app.catalog.json_source import JsonCatalogSource
from app.catalog.postgres import PostgresCatalogSource
from app.config import Settings
from app.providers.gigachat import (
    GigaChatHttpPlanner,
    GigaChatTokenManager,
    RequestsJsonTransport,
)
from app.runtime.postgres import PostgresStateStore
from app.runtime.service import RuntimeService


class UnavailablePlanner:
    def plan(self, request, context, *, deadline_ms: int):
        raise RuntimeError("GigaChat is not configured")


def build_runtime(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    settings.validate_isolation()

    if settings.catalog_backend == "postgres":
        catalog_source = PostgresCatalogSource(
            dsn=settings.effective_catalog_dsn,
            schema=settings.catalog_schema,
        )
        catalog_source.verify_ready()
        catalog_version = settings.catalog_version or catalog_source.active_version()
        if catalog_version is None:
            raise RuntimeError("V2 PostgreSQL catalog has no active release")
    else:
        if settings.catalog_path is None or settings.catalog_version is None:
            raise RuntimeError(
                "JSON catalog requires explicit path and version"
            )
        catalog_source = JsonCatalogSource(settings.catalog_path)
        catalog_version = settings.catalog_version
    catalog_cache = CatalogCache(catalog_source)
    refresh = catalog_cache.refresh(catalog_version)
    if not refresh.activated and catalog_cache.status.active_version is None:
        raise RuntimeError(f"V2 catalog failed to load: {refresh.error}")

    planner = _build_planner(settings)
    engine = AgentEngine(
        catalog_cache=catalog_cache,
        planner=planner,
        planner_deadline_ms=settings.planner_deadline_ms,
    )
    runtime = RuntimeService(
        state_store=PostgresStateStore(
            dsn=settings.database_dsn,
            schema=settings.state_schema,
        ),
        engine=engine,
    )
    runtime.initialize()
    return settings, catalog_cache, runtime


def build_default_app():
    settings, catalog_cache, runtime = build_runtime()
    return create_app(
        message_service=runtime,
        catalog_cache=catalog_cache,
        settings=settings,
    )


def _build_planner(settings: Settings):
    if not settings.gigachat_enabled:
        return UnavailablePlanner()
    verify: bool | str = (
        str(settings.gigachat_ca_bundle)
        if settings.gigachat_ca_bundle is not None
        else settings.tls_verify
    )
    cert = (
        (str(settings.gigachat_cert_file), str(settings.gigachat_key_file))
        if settings.gigachat_cert_file is not None and settings.gigachat_key_file is not None
        else None
    )
    transport = RequestsJsonTransport(verify=verify, cert=cert)
    if settings.gigachat_access_token:
        return GigaChatHttpPlanner(
            endpoint=settings.gigachat_endpoint,
            access_token=settings.gigachat_access_token,
            transport=transport,
            model=settings.gigachat_model,
        )
    token_manager = GigaChatTokenManager(
        auth_url=settings.gigachat_auth_url,
        scope=settings.gigachat_scope,
        auth_key=settings.gigachat_auth_key,
        client_id=settings.gigachat_client_id,
        client_secret=settings.gigachat_client_secret,
        verify=verify,
        cert=cert,
    )
    return GigaChatHttpPlanner(
        endpoint=settings.gigachat_endpoint,
        token_provider=token_manager,
        transport=transport,
        model=settings.gigachat_model,
    )
