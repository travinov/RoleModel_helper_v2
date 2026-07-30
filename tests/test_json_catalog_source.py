from __future__ import annotations

import unittest

from app.catalog.json_source import CatalogVersionMismatch, JsonCatalogSource

from tests.fakes import FIXTURES


class JsonCatalogSourceTests(unittest.TestCase):
    def test_loads_demo_json_as_versioned_immutable_bundle(self) -> None:
        source = JsonCatalogSource(FIXTURES / "catalog_v42.json")

        bundle = source.load("v42")

        self.assertEqual(bundle.version, "v42")
        self.assertEqual(bundle.systems["sberdrug"]["name"], "СберДруг")
        self.assertIn("role-reader-v42", bundle.role_ids)
        with self.assertRaises(TypeError):
            bundle.systems["sberdrug"]["name"] = "Подмена"
        with self.assertRaises(TypeError):
            bundle.systems["new-system"] = {}

    def test_rejects_requested_version_that_does_not_match_json(self) -> None:
        source = JsonCatalogSource(FIXTURES / "catalog_v42.json")

        with self.assertRaises(CatalogVersionMismatch):
            source.load("v43")


if __name__ == "__main__":
    unittest.main()
