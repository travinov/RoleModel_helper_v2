from __future__ import annotations

import unittest

from app.agent.models import Outcome, RouteSource, TurnRequest, TurnState
from app.agent.service import AgentEngine
from app.catalog.cache import CatalogBundle, CatalogCache
from tests.fakes import RecordingPlanner, VersionedCatalogSource, load_catalog_mapping


def make_engine() -> tuple[AgentEngine, RecordingPlanner]:
    source = VersionedCatalogSource(
        {"v44-org": load_catalog_mapping("catalog_v44_org.json")},
        bundle_factory=CatalogBundle.from_mapping,
    )
    cache = CatalogCache(source)
    cache.refresh("v44-org")
    planner = RecordingPlanner(
        error=AssertionError("organization fast path must not call GigaChat")
    )
    return AgentEngine(catalog_cache=cache, planner=planner), planner


class OrganizationDialogueTests(unittest.TestCase):
    def test_department_number_without_city_returns_only_exact_number_candidates(self) -> None:
        engine, planner = make_engine()

        result = engine.handle(
            TurnRequest(
                request_id="org-ambiguous-number",
                text="Какие роли у руководителя отдела 2?",
                state=TurnState.empty("session-org"),
            )
        )

        self.assertEqual(result.route, RouteSource.DETERMINISTIC)
        self.assertEqual(result.outcome, Outcome.NEEDS_CLARIFICATION)
        self.assertEqual(result.state.pending_question.topic, "department")
        candidate_ids = {
            item.id for item in result.state.pending_question.options
        }
        self.assertEqual(
            candidate_ids,
            {
                "department-samara-credit-2",
                "department-moscow-credit-2",
            },
        )
        self.assertNotIn("department-samara-credit-20", candidate_ids)
        self.assertEqual(result.state.slots["position_id"], "position-head")
        self.assertEqual(planner.calls, [])

    def test_city_department_and_position_resolve_profile_access(self) -> None:
        engine, planner = make_engine()

        result = engine.handle(
            TurnRequest(
                request_id="org-complete",
                text="Какие роли у руководителя отдела 2 в Самаре?",
                state=TurnState.empty("session-org-complete"),
            )
        )

        self.assertEqual(result.outcome, Outcome.HANDLED)
        self.assertEqual(result.route, RouteSource.DETERMINISTIC)
        self.assertEqual(result.answer.answer_type, "PROFILE_ACCESS")
        self.assertEqual(
            result.answer.facts["department_id"],
            "department-samara-credit-2",
        )
        self.assertEqual(result.answer.facts["position_id"], "position-head")
        self.assertEqual(
            result.answer.facts["profile_ids"],
            ["profile-samara-head"],
        )
        self.assertEqual(result.answer.facts["system_ids"], ["access-flow"])
        self.assertEqual(result.answer.facts["role_ids"], ["access-approver"])
        self.assertNotIn("risk-viewer", result.answer.facts["role_ids"])
        self.assertEqual(result.diagnostics.gigachat_calls, 0)
        self.assertEqual(planner.calls, [])

    def test_missing_position_asks_one_bounded_follow_up_then_resolves(self) -> None:
        engine, planner = make_engine()
        first = engine.handle(
            TurnRequest(
                request_id="org-needs-position",
                text="Какие доступы у отдела 2 в Самаре?",
                state=TurnState.empty("session-org-follow-up"),
            )
        )

        self.assertEqual(first.outcome, Outcome.NEEDS_CLARIFICATION)
        self.assertEqual(first.state.pending_question.topic, "position")
        self.assertEqual(
            first.state.slots["department_id"],
            "department-samara-credit-2",
        )

        second = engine.handle(
            TurnRequest(
                request_id="org-position-answer",
                text="Начальник",
                state=first.state,
            )
        )

        self.assertEqual(second.outcome, Outcome.HANDLED)
        self.assertEqual(second.answer.answer_type, "PROFILE_ACCESS")
        self.assertEqual(second.state.slots["position_id"], "position-head")
        self.assertEqual(second.answer.facts["role_ids"], ["access-approver"])
        self.assertEqual(planner.calls, [])

    def test_selecting_ambiguous_department_continues_existing_position_context(self) -> None:
        engine, planner = make_engine()
        first = engine.handle(
            TurnRequest(
                request_id="org-select-first",
                text="Какие роли у руководителя отдела 2?",
                state=TurnState.empty("session-org-select"),
            )
        )
        labels = [item.label for item in first.state.pending_question.options]
        samara_index = next(
            index
            for index, label in enumerate(labels, start=1)
            if "Самара" in label
        )

        second = engine.handle(
            TurnRequest(
                request_id="org-select-second",
                text=str(samara_index),
                state=first.state,
            )
        )

        self.assertEqual(second.outcome, Outcome.HANDLED)
        self.assertEqual(second.answer.answer_type, "PROFILE_ACCESS")
        self.assertEqual(
            second.state.slots["department_id"],
            "department-samara-credit-2",
        )
        self.assertEqual(second.answer.facts["role_ids"], ["access-approver"])
        self.assertEqual(planner.calls, [])


if __name__ == "__main__":
    unittest.main()
