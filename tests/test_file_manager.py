"""Tests for the library file-manager presentation helpers."""

from __future__ import annotations

import base64
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.library import LibraryStore
from src.ui.file_manager_component import file_icon
from src.ui.library_page import _handle_file_manager_event


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
        self.assertIn('event("create_folder"', component)
        self.assertIn('event("rename_folder"', component)
        self.assertIn('event("delete_folder"', component)

    def test_file_manager_accepts_direct_file_drops(self) -> None:
        component = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "ui"
            / "components"
            / "file_manager"
            / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn('event("upload"', component)
        self.assertIn("eventObject.dataTransfer.files", component)

    def test_direct_drop_event_stores_file_in_target_folder(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            library = LibraryStore(Path(temporary_directory))
            subject = library.create_subject("Physics")
            folder = library.create_folder(subject.id, "Week 1")
            fake_streamlit = SimpleNamespace(session_state={}, error=Mock(), rerun=Mock())
            event = {
                "event_id": "direct-drop-1",
                "action": "upload",
                "folder_id": folder.id,
                "files": [
                    {
                        "name": "slides.pdf",
                        "data": base64.b64encode(b"lecture slides").decode("ascii"),
                    }
                ],
            }

            with patch("src.ui.library_page.st", fake_streamlit):
                _handle_file_manager_event(
                    library,
                    subject.id,
                    [folder],
                    [],
                    "browse_key",
                    "selected_key",
                    event,
                )

            documents = library.list_documents(subject.id)
            self.assertEqual(len(documents), 1)
            self.assertEqual(documents[0].original_name, "slides.pdf")
            self.assertEqual(documents[0].folder_id, folder.id)
            self.assertEqual(fake_streamlit.session_state["library_file_notice"], "Added 1 file to Week 1.")
            fake_streamlit.rerun.assert_called_once()


if __name__ == "__main__":
    unittest.main()
