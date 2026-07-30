from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path

from app.agent.models import Plan
from app.agent.service import AgentEngine
from app.catalog.cache import CatalogBundle, CatalogCache
from app.runtime.service import RuntimeService, StateRevisionConflict

from tests.fakes import RecordingPlanner, VersionedCatalogSource, load_catalog_mapping


class RuntimeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        source = VersionedCatalogSource(
            {"v42": load_catalog_mapping("catalog_v42.json")},
            bundle_factory=CatalogBundle.from_mapping,
        )
        cache = CatalogCache(source)
        cache.refresh("v42")
        engine = AgentEngine(
            catalog_cache=cache,
            planner=RecordingPlanner(error=AssertionError("fast path called GigaChat")),
        )
        self.runtime = RuntimeService(
            database_path=Path(self.temp_dir.name) / "state.sqlite3",
            engine=engine,
        )
        self.runtime.initialize()

    def test_create_session_and_post_message_return_complete_updated_payload(self) -> None:
        created = self.runtime.create_session(session_id="session-runtime")

        self.assertEqual(created["session_id"], "session-runtime")
        self.assertEqual(created["state"]["revision"], 0)
        self.assertEqual(created["messages"], [])

        response = self.runtime.post_message(
            session_id="session-runtime",
            request_id="req-runtime-1",
            text="Покажи роли в АС СберДруг",
            state_revision=0,
        )

        self.assertEqual(response["request_id"], "req-runtime-1")
        self.assertEqual(response["session_id"], "session-runtime")
        self.assertEqual(response["state"]["revision"], 1)
        self.assertEqual(response["state"]["slots"]["system_id"], "sberdrug")
        self.assertEqual(response["assistant"]["answer_type"], "ROLE_DISCOVERY")
        self.assertTrue(response["assistant"]["text"])
        self.assertEqual(response["diagnostics"]["catalog_version"], "v42")
        self.assertEqual(response["diagnostics"]["route"], "DETERMINISTIC")
        self.assertEqual(response["diagnostics"]["gigachat_calls"], 0)
        self.assertIn("total", response["diagnostics"]["durations_ms"])

        persisted = self.runtime.get_session("session-runtime")
        self.assertEqual(persisted["state"], response["state"])
        self.assertEqual(len(persisted["messages"]), 1)
        self.assertEqual(persisted["messages"][0]["request_id"], "req-runtime-1")

    def test_request_id_is_idempotent_and_does_not_duplicate_message(self) -> None:
        self.runtime.create_session(session_id="session-idempotent")
        kwargs = {
            "session_id": "session-idempotent",
            "request_id": "req-same",
            "text": "Покажи роли в АС СберДруг",
            "state_revision": 0,
        }

        first = self.runtime.post_message(**kwargs)
        replay = self.runtime.post_message(**kwargs)

        self.assertEqual(replay, first)
        persisted = self.runtime.get_session("session-idempotent")
        self.assertEqual(persisted["state"]["revision"], 1)
        self.assertEqual(len(persisted["messages"]), 1)

    def test_stale_state_revision_conflicts_without_writing_duplicate(self) -> None:
        self.runtime.create_session(session_id="session-conflict")
        first = self.runtime.post_message(
            session_id="session-conflict",
            request_id="req-first",
            text="Покажи роли в АС СберДруг",
            state_revision=0,
        )

        with self.assertRaises(StateRevisionConflict):
            self.runtime.post_message(
                session_id="session-conflict",
                request_id="req-stale",
                text="Перейти на СберКоманда",
                state_revision=0,
            )

        persisted = self.runtime.get_session("session-conflict")
        self.assertEqual(persisted["state"], first["state"])
        self.assertEqual(persisted["state"]["revision"], 1)
        self.assertEqual(len(persisted["messages"]), 1)
        self.assertEqual(persisted["messages"][0]["request_id"], "req-first")

    def test_slow_fallback_does_not_block_fast_path_in_another_session(self) -> None:
        class BlockingPlanner:
            def __init__(self) -> None:
                self.started = threading.Event()
                self.release = threading.Event()

            def plan(self, request, context, *, deadline_ms: int):
                self.started.set()
                if not self.release.wait(timeout=2):
                    raise TimeoutError("test did not release planner")
                return Plan(
                    catalog_version=context.version,
                    intent="ROLE_DISCOVERY",
                    action="SEARCH_ROLES",
                    slots={"system_id": "sberdrug"},
                    confidence=0.9,
                )

        planner = BlockingPlanner()
        source = VersionedCatalogSource(
            {"v42": load_catalog_mapping("catalog_v42.json")},
            bundle_factory=CatalogBundle.from_mapping,
        )
        cache = CatalogCache(source)
        cache.refresh("v42")
        runtime = RuntimeService(
            database_path=Path(self.temp_dir.name) / "concurrency.sqlite3",
            engine=AgentEngine(catalog_cache=cache, planner=planner),
        )
        runtime.initialize()
        runtime.create_session(session_id="slow-session")
        runtime.create_session(session_id="fast-session")

        with ThreadPoolExecutor(max_workers=2) as executor:
            slow = executor.submit(
                runtime.post_message,
                session_id="slow-session",
                request_id="slow-request",
                text="Нужен доступ для согласования; что выбрать?",
                state_revision=0,
            )
            self.assertTrue(planner.started.wait(timeout=1))
            fast = executor.submit(
                runtime.post_message,
                session_id="fast-session",
                request_id="fast-request",
                text="Покажи роли в АС СберДруг",
                state_revision=0,
            )
            fast_completed = True
            try:
                fast_result = fast.result(timeout=0.25)
            except FutureTimeout:
                fast_completed = False
                fast_result = None
            finally:
                planner.release.set()
            slow.result(timeout=2)

        self.assertTrue(fast_completed, "a slow fallback serialized another session")
        self.assertEqual(fast_result["diagnostics"]["route"], "DETERMINISTIC")


if __name__ == "__main__":
    unittest.main()
