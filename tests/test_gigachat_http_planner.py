from __future__ import annotations

import json
import unittest
from typing import Any

from app.agent.models import Plan, TurnRequest, TurnState
from app.catalog.cache import CatalogBundle
from app.providers.gigachat import (
    GigaChatHttpPlanner,
    PlannerResponseError,
    _expiration_epoch,
)

from tests.fakes import FakeClock, load_catalog_mapping


class FakeTransport:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_ms: int,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout_ms": timeout_ms,
            }
        )
        return self.response


def request() -> TurnRequest:
    return TurnRequest(
        request_id="req-http-planner",
        text="Нужен доступ для согласования заявок",
        state=TurnState.empty(session_id="session-http-planner"),
    )


def bundle() -> CatalogBundle:
    return CatalogBundle.from_mapping(load_catalog_mapping("catalog_v42.json"))


class GigaChatHttpPlannerTests(unittest.TestCase):
    def test_oauth_expiration_accepts_seconds_and_milliseconds(self) -> None:
        self.assertEqual(_expiration_epoch(1_800_000_000), 1_800_000_000.0)
        self.assertEqual(_expiration_epoch(1_800_000_000_000), 1_800_000_000.0)

    def test_parses_structured_json_and_passes_pinned_version_and_deadline(self) -> None:
        structured = {
            "catalog_version": "v42",
            "intent": "ROLE_DISCOVERY",
            "action": "SEARCH_ROLES",
            "slots": {"system_id": "sberdrug"},
            "confidence": 0.92,
        }
        transport = FakeTransport(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(structured, ensure_ascii=False),
                        }
                    }
                ]
            }
        )
        token = "test-token-must-not-enter-prompt"
        planner = GigaChatHttpPlanner(
            endpoint="https://gigachat.example/v1/chat/completions",
            access_token=token,
            transport=transport,
        )

        plan = planner.plan(request(), bundle(), deadline_ms=750)

        self.assertIsInstance(plan, Plan)
        self.assertEqual(plan.catalog_version, "v42")
        self.assertEqual(plan.intent, "ROLE_DISCOVERY")
        self.assertEqual(plan.action, "SEARCH_ROLES")
        self.assertEqual(plan.slots["system_id"], "sberdrug")
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["timeout_ms"], 750)
        self.assertEqual(call["payload"]["catalog_version"], "v42")
        self.assertEqual(call["payload"]["request_id"], "req-http-planner")
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {token}")
        self.assertNotIn(token, json.dumps(call["payload"], ensure_ascii=False))
        user_payload = json.loads(call["payload"]["messages"][1]["content"])
        self.assertNotIn("systems", user_payload)
        self.assertIn("catalog_candidates", user_payload)
        candidates = user_payload["catalog_candidates"]
        self.assertLessEqual(len(candidates["systems"]), 5)
        self.assertLessEqual(len(candidates["departments"]), 5)
        self.assertLessEqual(len(candidates["positions"]), 5)
        self.assertEqual(
            [item["id"] for item in candidates["systems"]],
            ["sberdrug"],
        )
        self.assertNotIn(
            "СберКоманда",
            json.dumps(user_payload, ensure_ascii=False),
        )

    def test_malformed_provider_content_is_rejected(self) -> None:
        transport = FakeTransport(
            {
                "choices": [
                    {
                        "message": {
                            "content": "это не structured JSON",
                        }
                    }
                ]
            }
        )
        planner = GigaChatHttpPlanner(
            endpoint="https://gigachat.example/v1/chat/completions",
            access_token="test-token",
            transport=transport,
        )

        with self.assertRaises(PlannerResponseError):
            planner.plan(request(), bundle(), deadline_ms=500)

    def test_oauth_time_is_subtracted_from_end_to_end_deadline(self) -> None:
        clock = FakeClock()

        class SlowTokenProvider:
            def bearer_token(self, *, deadline_ms: int) -> str:
                clock.advance_ms(800)
                return "token"

        transport = FakeTransport({"choices": []})
        planner = GigaChatHttpPlanner(
            endpoint="https://gigachat.example/v1/chat/completions",
            token_provider=SlowTokenProvider(),
            transport=transport,
            clock=clock,
        )

        with self.assertRaises(TimeoutError):
            planner.plan(request(), bundle(), deadline_ms=750)
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
