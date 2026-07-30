from __future__ import annotations

import unittest

from app.catalog.cache import CatalogBundle
from app.catalog.normalization import normalize_query, parse_department_number
from app.tools.catalog import CatalogSearchTools, ToolStatus
from tests.fakes import load_catalog_mapping


class CatalogNormalizationTests(unittest.TestCase):
    def test_department_number_variants_are_normalized_deterministically(self) -> None:
        cases = {
            "Отдел 2": 2,
            "отдел №2": 2,
            "номер 2": 2,
            "второй отдел": 2,
            "2-й отдел": 2,
            "Отдел кредитования номер 20": 20,
        }

        for query, expected in cases.items():
            with self.subTest(query=query):
                normalized = normalize_query(query)
                self.assertEqual(parse_department_number(normalized), expected)


class CatalogSearchToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = CatalogBundle.from_mapping(
            load_catalog_mapping("catalog_v44_org.json")
        )
        self.tools = CatalogSearchTools(self.bundle)

    def test_department_number_is_a_hard_filter_and_city_disambiguates(self) -> None:
        ambiguous = self.tools.search_departments("Отдел 2")

        self.assertEqual(ambiguous.status, ToolStatus.AMBIGUOUS)
        self.assertEqual(
            {candidate.id for candidate in ambiguous.candidates},
            {
                "department-samara-credit-2",
                "department-moscow-credit-2",
            },
        )
        self.assertNotIn(
            "department-samara-credit-20",
            {candidate.id for candidate in ambiguous.candidates},
        )

        resolved = self.tools.search_departments("второй отдел", city="Самара")

        self.assertEqual(resolved.status, ToolStatus.FOUND)
        self.assertEqual(len(resolved.candidates), 1)
        self.assertEqual(
            resolved.candidates[0].id,
            "department-samara-credit-2",
        )
        self.assertIn("exact_number", resolved.candidates[0].matched_by)

    def test_position_profile_system_role_and_instruction_are_relation_scoped(self) -> None:
        positions = self.tools.search_positions(
            "начальник",
            city="Самара",
            department_id="department-samara-credit-2",
        )
        self.assertEqual(positions.status, ToolStatus.FOUND)
        self.assertEqual(positions.candidates[0].id, "position-head")

        profiles = self.tools.resolve_profiles(
            city="Самара",
            department_id="department-samara-credit-2",
            position_id="position-head",
        )
        self.assertEqual(profiles.status, ToolStatus.FOUND)
        self.assertEqual(profiles.candidates[0].id, "profile-samara-head")

        systems = self.tools.search_systems(profile_ids=["profile-samara-head"])
        self.assertEqual(systems.status, ToolStatus.FOUND)
        self.assertEqual(
            [candidate.id for candidate in systems.candidates],
            ["access-flow"],
        )

        roles = self.tools.search_roles(
            system_id="access-flow",
            profile_ids=["profile-samara-head"],
            query="хочу согласовывать заявки",
        )
        self.assertEqual(roles.status, ToolStatus.FOUND)
        self.assertEqual(
            [candidate.id for candidate in roles.candidates],
            ["access-approver"],
        )

        instruction = self.tools.get_access_instruction(system_id="access-flow")
        self.assertEqual(instruction.status, ToolStatus.FOUND)
        self.assertEqual(
            instruction.candidates[0].id,
            "instruction-access-flow",
        )

    def test_tool_results_are_bounded(self) -> None:
        result = self.tools.search_departments("отдел", limit=1)

        self.assertLessEqual(len(result.candidates), 1)
        self.assertEqual(result.catalog_version, "v44-org")


if __name__ == "__main__":
    unittest.main()
