"""Tests for the library file-manager presentation helpers."""

from __future__ import annotations

from pathlib import Path
import unittest

from src.ui.file_manager_component import file_icon


class FileManagerIconTest(unittest.TestCase):
    def test_common_lecture_files_have_distinct_icons(self) -> None:
        icons = {
            file_icon("recording.m4a"),
            file_icon("slides.pdf"),
            file_icon("notes.md"),
            file_icon("deck.pptx"),
        }

        self.assertEqual(len(icons), 4)
        self.assertEqual(file_icon("recording.m4a"), "🎧")
        self.assertEqual(file_icon("slides.pdf"), "📕")
        self.assertEqual(file_icon("notes.md"), "📝")
        self.assertEqual(file_icon("deck.pptx"), "📊")

    def test_folder_actions_use_a_compact_three_dot_menu(self) -> None:
        component = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "ui"
            / "components"
            / "file_manager"
            / "index.html"
        ).read_text(encoding="utf-8")

        self.assertNotIn("Click to preview · Drag to move", component)
        self.assertIn('"⋯"', component)
        self.assertIn('event("rename_folder"', component)
        self.assertIn('event("delete_folder"', component)


if __name__ == "__main__":
    unittest.main()
