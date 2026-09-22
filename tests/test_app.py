"""Streamlit UI regressions that do not require local AI models."""

from __future__ import annotations

import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StreamlitAppTest(unittest.TestCase):
    def test_all_workspaces_render_without_exceptions(self) -> None:
        app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=20).run()
        self.assertFalse(list(app.exception))
        workspace = next(item for item in app.radio if item.label == "Workspace")
        self.assertEqual(workspace.options, ["Library", "Agent", "Lecture Notes", "Job Queue"])

        workspace.set_value("Agent").run()
        self.assertFalse(list(app.exception))
        self.assertIn("🤖 Local Library Agent", [item.value for item in app.title])

        workspace = next(item for item in app.radio if item.label == "Workspace")
        workspace.set_value("Lecture Notes").run()
        self.assertFalse(list(app.exception))
        self.assertIn("🎓 Queue Lecture Notes", [item.value for item in app.title])

        workspace = next(item for item in app.radio if item.label == "Workspace")
        workspace.set_value("Job Queue").run()
        self.assertFalse(list(app.exception))
        self.assertIn("🗂️ Job Queue", [item.value for item in app.title])


if __name__ == "__main__":
    unittest.main()
