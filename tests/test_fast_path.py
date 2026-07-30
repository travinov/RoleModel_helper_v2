from __future__ import annotations

import unittest

from app.agent.models import (
    Candidate,
    Outcome,
    PendingQuestion,
    Plan,
    RouteSource,
    TurnRequest,
    TurnState,
)
from app.agent.service import AgentEngine
from app.catalog.cache import CatalogBundle, CatalogCache

from tests.fakes import RecordingPlanner, VersionedCatalogSource, load_catalog_mapping


def make_cache() -> CatalogCache:
    mapping = load_catalog_mapping("catalog_v42.json")
    source = VersionedCatalogSource(
        {"v42": mapping},
        bundle_factory=CatalogBundle.from_mapping,
    )
    cache = CatalogCache(source)
    cache.refresh("v42")
    return cache


class FastPathTests(unittest.TestCase):
    def test_exact_safe_alias_answers_without_gigachat(self) -> None:
        planner = RecordingPlanner(error=AssertionError("GigaChat must not be called"))
        engine = AgentEngine(catalog_cache=make_cache(), planner=planner)

        result = engine.handle(
            TurnRequest(
                request_id="req-exact",
                text="Покажи роли в АС СберДруг",
                state=TurnState.empty(session_id="session-1"),
            )
        )

        self.assertEqual(result.route, RouteSource.DETERMINISTIC)
        self.assertEqual(result.outcome, Outcome.HANDLED)
        self.assertEqual(result.state.slots["system_id"], "sberdrug")
        self.assertEqual(result.answer.answer_type, "ROLE_DISCOVERY")
        self.assertIn("СберДруг", result.answer.text)
        self.assertEqual(result.diagnostics.catalog_version, "v42")
        self.assertEqual(result.diagnostics.gigachat_calls, 0)
        self.assertEqual(planner.calls, [])

    def test_pending_selection_paging_reset_and_system_change_are_deterministic(self) -> None:
        candidate_state = TurnState(
            session_id="session-1",
            revision=4,
            intent="ROLE_DISCOVERY",
            phase="AWAITING_SELECTION",
            slots={"system_id": "sberdrug"},
            pending_question=PendingQuestion(
                topic="role",
                kind="candidate_selection",
                options=(
                    Candidate(id="role-reader-v42", label="Читатель заявок", confidence=1.0),
                    Candidate(id="role-approver-v42", label="Согласующий заявок", confidence=1.0),
                ),
                page=0,
            ),
        )
        pending_city_state = TurnState(
            session_id="session-1",
            revision=2,
            intent="ROLE_DISCOVERY",
            phase="AWAITING_SLOT",
            slots={"position": "Руководитель"},
            pending_question=PendingQuestion(topic="city", kind="slot"),
        )
        scenarios = (
            ("pending slot", pending_city_state, "Самара"),
            ("candidate selection", candidate_state, "2"),
            ("show more", candidate_state, "Покажи ещё"),
            ("reset", candidate_state, "Начать заново"),
            ("system change", candidate_state, "Перейти на СберКоманда"),
        )

        for name, state, text in scenarios:
            with self.subTest(name=name):
                planner = RecordingPlanner(error=AssertionError("unexpected GigaChat call"))
                engine = AgentEngine(catalog_cache=make_cache(), planner=planner)

                result = engine.handle(
                    TurnRequest(
                        request_id=f"req-{name}",
                        text=text,
                        state=state,
                    )
                )

                self.assertNotEqual(result.route, RouteSource.GIGACHAT_FALLBACK)
                self.assertIn(result.outcome, (Outcome.HANDLED, Outcome.NEEDS_CLARIFICATION))
                self.assertEqual(result.diagnostics.gigachat_calls, 0)
                self.assertEqual(planner.calls, [])

    def test_weak_candidate_requires_bounded_clarification(self) -> None:
        planner = RecordingPlanner(error=AssertionError("weak match must not call GigaChat"))
        engine = AgentEngine(catalog_cache=make_cache(), planner=planner)

        result = engine.handle(
            TurnRequest(
                request_id="req-weak",
                text="Покажи роли в Друг",
                state=TurnState.empty(session_id="session-weak"),
            )
        )

        self.assertEqual(result.route, RouteSource.DETERMINISTIC)
        self.assertEqual(result.outcome, Outcome.NEEDS_CLARIFICATION)
        self.assertNotIn("system_id", result.state.slots)
        self.assertIsNotNone(result.state.pending_question)
        self.assertEqual(result.state.pending_question.kind, "candidate_selection")
        self.assertGreaterEqual(len(result.state.pending_question.options), 1)
        self.assertLessEqual(len(result.state.pending_question.options), 5)
        self.assertEqual(result.diagnostics.gigachat_calls, 0)
        self.assertEqual(planner.calls, [])

    def test_how_to_get_access_uses_instruction_fast_path_not_role_list(self) -> None:
        planner = RecordingPlanner(error=AssertionError("instruction guardrail must not call GigaChat"))
        engine = AgentEngine(catalog_cache=make_cache(), planner=planner)

        result = engine.handle(
            TurnRequest(
                request_id="req-instruction",
                text="Как получить доступ в АС СберДруг?",
                state=TurnState.empty(session_id="session-instruction"),
            )
        )

        self.assertEqual(result.route, RouteSource.DETERMINISTIC)
        self.assertEqual(result.outcome, Outcome.HANDLED)
        self.assertEqual(result.answer.answer_type, "INSTRUCTION_LOOKUP")
        self.assertEqual(result.state.intent, "INSTRUCTION_LOOKUP")
        self.assertEqual(result.state.slots["system_id"], "sberdrug")
        self.assertEqual(result.diagnostics.gigachat_calls, 0)
        self.assertEqual(planner.calls, [])

    def test_unknown_pending_city_is_not_saved_as_confirmed_context(self) -> None:
        planner = RecordingPlanner(error=AssertionError("invalid slot must not call GigaChat"))
        engine = AgentEngine(catalog_cache=make_cache(), planner=planner)
        state = TurnState(
            session_id="session-invalid-city",
            revision=3,
            intent="ROLE_DISCOVERY",
            phase="AWAITING_SLOT",
            slots={"position": "Руководитель"},
            pending_question=PendingQuestion(topic="city", kind="slot"),
        )

        result = engine.handle(
            TurnRequest(
                request_id="req-invalid-city",
                text="Луна",
                state=state,
            )
        )

        self.assertEqual(result.outcome, Outcome.NEEDS_CLARIFICATION)
        self.assertNotIn("city", result.state.slots)
        self.assertEqual(result.state.pending_question.topic, "city")
        self.assertNotIn("сохранены", result.answer.text.lower())
        self.assertEqual(result.diagnostics.gigachat_calls, 0)

    def test_safe_alias_with_unrelated_question_does_not_return_roles(self) -> None:
        planner = RecordingPlanner(error=AssertionError("known system clarification is deterministic"))
        engine = AgentEngine(catalog_cache=make_cache(), planner=planner)

        result = engine.handle(
            TurnRequest(
                request_id="req-unrelated",
                text="Какая погода рядом со СберДруг?",
                state=TurnState.empty(session_id="session-unrelated"),
            )
        )

        self.assertEqual(result.route, RouteSource.DETERMINISTIC)
        self.assertEqual(result.outcome, Outcome.NEEDS_CLARIFICATION)
        self.assertNotEqual(result.answer.answer_type, "ROLE_DISCOVERY")
        self.assertEqual(result.state.slots["system_id"], "sberdrug")
        self.assertEqual(result.diagnostics.gigachat_calls, 0)

        follow_up = engine.handle(
            TurnRequest(
                request_id="req-unrelated-follow-up",
                text="Роли",
                state=result.state,
            )
        )
        self.assertEqual(follow_up.outcome, Outcome.HANDLED)
        self.assertEqual(follow_up.answer.answer_type, "ROLE_DISCOVERY")
        self.assertEqual(follow_up.diagnostics.gigachat_calls, 0)

    def test_genuinely_ambiguous_turn_calls_gigachat_exactly_once(self) -> None:
        planner = RecordingPlanner(
            response=Plan(
                catalog_version="v42",
                intent="ROLE_DISCOVERY",
                action="SEARCH_ROLES",
                slots={"system_id": "sberdrug"},
                confidence=0.91,
            )
        )
        engine = AgentEngine(
            catalog_cache=make_cache(),
            planner=planner,
            planner_deadline_ms=1200,
        )

        result = engine.handle(
            TurnRequest(
                request_id="req-ambiguous",
                text="Нужен доступ, чтобы согласовывать заявки; что выбрать?",
                state=TurnState.empty(session_id="session-ambiguous"),
            )
        )

        self.assertEqual(result.route, RouteSource.GIGACHAT_FALLBACK)
        self.assertEqual(result.outcome, Outcome.HANDLED)
        self.assertEqual(result.diagnostics.gigachat_calls, 1)
        self.assertEqual(len(planner.calls), 1)
        self.assertEqual(planner.calls[0]["deadline_ms"], 1200)
        self.assertEqual(planner.calls[0]["context"].catalog_version, "v42")


if __name__ == "__main__":
    unittest.main()
