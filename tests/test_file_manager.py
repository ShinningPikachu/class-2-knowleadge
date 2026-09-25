"""Tests for the library file-manager presentation helpers."""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
