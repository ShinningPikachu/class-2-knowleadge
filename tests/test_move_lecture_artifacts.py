"""Portable lecture evidence must follow its source document on moves."""

import tempfile
import unittest
from pathlib import Path

from src.library import LibraryError, LibraryStore
from src.lecture_artifacts import artifact_dir


class LectureArtifactMoveTests(unittest.TestCase):
    def test_folder_and_subject_moves_preserve_slide_explanations(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            library = LibraryStore(Path(root) / "library")
            original = library.create_subject("Original")
            other = library.create_subject("Other")
            first = library.create_folder(original.id, "Lecture 1")
            second = library.create_folder(original.id, "Lecture 2")
            doc = library.add_document_bytes(original.id, "Slides.pdf", b"fake slide deck", first.id)
            old_dir = artifact_dir(library, doc)
            old_dir.mkdir(parents=True)
            (old_dir / "slide_summaries.json").write_text('{"slides":[{"slide":1,"summary":"Hello"}]}')
            moved = library.move_document_to_folder(doc.id, second.id)
            self.assertEqual((artifact_dir(library, moved) / "slide_summaries.json").read_text(),
                             (old_dir / "slide_summaries.json").read_text())
            moved_again = library.move_document(doc.id, other.id)
            self.assertEqual(moved_again.subject_id, other.id)
            self.assertTrue((artifact_dir(library, moved_again) / "slide_summaries.json").is_file())
            library.delete_folder(first.id)
            self.assertFalse(old_dir.exists())
            self.assertTrue((artifact_dir(library, moved_again) / "slide_summaries.json").exists())

    def test_conflicting_destination_is_rejected_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            library = LibraryStore(Path(root) / "library")
            subject = library.create_subject("Subject")
            first = library.create_folder(subject.id, "A")
            second = library.create_folder(subject.id, "B")
            doc = library.add_document_bytes(subject.id, "Slides.pdf", b"slides", first.id)
            source = artifact_dir(library, doc)
            source.mkdir(parents=True)
            (source / "slide_summaries.json").write_text("one")
            target = library.subjects_root / subject.id / "derived" / second.id
            target.mkdir(parents=True)
            (target / "slide_summaries.json").write_text("different")
            with self.assertRaisesRegex(LibraryError, "different lecture artifacts"):
                library.move_document_to_folder(doc.id, second.id)
            self.assertEqual(library.get_document(doc.id).folder_id, first.id)
            self.assertEqual((source / "slide_summaries.json").read_text(), "one")
            self.assertEqual((target / "slide_summaries.json").read_text(), "different")


if __name__ == "__main__":
    unittest.main()
