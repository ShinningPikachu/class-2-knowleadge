"""Dry-run planning and atomic catalog update for lecture reprocessing."""

import json
import tempfile
import unittest
from pathlib import Path

from scripts.rebuild_legacy_lectures import candidates
from src.library import DuplicateDocumentError, LibraryStore


class LegacyRebuildTests(unittest.TestCase):
    def test_rebuild_plan_finds_only_legacy_recorded_lectures(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            library = LibraryStore(Path(root) / "library")
            subject = library.create_subject("NLP")
            folder = library.create_folder(subject.id, "Week 1")
            library.add_document_bytes(subject.id, "Slides.pdf", b"fake pdf", folder.id)
            library.add_document_bytes(subject.id, "Recording.m4a", b"fake recording", folder.id)
            derived = library.subjects_root / subject.id / "derived" / folder.id
            derived.mkdir(parents=True)
            alignment = derived / "alignment.json"
            summaries = derived / "slide_summaries.json"
            alignment.write_text(json.dumps({"metadata": {"alignment_version": 1}, "slides": []}))
            summaries.write_text(json.dumps({"slides": [{"slide": 1, "summary": "A note."}]}))
            planned = candidates(library)
            self.assertEqual(len(planned), 1)
            self.assertEqual(planned[0].folder_id, folder.id)
            self.assertEqual(len(planned[0].reasons), 2)
            alignment.write_text(json.dumps({"metadata": {"alignment_version": 2}, "slides": []}))
            summaries.write_text(json.dumps({"slides": [
                {"slide": 1, "summary": "A note.", "sources": {"slide": True, "teacher_audio": True}}
            ]}))
            self.assertEqual(candidates(library), [])

    def test_generated_document_replacement_keeps_id_and_reindexes(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            library = LibraryStore(Path(root) / "library")
            subject = library.create_subject("Subject")
            document = library.add_document_bytes(subject.id, "Notes.txt", b"Old queue explanation")
            replacement = Path(root) / "replacement.txt"
            replacement.write_text("New transformer explanation")
            updated = library.replace_document_contents(document.id, replacement)
            self.assertEqual(updated.id, document.id)
            self.assertEqual(updated.stored_path.read_text(), "New transformer explanation")
            self.assertEqual(library.search_documents("Old queue", subject.id), [])
            self.assertEqual(library.search_documents("transformer", subject.id)[0].document_id, document.id)

            other = library.add_document_bytes(subject.id, "Other.txt", b"Duplicate content")
            replacement.write_bytes(other.stored_path.read_bytes())
            with self.assertRaises(DuplicateDocumentError):
                library.replace_document_contents(document.id, replacement)
            self.assertEqual(updated.stored_path.read_text(), "New transformer explanation")


if __name__ == "__main__":
    unittest.main()
