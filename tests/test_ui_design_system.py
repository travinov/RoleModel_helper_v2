from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class UiDesignSystemContractTests(unittest.TestCase):
    def test_v2_uses_v1_glass_components_and_agentic_palette(self) -> None:
        page = (ROOT / "app" / "static" / "index.html").read_text(
            encoding="utf-8"
        )

        for token in (
            "--font-ui:",
            "--glass-fill:",
            "--radius-shell:",
            "--radius-panel:",
            "--radius-card:",
            "--blur:",
            "--agent-green:",
            "--agent-violet:",
            "--agent-pink:",
        ):
            self.assertIn(token, page)
        for component in (
            'class="viewport"',
            'class="phone"',
            'class="app-shell"',
            'class="topbar',
            'class="context-tray',
            'id="messages"',
            'id="composer"',
        ):
            self.assertIn(component, page)

    def test_ui_preserves_api_contract_accessibility_and_safe_text_rendering(self) -> None:
        page = (ROOT / "app" / "static" / "index.html").read_text(
            encoding="utf-8"
        )

        for contract in (
            "/api/v2/health",
            "/api/v2/sessions",
            "request_id",
            "state_revision",
            "crypto.randomUUID()",
        ):
            self.assertIn(contract, page)
        self.assertIn(":focus-visible", page)
        self.assertIn("prefers-reduced-motion", page)
        self.assertIn("@media (max-width: 720px)", page)
        self.assertIn("bubbleText.textContent = text", page)
        self.assertNotIn("bubble.innerHTML", page)

    def test_ui_copy_is_concise_and_non_promotional(self) -> None:
        page = (ROOT / "app" / "static" / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("Поиск доступов и ролей", page)
        self.assertIn(
            "Укажите подразделение, должность или автоматизированную систему.",
            page,
        )
        for slogan in (
            "Не заставляет ждать",
            "Мгновенный поиск",
            "Агентская логика",
            "Без догадок",
            "Agentic access navigator",
        ):
            self.assertNotIn(slogan, page)


if __name__ == "__main__":
    unittest.main()
