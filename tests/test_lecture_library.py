"""Tests for storing completed lecture sources and outputs in a subject."""

from __future__ import annotations

from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path

from src.lecture_library import save_lecture_result
from src.library import LibraryStore


class LectureLibraryHandoffTest(unittest.TestCase):
    def test_only_learner_facing_sources_and_concise_pdf_are_saved(self) -> None:
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
            raw_transcript = run_dir / "transcript.raw.json"
            transcript_text = run_dir / "transcript.txt"
            slides_json = run_dir / "slides.json"
            summaries = run_dir / "slide_summaries.json"
            alignment = run_dir / "alignment.json"
            quality = run_dir / "quality_report.json"
            manifest = run_dir / "lecture_manifest.json"
            markdown.write_text("# Search\nBreadth-first search uses a queue.", encoding="utf-8")
            pdf.write_bytes(b"generated-notes-pdf")
            transcript.write_text(
                '{"paragraphs": [{"start_time": "00:00:01", "text": "Professor explanation."}]}',
                encoding="utf-8",
            )
            raw_transcript.write_text('{"paragraphs": [{"text": "raw"}]}', encoding="utf-8")
            transcript_text.write_text("[00:00:01] Professor explanation.\n", encoding="utf-8")
            slides_json.write_text('{"slides": [{"slide": 1}]}', encoding="utf-8")
            summaries.write_text('{"slides": [{"slide": 1, "summary": "Search."}]}', encoding="utf-8")
            alignment.write_text('{"slides": [{"slide": 1, "paragraph_ids": [1]}]}', encoding="utf-8")
            quality.write_text('{"status": "passed"}', encoding="utf-8")
            manifest.write_text('{"lecture_name": "Lecture_01_Introduction_to_AI"}', encoding="utf-8")
            result = SimpleNamespace(
                run_id="lecture_123",
                run_dir=run_dir,
                transcript_path=transcript,
                raw_transcript_path=raw_transcript,
                transcript_text_path=transcript_text,
                slides_path=slides_json,
                slide_summaries_path=summaries,
                alignment_path=alignment,
                quality_report_path=quality,
                manifest_path=manifest,
                markdown_path=markdown,
                pdf_path=pdf,
            )

            messages = save_lecture_result(store, subject.id, result, "Introduction to AI")
            documents = store.list_documents(subject.id)
            names = {document.original_name for document in documents}
            folder_counts = [folder.document_count for folder in store.list_folders(subject.id)]

        self.assertEqual(
            names,
            {
                "Lecture_01_Introduction_to_AI_Slides.pdf",
                "Lecture_01_Introduction_to_AI_Concise_Notes.pdf",
            },
        )
        self.assertEqual(len(messages), 2)
        self.assertEqual(
            {document.folder_name for document in documents},
            {"Introduction to AI"},
        )
        self.assertEqual(folder_counts, [2])


if __name__ == "__main__":
    unittest.main()
