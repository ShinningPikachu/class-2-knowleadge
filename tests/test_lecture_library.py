"""Tests for storing completed lecture sources and outputs in a subject."""

from __future__ import annotations

from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path

from src.lecture_library import save_lecture_result
from src.library import LibraryStore


class LectureLibraryHandoffTest(unittest.TestCase):
    def test_sources_transcript_markdown_and_pdf_are_saved_with_clear_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = LibraryStore(root / "library")
            subject = store.create_subject("Artificial Intelligence")
            run_dir = root / "run"
            input_dir = run_dir / "input"
            input_dir.mkdir(parents=True)
            (input_dir / "slides.pdf").write_bytes(b"not-a-real-pdf")
            markdown = run_dir / "lecture_notes.md"
            pdf = run_dir / "lecture_notes.pdf"
            transcript = run_dir / "transcript.json"
            markdown.write_text("# Search\nBreadth-first search uses a queue.", encoding="utf-8")
            pdf.write_bytes(b"generated-notes-pdf")
            transcript.write_text(
                '{"paragraphs": [{"start_time": "00:00:01", "text": "Professor explanation."}]}',
                encoding="utf-8",
            )
            result = SimpleNamespace(
                run_id="lecture_123",
                run_dir=run_dir,
                transcript_path=transcript,
                markdown_path=markdown,
                pdf_path=pdf,
            )

            messages = save_lecture_result(store, subject.id, result, "Introduction to AI")
            names = {document.original_name for document in store.list_documents(subject.id)}

        self.assertEqual(
            names,
            {
                "slides.pdf",
                "Introduction_to_AI_transcript.json",
                "Introduction_to_AI_notes.md",
                "Introduction_to_AI_notes.pdf",
            },
        )
        self.assertEqual(len(messages), 4)


if __name__ == "__main__":
    unittest.main()
