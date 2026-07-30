from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.api import create_app


class StubMessageService:
    def __init__(self) -> None:
        self.post_calls: list[dict[str, object]] = []
        self.get_calls = 0

    def post_message(
        self,
        *,
        session_id: str,
        request_id: str,
        text: str,
        state_revision: int,
    ) -> dict[str, object]:
        self.post_calls.append(
            {
                "session_id": session_id,
                "request_id": request_id,
                "text": text,
                "state_revision": state_revision,
            }
        )
        return {
            "request_id": request_id,
            "session_id": session_id,
            "assistant": {
                "text": "Для АС СберДруг найдены две роли.",
                "answer_type": "ROLE_DISCOVERY",
                "facts": {"system_id": "sberdrug", "role_ids": ["reader", "approver"]},
            },
            "state": {
                "revision": state_revision + 1,
                "intent": "ROLE_DISCOVERY",
                "phase": "ANSWERED",
                "slots": {"system_id": "sberdrug"},
                "pending_question": None,
            },
            "diagnostics": {
                "trace_id": "trace-api-1",
                "route": "DETERMINISTIC",
                "outcome": "HANDLED",
                "catalog_version": "v42",
                "cache_hit": True,
                "gigachat_calls": 0,
                "durations_ms": {
                    "total": 8.0,
                    "state_read": 1.0,
                    "retrieval": 3.0,
                    "agent": 2.0,
                    "gigachat": 0.0,
                    "state_write": 2.0,
                },
            },
        }

    def get_session(self, session_id: str) -> object:
        self.get_calls += 1
        raise AssertionError("message POST must not reload the session")


class MessageApiContractTests(unittest.TestCase):
    def test_post_returns_updated_state_answer_and_diagnostics_without_follow_up_get(self) -> None:
        service = StubMessageService()
        client = TestClient(create_app(message_service=service))

        response = client.post(
            "/api/v2/sessions/session-api/messages",
            json={
                "request_id": "req-api-1",
                "text": "Покажи роли в СберДруг",
                "state_revision": 7,
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["request_id"], "req-api-1")
        self.assertEqual(payload["session_id"], "session-api")
        self.assertEqual(payload["state"]["revision"], 8)
        self.assertEqual(payload["state"]["slots"]["system_id"], "sberdrug")
        self.assertEqual(payload["assistant"]["answer_type"], "ROLE_DISCOVERY")
        self.assertTrue(payload["assistant"]["text"])
        diagnostics = payload["diagnostics"]
        self.assertEqual(diagnostics["trace_id"], "trace-api-1")
        self.assertEqual(diagnostics["route"], "DETERMINISTIC")
        self.assertEqual(diagnostics["outcome"], "HANDLED")
        self.assertEqual(diagnostics["catalog_version"], "v42")
        self.assertTrue(diagnostics["cache_hit"])
        self.assertEqual(diagnostics["gigachat_calls"], 0)
        self.assertIn("total", diagnostics["durations_ms"])
        self.assertEqual(len(service.post_calls), 1)
        self.assertEqual(service.get_calls, 0)


if __name__ == "__main__":
    unittest.main()
