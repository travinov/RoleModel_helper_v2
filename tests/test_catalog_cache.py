from __future__ import annotations

import concurrent.futures
import threading
import unittest

from app.catalog.cache import CatalogBundle, CatalogCache, Freshness

from tests.fakes import VersionedCatalogSource, load_catalog_mapping


def make_source() -> VersionedCatalogSource:
    return VersionedCatalogSource(
        {
            "v42": load_catalog_mapping("catalog_v42.json"),
            "v43": load_catalog_mapping("catalog_v43.json"),
        },
        bundle_factory=CatalogBundle.from_mapping,
    )


class CatalogCacheTests(unittest.TestCase):
    def test_version_is_loaded_once_reused_and_exposed_as_immutable(self) -> None:
        source = make_source()
        cache = CatalogCache(source)

        first_refresh = cache.refresh("v42")
        first = cache.get()
        second_refresh = cache.refresh("v42")
        second = cache.get()

        self.assertTrue(first_refresh.activated)
        self.assertTrue(second_refresh.cache_hit)
        self.assertIs(first, second)
        self.assertEqual(source.load_calls, ["v42"])
        self.assertEqual(first.version, "v42")
        with self.assertRaises(TypeError):
            first.systems["unexpected"] = object()

    def test_successful_refresh_activates_atomically_without_mutating_pinned_turn(self) -> None:
        source = make_source()
        cache = CatalogCache(source)
        cache.refresh("v42")
        pinned_v42 = cache.get()

        result = cache.refresh("v43")
        active_v43 = cache.get()

        self.assertTrue(result.activated)
        self.assertEqual(active_v43.version, "v43")
        self.assertEqual(pinned_v42.version, "v42")
        self.assertIn("role-approver-v42", pinned_v42.role_ids)
        self.assertNotIn("role-approver-v42", active_v43.role_ids)
        self.assertIn("role-auditor-v43", active_v43.role_ids)

    def test_failed_refresh_preserves_last_good_and_reports_degraded_freshness(self) -> None:
        source = make_source()
        cache = CatalogCache(source)
        cache.refresh("v42")
        last_good = cache.get()
        source.fail_versions.add("v43")

        result = cache.refresh("v43")

        self.assertFalse(result.activated)
        self.assertIs(cache.get(), last_good)
        self.assertEqual(cache.get().version, "v42")
        self.assertEqual(cache.status.freshness, Freshness.DEGRADED)
        self.assertIn("v43", cache.status.last_error)

    def test_concurrent_refresh_for_one_version_is_single_flight(self) -> None:
        source = make_source()
        cache = CatalogCache(source)
        cache.refresh("v42")
        source.block_version = "v43"
        workers = 4
        start_together = threading.Barrier(workers + 1)

        def refresh() -> object:
            start_together.wait()
            return cache.refresh("v43")

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(refresh) for _ in range(workers)]
            start_together.wait()
            self.assertTrue(source.load_started.wait(timeout=1))
            source.release_load.set()
            results = [future.result(timeout=2) for future in futures]

        self.assertEqual(source.load_calls.count("v43"), 1)
        self.assertTrue(all(result.version == "v43" for result in results))
        self.assertIs(cache.get(), cache.get())

    def test_active_pointer_refresh_switches_versions_between_pinned_turns(self) -> None:
        source = make_source()
        cache = CatalogCache(source)
        cache.refresh("v42")
        pinned = cache.get()
        source.active_version_value = "v43"

        result = cache.refresh_active()

        self.assertTrue(result.activated)
        self.assertEqual(cache.get().version, "v43")
        self.assertEqual(pinned.version, "v42")


if __name__ == "__main__":
    unittest.main()
