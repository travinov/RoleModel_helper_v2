from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import requests

from app.agent.models import Plan, TurnRequest
from app.catalog.cache import CatalogBundle
from app.tools.catalog import CatalogSearchTools


class PlannerResponseError(ValueError):
    pass


def _expiration_epoch(value: Any) -> float:
    parsed = float(value)
    return parsed / 1000.0 if parsed > 2_000_000_000 else parsed


class JsonTransport(Protocol):
    def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_ms: int,
    ) -> dict[str, Any]: ...


class TokenProvider(Protocol):
    def bearer_token(self, *, deadline_ms: int) -> str: ...


class RequestsJsonTransport:
    """Persistent HTTP transport; metadata stays local and is not sent upstream."""

    def __init__(self, *, verify: bool | str = True, cert: tuple[str, str] | None = None) -> None:
        self._session = requests.Session()
        self._verify = verify
        self._cert = cert

    def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_ms: int,
    ) -> dict[str, Any]:
        provider_payload = {
            key: value
            for key, value in payload.items()
            if key in {"model", "messages", "temperature", "stream", "max_tokens"}
        }
        try:
            response = self._session.post(
                url,
                headers=headers,
                json=provider_payload,
                timeout=max(timeout_ms / 1000.0, 0.05),
                verify=self._verify,
                cert=self._cert,
            )
        except requests.Timeout as exc:
            raise TimeoutError("GigaChat completion deadline exceeded") from exc
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise PlannerResponseError("GigaChat response must be a JSON object")
        return body


class StaticTokenProvider:
    def __init__(self, token: str) -> None:
        self._token = token

    def bearer_token(self, *, deadline_ms: int) -> str:
        return self._token


class GigaChatTokenManager:
    """Thread-safe OAuth token cache supporting basic credentials and mTLS."""

    def __init__(
        self,
        *,
        auth_url: str,
        scope: str,
        auth_key: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        verify: bool | str = True,
        cert: tuple[str, str] | None = None,
    ) -> None:
        self._auth_url = auth_url
        self._scope = scope
        self._auth_key = auth_key
        self._client_id = client_id
        self._client_secret = client_secret
        self._verify = verify
        self._cert = cert
        self._session = requests.Session()
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def bearer_token(self, *, deadline_ms: int) -> str:
        now = time.time()
        if self._token and now < self._expires_at:
            return self._token
        with self._lock:
            now = time.time()
            if self._token and now < self._expires_at:
                return self._token
            authorization = self._authorization()
            headers = {
                "RqUID": str(uuid.uuid4()),
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            }
            if authorization:
                headers["Authorization"] = authorization
            try:
                response = self._session.post(
                    self._auth_url,
                    headers=headers,
                    data={"scope": self._scope},
                    timeout=max(deadline_ms / 1000.0, 0.05),
                    verify=self._verify,
                    cert=self._cert,
                )
            except requests.Timeout as exc:
                raise TimeoutError("GigaChat OAuth deadline exceeded") from exc
            response.raise_for_status()
            body = response.json()
            token = str(body.get("access_token") or "")
            if not token:
                raise PlannerResponseError("GigaChat token response has no access_token")
            expires_at = body.get("expires_at")
            try:
                parsed_expiry = _expiration_epoch(expires_at)
            except (TypeError, ValueError):
                parsed_expiry = time.time() + 1_700.0
            self._token = token
            self._expires_at = max(time.time() + 30.0, parsed_expiry - 60.0)
            return token

    def _authorization(self) -> str | None:
        if self._auth_key:
            return f"Basic {self._auth_key}"
        if self._client_id and self._client_secret:
            encoded = base64.b64encode(
                f"{self._client_id}:{self._client_secret}".encode()
            ).decode()
            return f"Basic {encoded}"
        if self._cert:
            return None
        raise PlannerResponseError("GigaChat credentials are not configured")


@dataclass
class GigaChatHttpPlanner:
    endpoint: str
    token_provider: TokenProvider
    transport: JsonTransport
    model: str = "GigaChat-2-Max"
    clock: Callable[[], float] = time.monotonic

    def __init__(
        self,
        *,
        endpoint: str,
        access_token: str | None = None,
        token_provider: TokenProvider | None = None,
        transport: JsonTransport | None = None,
        model: str = "GigaChat-2-Max",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if token_provider is None:
            if not access_token:
                raise ValueError("access_token or token_provider is required")
            token_provider = StaticTokenProvider(access_token)
        self.endpoint = endpoint
        self.token_provider = token_provider
        self.transport = transport or RequestsJsonTransport()
        self.model = model
        self.clock = clock

    def plan(
        self,
        request: TurnRequest,
        context: CatalogBundle,
        *,
        deadline_ms: int,
    ) -> Plan:
        started_at = self.clock()
        catalog_candidates = self._catalog_candidates(request, context)
        state = {
            "intent": request.state.intent,
            "phase": request.state.phase,
            "slots": dict(request.state.slots),
            "pending_topic": (
                request.state.pending_question.topic
                if request.state.pending_question is not None
                else None
            ),
        }
        system_prompt = (
            "Ты планировщик агента по ролевой модели. Верни только JSON: "
            "catalog_version, intent, action, slots, confidence. "
            "intent: SYSTEM_DISCOVERY, ROLE_DISCOVERY, ROLE_ACQUISITION или INSTRUCTION_LOOKUP. "
            "Не придумывай catalog id: используй только id из ограниченного "
            "catalog_candidates. Если подходящего id нет, оставь соответствующий "
            "slot пустым. Каталог целиком тебе не передаётся."
        )
        user_payload = {
            "catalog_version": context.version,
            "request_id": request.request_id,
            "text": request.text[:2000],
            "state": state,
            "catalog_candidates": catalog_candidates,
        }
        payload = {
            "catalog_version": context.version,
            "request_id": request.request_id,
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False),
                },
            ],
            "temperature": 0.0,
            "stream": False,
            "max_tokens": 350,
        }
        token = self.token_provider.bearer_token(deadline_ms=deadline_ms)
        elapsed_ms = int((self.clock() - started_at) * 1000.0)
        remaining_ms = deadline_ms - elapsed_ms
        if remaining_ms <= 0:
            raise TimeoutError("GigaChat end-to-end planner deadline exceeded")
        body = self.transport.post_json(
            url=self.endpoint,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            payload=payload,
            timeout_ms=remaining_ms,
        )
        try:
            choices = body["choices"]
            content = choices[0]["message"]["content"]
            parsed = json.loads(content)
            return Plan(
                catalog_version=str(parsed["catalog_version"]),
                intent=str(parsed["intent"]),
                action=str(parsed["action"]),
                slots={
                    str(key): str(value)
                    for key, value in dict(parsed.get("slots") or {}).items()
                },
                confidence=float(parsed.get("confidence") or 0.0),
            )
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PlannerResponseError("GigaChat returned malformed structured plan") from exc

    @staticmethod
    def _catalog_candidates(
        request: TurnRequest,
        context: CatalogBundle,
    ) -> dict[str, list[dict[str, Any]]]:
        tools = CatalogSearchTools(context)
        system_matches: dict[str, dict[str, Any]] = {}
        direct_systems = tools.search_systems(request.text, limit=5)
        for candidate in direct_systems.candidates:
            system_matches[candidate.id] = {
                "id": candidate.id,
                "label": candidate.label,
                "score": candidate.score,
                "matched_roles": [],
            }

        for system_id, system in context.systems.items():
            role_matches = tools.search_roles(
                system_id=system_id,
                query=request.text,
                limit=3,
            )
            if not role_matches.candidates:
                continue
            entry = system_matches.setdefault(
                system_id,
                {
                    "id": system_id,
                    "label": str(system["name"]),
                    "score": 0.0,
                    "matched_roles": [],
                },
            )
            entry["score"] = max(
                float(entry["score"]),
                max(item.score for item in role_matches.candidates),
            )
            entry["matched_roles"] = [
                {
                    "id": item.id,
                    "label": item.label,
                    "access_level": item.context.get("access_level"),
                }
                for item in role_matches.candidates[:3]
            ]

        state_system_id = str(request.state.slots.get("system_id") or "")
        if state_system_id in context.systems:
            system = context.systems[state_system_id]
            system_matches[state_system_id] = {
                "id": state_system_id,
                "label": str(system["name"]),
                "score": 1.0,
                "matched_roles": system_matches.get(
                    state_system_id, {}
                ).get("matched_roles", []),
            }

        systems = sorted(
            system_matches.values(),
            key=lambda item: (-float(item["score"]), str(item["label"]), str(item["id"])),
        )[:5]
        departments = [
            {
                "id": item.id,
                "label": item.label,
                "city": item.context.get("city"),
                "number": item.context.get("number"),
                "score": item.score,
            }
            for item in tools.search_departments(request.text, limit=5).candidates
        ]
        positions = [
            {
                "id": item.id,
                "label": item.label,
                "score": item.score,
            }
            for item in tools.search_positions(request.text, limit=5).candidates
        ]
        return {
            "systems": systems,
            "departments": departments,
            "positions": positions,
        }
