"""Guardrails for the bundled Hermes routing skill."""

from __future__ import annotations

import unittest
from pathlib import Path


SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / "clawlink.md"


class SkillContractTests(unittest.TestCase):
    def test_skill_uses_current_mcp_tool_names(self) -> None:
        text = SKILL_PATH.read_text()

        for name in [
            "clawlink.list_integrations",
            "clawlink.search",
            "clawlink.get_connection",
            "clawlink.list_actions",
            "clawlink.get_action",
            "clawlink.connect_app",
            "clawlink.execute",
        ]:
            self.assertIn(name, text)

    def test_skill_does_not_reference_legacy_openclaw_tool_names(self) -> None:
        text = SKILL_PATH.read_text()

        for name in [
            "clawlink_list_integrations",
            "clawlink_list_tools",
            "clawlink_search_tools",
            "clawlink_describe_tool",
            "clawlink_preview_tool",
            "clawlink_call_tool",
        ]:
            self.assertNotIn(name, text)


if __name__ == "__main__":
    unittest.main()
