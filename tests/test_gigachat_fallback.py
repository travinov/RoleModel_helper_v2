from __future__ import annotations

import unittest

from app.agent.models import (
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
    source = VersionedCatalogSource(
        {"v42": load_catalog_mapping("catalog_v42.json")},
        bundle_factory=CatalogBundle.from_mapping,
    )
    cache = CatalogCache(source)
    cache.refresh("v42")
    return cache


def preserved_state() -> TurnState:
    return TurnState(
        session_id="session-preserve",
        revision=9,
        intent="ROLE_DISCOVERY",
        phase="AWAITING_SLOT",
        slots={"city": "Самара", "position": "Руководитель"},
        pending_question=PendingQuestion(topic="department", kind="slot"),
    )


class GigaChatFallbackTests(unittest.TestCase):
    def assertRejectedWithoutStateMutation(
        self,
        planner: RecordingPlanner,
        *,
        expected_outcome: Outcome,
        expected_code: str,
    ) -> None:
        before = preserved_state()
        engine = AgentEngine(
            catalog_cache=make_cache(),
            planner=planner,
            planner_deadline_ms=500,
        )

        result = engine.handle(
            TurnRequest(
                request_id=f"req-{expected_code}",
                text="Сложный неоднозначный вопрос про доступ",
                state=before,
            )
        )

        self.assertEqual(result.route, RouteSource.GIGACHAT_FALLBACK)
        self.assertEqual(result.outcome, expected_outcome)
        self.assertEqual(result.state, before)
        self.assertEqual(result.failure.code, expected_code)
        self.assertTrue(result.failure.user_message)
        self.assertEqual(result.diagnostics.gigachat_calls, 1)
        self.assertEqual(len(planner.calls), 1)

    def test_malformed_plan_preserves_pre_turn_state(self) -> None:
        self.assertRejectedWithoutStateMutation(
            RecordingPlanner(response={"intent": "ROLE_DISCOVERY"}),
            expected_outcome=Outcome.NEEDS_CLARIFICATION,
            expected_code="MALFORMED_PLAN",
        )

    def test_stale_catalog_plan_preserves_pre_turn_state(self) -> None:
        self.assertRejectedWithoutStateMutation(
            RecordingPlanner(
                response=Plan(
                    catalog_version="v41",
                    intent="ROLE_DISCOVERY",
                    action="SEARCH_ROLES",
                    slots={"system_id": "sberdrug"},
                    confidence=0.95,
                )
            ),
            expected_outcome=Outcome.NEEDS_CLARIFICATION,
            expected_code="STALE_PLAN",
        )

    def test_hallucinated_catalog_id_preserves_pre_turn_state(self) -> None:
        self.assertRejectedWithoutStateMutation(
            RecordingPlanner(
                response=Plan(
                    catalog_version="v42",
                    intent="ROLE_DISCOVERY",
                    action="SEARCH_ROLES",
                    slots={"system_id": "invented-by-model"},
                    confidence=0.99,
                )
            ),
            expected_outcome=Outcome.NEEDS_CLARIFICATION,
            expected_code="UNKNOWN_CATALOG_FACT",
        )

    def test_invalid_intent_or_action_is_rejected_as_malformed(self) -> None:
        self.assertRejectedWithoutStateMutation(
            RecordingPlanner(
                response=Plan(
                    catalog_version="v42",
                    intent="DELETE_ALL",
                    action="DROP_CATALOG",
                    slots={"system_id": "sberdrug"},
                    confidence=1.0,
                )
            ),
            expected_outcome=Outcome.NEEDS_CLARIFICATION,
            expected_code="MALFORMED_PLAN",
        )

    def test_timeout_preserves_pre_turn_state_and_returns_retryable_failure(self) -> None:
        planner = RecordingPlanner(error=TimeoutError("provider deadline exceeded"))
        self.assertRejectedWithoutStateMutation(
            planner,
            expected_outcome=Outcome.PROVIDER_FAILURE,
            expected_code="PROVIDER_TIMEOUT",
        )


if __name__ == "__main__":
    unittest.main()
