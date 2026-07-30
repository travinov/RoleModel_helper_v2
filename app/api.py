from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.runtime.service import SessionNotFound, StateRevisionConflict


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)
    state_revision: int = Field(default=0, ge=0)

    @field_validator("request_id", "text")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


def create_app(
    *,
    message_service: Any,
    catalog_cache: Any | None = None,
    settings: Any | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        should_refresh = (
            catalog_cache is not None
            and getattr(settings, "catalog_backend", None) == "postgres"
            and hasattr(catalog_cache, "start_auto_refresh")
        )
        if should_refresh:
            catalog_cache.start_auto_refresh(
                getattr(settings, "catalog_refresh_seconds", 5.0)
            )
        try:
            yield
        finally:
            if should_refresh:
                catalog_cache.stop_auto_refresh()

    app = FastAPI(
        title="RoleModel Helper V2",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/api/v2/health")
    async def health() -> dict[str, Any]:
        cache_status = catalog_cache.status if catalog_cache is not None else None
        ready = cache_status is None or cache_status.active_version is not None
        return {
            "status": "ok" if ready else "degraded",
            "version": "v2",
            "catalog_ready": ready,
            "catalog_version": cache_status.active_version if cache_status is not None else None,
            "catalog_freshness": cache_status.freshness.value if cache_status is not None else None,
            "gigachat_enabled": bool(getattr(settings, "gigachat_enabled", False)),
            "port": getattr(settings, "port", None),
        }

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        path = Path(__file__).parent / "static" / "index.html"
        return await asyncio.to_thread(path.read_text, encoding="utf-8")

    @app.post("/api/v2/sessions")
    async def create_session() -> Any:
        return await asyncio.to_thread(message_service.create_session)

    @app.get("/api/v2/sessions/{session_id}")
    async def get_session(session_id: str) -> Any:
        try:
            return await asyncio.to_thread(message_service.get_session, session_id)
        except SessionNotFound as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc

    @app.post("/api/v2/sessions/{session_id}/messages")
    async def post_message(session_id: str, payload: MessageRequest) -> Any:
        try:
            if inspect.iscoroutinefunction(message_service.post_message):
                return await message_service.post_message(
                    session_id=session_id,
                    request_id=payload.request_id,
                    text=payload.text,
                    state_revision=payload.state_revision,
                )
            return await asyncio.to_thread(
                message_service.post_message,
                session_id=session_id,
                request_id=payload.request_id,
                text=payload.text,
                state_revision=payload.state_revision,
            )
        except SessionNotFound as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc
        except StateRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app
