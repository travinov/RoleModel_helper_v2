from __future__ import annotations

import re
import time
import uuid
from dataclasses import replace
from difflib import SequenceMatcher
from typing import Any, Protocol

from app.agent.models import (
    Answer,
    Candidate,
    Failure,
    Outcome,
    PendingQuestion,
    Plan,
    RouteSource,
    TurnRequest,
    TurnResult,
    TurnState,
)
from app.catalog.cache import CatalogBundle, CatalogCache
from app.telemetry import TurnTrace


class Planner(Protocol):
    def plan(self, request: TurnRequest, context: CatalogBundle, *, deadline_ms: int) -> Plan: ...


def _normalize(text: str) -> str:
    return " ".join(re.sub(r"[^0-9a-zа-яё]+", " ", text.lower().replace("ё", "е")).split())


class AgentEngine:
    def __init__(
        self,
        *,
        catalog_cache: CatalogCache,
        planner: Planner,
        planner_deadline_ms: int = 12_000,
    ) -> None:
        self._catalog_cache = catalog_cache
        self._planner = planner
        self._planner_deadline_ms = planner_deadline_ms

    def handle(self, request: TurnRequest) -> TurnResult:
        bundle = self._catalog_cache.get()
        trace = TurnTrace.start(
            trace_id=str(uuid.uuid4()),
            request_id=request.request_id,
            catalog_version=bundle.version,
            cache_hit=True,
        )
        with trace.component("agent"):
            deterministic = self._deterministic(request, bundle)
        if deterministic is not None:
            outcome, state, answer, failure = deterministic
            diagnostics = trace.finish(
                route=RouteSource.DETERMINISTIC,
                outcome=outcome,
                gigachat_calls=0,
            )
            return TurnResult(
                route=RouteSource.DETERMINISTIC,
                outcome=outcome,
                state=state,
                answer=answer,
                failure=failure,
                diagnostics=diagnostics,
            )

        with trace.component("gigachat"):
            try:
                plan = self._planner.plan(
                    request,
                    bundle,
                    deadline_ms=self._planner_deadline_ms,
                )
            except TimeoutError:
                return self._provider_rejection(
                    request,
                    bundle,
                    trace,
                    Outcome.PROVIDER_FAILURE,
                    "PROVIDER_TIMEOUT",
                    "GigaChat не успел ответить. Повторите запрос.",
                    retryable=True,
                )
            except Exception:
                return self._provider_rejection(
                    request,
                    bundle,
                    trace,
                    Outcome.PROVIDER_FAILURE,
                    "PROVIDER_ERROR",
                    "GigaChat временно недоступен. Повторите запрос.",
                    retryable=True,
                )

        if not isinstance(plan, Plan):
            return self._provider_rejection(
                request,
                bundle,
                trace,
                Outcome.NEEDS_CLARIFICATION,
                "MALFORMED_PLAN",
                "Не удалось однозначно понять запрос. Уточните АС или требуемую роль.",
            )
        allowed_actions = {
            "ROLE_DISCOVERY": {"SEARCH_ROLES"},
            "SYSTEM_DISCOVERY": {"SEARCH_SYSTEMS"},
            "ROLE_ACQUISITION": {"GET_INSTRUCTION"},
            "INSTRUCTION_LOOKUP": {"GET_INSTRUCTION"},
        }
        allowed_slot_keys = {"system_id", "city", "department"}
        plan_keys = {str(key) for key in plan.slots}
        if (
            plan.intent not in allowed_actions
            or plan.action not in allowed_actions[plan.intent]
            or not plan_keys.issubset(allowed_slot_keys)
            or not 0.0 <= plan.confidence <= 1.0
        ):
            return self._provider_rejection(
                request,
                bundle,
                trace,
                Outcome.NEEDS_CLARIFICATION,
                "MALFORMED_PLAN",
                "Не удалось однозначно понять запрос. Уточните АС или требуемую роль.",
            )
        if plan.confidence < 0.55:
            return self._provider_rejection(
                request,
                bundle,
                trace,
                Outcome.NEEDS_CLARIFICATION,
                "LOW_CONFIDENCE",
                "Запрос понят неуверенно. Уточните АС и ожидаемое действие.",
            )
        if plan.catalog_version != bundle.version:
            return self._provider_rejection(
                request,
                bundle,
                trace,
                Outcome.NEEDS_CLARIFICATION,
                "STALE_PLAN",
                "Каталог обновился во время запроса. Повторите формулировку.",
            )
        system_id = str(plan.slots.get("system_id") or "")
        city = str(plan.slots.get("city") or "")
        department = str(plan.slots.get("department") or "")
        valid_cities = {
            _normalize(str(item.get("city") or ""))
            for item in bundle.departments
        }
        valid_departments = {
            _normalize(str(item.get("name") or ""))
            for item in bundle.departments
        }
        if (
            (system_id and system_id not in bundle.systems)
            or (city and _normalize(city) not in valid_cities)
            or (department and _normalize(department) not in valid_departments)
        ):
            return self._provider_rejection(
                request,
                bundle,
                trace,
                Outcome.NEEDS_CLARIFICATION,
                "UNKNOWN_CATALOG_FACT",
                "Указанная АС не найдена в актуальном каталоге. Уточните название.",
            )

        state = replace(
            request.state,
            revision=request.state.revision + 1,
            intent=plan.intent,
            phase="ANSWERED" if system_id else "AWAITING_SLOT",
            slots={**dict(request.state.slots), **dict(plan.slots)},
            pending_question=None if system_id else PendingQuestion(topic="system", kind="slot"),
        )
        if system_id:
            if plan.intent in {"INSTRUCTION_LOOKUP", "ROLE_ACQUISITION"}:
                answer = self._instruction_answer(bundle, system_id)
            elif plan.intent == "SYSTEM_DISCOVERY":
                system = bundle.systems[system_id]
                answer = Answer(
                    text=f"АС {system['name']} найдена в актуальном каталоге.",
                    answer_type="SYSTEM_DISCOVERY",
                    facts={
                        "system_id": system_id,
                        "catalog_version": bundle.version,
                    },
                )
            else:
                answer = self._roles_answer(bundle, system_id)
            outcome = Outcome.HANDLED
        else:
            answer = Answer(
                text="Уточните название автоматизированной системы.",
                answer_type=plan.intent,
            )
            outcome = Outcome.NEEDS_CLARIFICATION
        diagnostics = trace.finish(
            route=RouteSource.GIGACHAT_FALLBACK,
            outcome=outcome,
            gigachat_calls=1,
        )
        return TurnResult(
            route=RouteSource.GIGACHAT_FALLBACK,
            outcome=outcome,
            state=state,
            answer=answer,
            diagnostics=diagnostics,
        )

    def _deterministic(
        self,
        request: TurnRequest,
        bundle: CatalogBundle,
    ) -> tuple[Outcome, TurnState, Answer | None, Failure | None] | None:
        text_norm = _normalize(request.text)
        state = request.state

        if any(marker in text_norm for marker in ("начать заново", "сброс", "очистить контекст")):
            reset = TurnState(
                session_id=state.session_id,
                revision=state.revision + 1,
                phase="READY",
                slots={},
            )
            return Outcome.HANDLED, reset, Answer("Контекст сброшен. Опишите новый вопрос.", "RESET"), None

        pending = state.pending_question
        if pending is not None and pending.kind == "slot" and pending.topic == "intent":
            system_id = str(state.slots.get("system_id") or "")
            if system_id in bundle.systems and (
                "роль" in text_norm or "роли" in text_norm or "доступы" in text_norm
            ):
                updated = replace(
                    state,
                    revision=state.revision + 1,
                    intent="ROLE_DISCOVERY",
                    phase="ANSWERED",
                    pending_question=None,
                )
                return Outcome.HANDLED, updated, self._roles_answer(bundle, system_id), None
            if system_id in bundle.systems and (
                "инструкция" in text_norm
                or "как получить" in text_norm
                or "как запросить" in text_norm
            ):
                updated = replace(
                    state,
                    revision=state.revision + 1,
                    intent="INSTRUCTION_LOOKUP",
                    phase="ANSWERED",
                    pending_question=None,
                )
                return Outcome.HANDLED, updated, self._instruction_answer(bundle, system_id), None
            updated = replace(state, revision=state.revision + 1)
            return (
                Outcome.NEEDS_CLARIFICATION,
                updated,
                Answer("Выберите: роли или инструкция по доступу.", "CLARIFICATION"),
                None,
            )
        pending_value_is_simple = (
            len(text_norm.split()) <= 5
            and not any(marker in text_norm for marker in ("вопрос", "доступ", "роль", "система"))
        )
        if pending is not None and pending.kind == "slot" and pending_value_is_simple:
            return self._handle_pending_slot(
                request=request,
                state=state,
                pending=pending,
                bundle=bundle,
            )

        if pending is not None and pending.kind == "candidate_selection":
            if "еще" in text_norm:
                updated = replace(state, revision=state.revision + 1)
                return Outcome.NEEDS_CLARIFICATION, updated, Answer("Других вариантов на этой странице нет.", state.intent or "UNKNOWN"), None
            if text_norm.isdigit():
                index = int(text_norm) - 1
                if 0 <= index < len(pending.options):
                    selected = pending.options[index]
                    updated = replace(
                        state,
                        revision=state.revision + 1,
                        phase="ANSWERED",
                        slots={**dict(state.slots), f"{pending.topic}_id": selected.id},
                        pending_question=None,
                    )
                    return Outcome.HANDLED, updated, Answer(f"Выбран вариант: {selected.label}.", state.intent or "UNKNOWN"), None

        safe, weak = self._system_matches(text_norm, bundle)
        if safe is not None:
            system_id = str(safe["id"])
            instruction_query = any(
                marker in text_norm
                for marker in (
                    "как получить доступ",
                    "как запросить доступ",
                    "инструкция",
                    "порядок получения",
                )
            )
            if instruction_query:
                updated = replace(
                    state,
                    revision=state.revision + 1,
                    intent="INSTRUCTION_LOOKUP",
                    phase="ANSWERED",
                    slots={**dict(state.slots), "system_id": system_id},
                    pending_question=None,
                )
                return (
                    Outcome.HANDLED,
                    updated,
                    self._instruction_answer(bundle, system_id),
                    None,
                )
            role_query = (
                "роль" in text_norm
                or "роли" in text_norm
                or "доступы" in text_norm
            )
            context_change = any(
                marker in text_norm
                for marker in ("перейти на", "сменить ас", "другая ас", "другую ас")
            )
            if not role_query:
                updated = replace(
                    state,
                    revision=state.revision + 1,
                    phase="AWAITING_INTENT",
                    slots={**dict(state.slots), "system_id": system_id},
                    pending_question=PendingQuestion(topic="intent", kind="slot"),
                )
                answer_text = (
                    f"Фокус переключён на АС {safe['name']}. Что нужно: роли или инструкция?"
                    if context_change
                    else f"АС {safe['name']} найдена. Уточните: нужны роли или инструкция по доступу?"
                )
                return (
                    Outcome.HANDLED if context_change else Outcome.NEEDS_CLARIFICATION,
                    updated,
                    Answer(answer_text, "CONTEXT_UPDATE" if context_change else "CLARIFICATION"),
                    None,
                )
            updated = replace(
                state,
                revision=state.revision + 1,
                intent="ROLE_DISCOVERY",
                phase="ANSWERED",
                slots={**dict(state.slots), "system_id": system_id},
                pending_question=None,
            )
            return Outcome.HANDLED, updated, self._roles_answer(bundle, system_id), None

        if weak:
            options = tuple(
                Candidate(
                    id=str(item["id"]),
                    label=str(item["name"]),
                    confidence=float(score),
                )
                for item, score in weak[:5]
            )
            updated = replace(
                state,
                revision=state.revision + 1,
                intent="ROLE_DISCOVERY",
                phase="AWAITING_SELECTION",
                pending_question=PendingQuestion(
                    topic="system",
                    kind="candidate_selection",
                    options=options,
                ),
            )
            return (
                Outcome.NEEDS_CLARIFICATION,
                updated,
                Answer("Нашлось несколько похожих АС. Выберите нужную.", "ROLE_DISCOVERY"),
                None,
            )

        return None

    def _handle_pending_slot(
        self,
        *,
        request: TurnRequest,
        state: TurnState,
        pending: PendingQuestion,
        bundle: CatalogBundle,
    ) -> tuple[Outcome, TurnState, Answer | None, Failure | None]:
        value_norm = _normalize(request.text)
        canonical: str | None = None
        slot_key = pending.topic
        if slot_key == "city":
            canonical = next(
                (
                    str(item["city"])
                    for item in bundle.departments
                    if _normalize(str(item.get("city") or "")) == value_norm
                ),
                None,
            )
        elif slot_key == "department":
            canonical = next(
                (
                    str(item["name"])
                    for item in bundle.departments
                    if _normalize(str(item.get("name") or "")) == value_norm
                ),
                None,
            )
        elif slot_key == "system":
            safe, weak = self._system_matches(value_norm, bundle)
            if safe is not None:
                canonical = str(safe["id"])
                slot_key = "system_id"
            elif weak:
                options = tuple(
                    Candidate(
                        id=str(item["id"]),
                        label=str(item["name"]),
                        confidence=float(score),
                    )
                    for item, score in weak[:5]
                )
                updated = replace(
                    state,
                    revision=state.revision + 1,
                    pending_question=PendingQuestion(
                        topic="system",
                        kind="candidate_selection",
                        options=options,
                    ),
                )
                return (
                    Outcome.NEEDS_CLARIFICATION,
                    updated,
                    Answer("Выберите точное название АС.", state.intent or "UNKNOWN"),
                    None,
                )

        if canonical is None:
            updated = replace(state, revision=state.revision + 1)
            return (
                Outcome.NEEDS_CLARIFICATION,
                updated,
                Answer(
                    f"Значение «{request.text.strip()}» не найдено в актуальном каталоге. Уточните {pending.topic}.",
                    state.intent or "UNKNOWN",
                ),
                None,
            )
        updated = replace(
            state,
            revision=state.revision + 1,
            phase="READY",
            slots={**dict(state.slots), slot_key: canonical},
            pending_question=None,
        )
        return (
            Outcome.HANDLED,
            updated,
            Answer("Контекст подтверждён. Уточните следующий параметр.", state.intent or "UNKNOWN"),
            None,
        )

    @staticmethod
    def _system_matches(
        text_norm: str,
        bundle: CatalogBundle,
    ) -> tuple[dict[str, Any] | None, list[tuple[dict[str, Any], float]]]:
        weak: list[tuple[dict[str, Any], float]] = []
        for system in bundle.systems.values():
            aliases = (
                {"value": system["name"], "safety": "SAFE"},
                *system["aliases"],
            )
            for alias in aliases:
                alias_norm = _normalize(str(alias["value"]))
                safety = str(alias.get("safety") or "SAFE").upper()
                if not alias_norm:
                    continue
                if safety == "SAFE" and re.search(rf"(?<!\w){re.escape(alias_norm)}(?!\w)", text_norm):
                    return dict(system), []
                if re.search(rf"(?<!\w){re.escape(alias_norm)}(?!\w)", text_norm):
                    weak.append((dict(system), 0.65))
                    break
                score = SequenceMatcher(None, alias_norm, text_norm).ratio()
                if score >= 0.50:
                    weak.append((dict(system), min(score, 0.64)))
                    break
        weak.sort(key=lambda item: item[1], reverse=True)
        unique: dict[str, tuple[dict[str, Any], float]] = {}
        for item in weak:
            unique.setdefault(str(item[0]["id"]), item)
        return None, list(unique.values())

    @staticmethod
    def _roles_answer(bundle: CatalogBundle, system_id: str) -> Answer:
        system = bundle.systems[system_id]
        roles = list(system["roles"])
        if roles:
            lines = [
                f"Роли для АС {system['name']}:",
                *[
                    f"{index}. {role['name']} ({'по умолчанию' if int(role.get('access_level') or 0) == 1 else 'по запросу'})"
                    for index, role in enumerate(roles, start=1)
                ],
            ]
        else:
            lines = [f"Для АС {system['name']} роли в текущем каталоге не найдены."]
        return Answer(
            text="\n".join(lines),
            answer_type="ROLE_DISCOVERY",
            facts={
                "system_id": system_id,
                "role_ids": [str(role["id"]) for role in roles],
                "catalog_version": bundle.version,
            },
        )

    @staticmethod
    def _instruction_answer(bundle: CatalogBundle, system_id: str) -> Answer:
        system = bundle.systems[system_id]
        instruction = next(
            (
                item
                for item in bundle.instructions
                if str(item.get("system_id") or "") == system_id
            ),
            None,
        )
        if instruction is None:
            text = (
                f"Для АС {system['name']} отдельная инструкция в каталоге не найдена. "
                "Обратитесь к владельцу системы или в службу поддержки доступов."
            )
            facts: dict[str, Any] = {"system_id": system_id, "catalog_version": bundle.version}
        else:
            text = (
                f"{instruction['title']} для АС {system['name']}. "
                "Откройте источник инструкции в корпоративном каталоге."
            )
            facts = {
                "system_id": system_id,
                "instruction_id": str(instruction["id"]),
                "citation": str(instruction.get("citation") or ""),
                "catalog_version": bundle.version,
            }
        return Answer(text=text, answer_type="INSTRUCTION_LOOKUP", facts=facts)

    @staticmethod
    def _provider_rejection(
        request: TurnRequest,
        bundle: CatalogBundle,
        trace: TurnTrace,
        outcome: Outcome,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> TurnResult:
        diagnostics = trace.finish(
            route=RouteSource.GIGACHAT_FALLBACK,
            outcome=outcome,
            gigachat_calls=1,
        )
        return TurnResult(
            route=RouteSource.GIGACHAT_FALLBACK,
            outcome=outcome,
            state=request.state,
            answer=None,
            failure=Failure(code=code, user_message=message, retryable=retryable),
            diagnostics=diagnostics,
        )
