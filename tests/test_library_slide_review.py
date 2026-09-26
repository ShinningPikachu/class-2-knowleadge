"""Tests for slide summaries embedded in the existing library file preview."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.config import PipelineConfig
from src.jobs import JobManager
from src.library import LibraryStore
from src.ui.library_page import (
    _exact_slide_summary,
    _lecture_audio_bundle,
    _lecture_slide_bundle,
    _visible_documents,
)


class LibrarySlideReviewTest(unittest.TestCase):
    def test_library_pdf_resolves_its_exact_slide_summary_and_source_job(self) -> None:
        import fitz

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "introduction.pdf"
            with fitz.open() as pdf:
                page = pdf.new_page()
                page.insert_text((72, 72), "Intelligent agents")
                pdf.save(source)

            manager = JobManager(root, autostart=False)
            self.addCleanup(manager.stop)
            job = manager.enqueue_lecture(
                PipelineConfig(),
                audio_path=None,
                presentation_path=source,
                lecture_title="Introduction",
                subject_id=None,
            )
            manager._claim_next_job()
            run_dir = root / "runs" / "lecture-test"
            run_dir.mkdir(parents=True)
            slides_path = run_dir / "slides.json"
            summaries_path = run_dir / "slide_summaries.json"
            alignment_path = run_dir / "alignment.json"
            slides_path.write_text(
                json.dumps(
                    {
                        "slides": [
                            {
                                "slide": 1,
                                "title": "Intelligent agents",
                                "content": "An agent perceives and acts.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            summaries_path.write_text(
                json.dumps(
                    {
                        "slides": [
                            {
                                "slide": 1,
                                "title": "Intelligent agents",
                                "summary": "An agent perceives its environment and selects actions.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            alignment_path.write_text(
                json.dumps({"slides": [{"slide": 1, "paragraphs": []}]}),
                encoding="utf-8",
            )
            manager._merge_job_result(
                job.id,
                {
                    "run_dir": str(run_dir),
                    "slides_path": str(slides_path),
                    "slide_summaries_path": str(summaries_path),
                    "alignment_path": str(alignment_path),
                },
            )
            manager._finish_job(job.id, "completed", "Lecture notes are ready", "")

            library = LibraryStore(root / "library")
            subject = library.create_subject("AI")
            folder = library.create_folder(subject.id, "Introduction")
            document = library.add_document(subject.id, source, folder_id=folder.id)

            bundle = _lecture_slide_bundle(library, manager, document)

            self.assertIsNotNone(bundle)
            assert bundle is not None
            self.assertEqual(bundle.source_job.id, job.id)  # type: ignore[union-attr]
            self.assertEqual(
                _exact_slide_summary(bundle, bundle.slides[0]),
                "An agent perceives its environment and selects actions.",
            )

    def test_audio_resolves_cleaned_timeline_without_showing_transcript_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "introduction.m4a"
            source.write_bytes(b"audio")
            manager = JobManager(root, autostart=False)
            self.addCleanup(manager.stop)
            job = manager.enqueue_lecture(
                PipelineConfig(enable_transcript_cleanup=True),
                audio_path=source,
                presentation_path=None,
                lecture_title="Introduction",
                subject_id=None,
            )
            manager._claim_next_job()
            run_dir = root / "runs" / "lecture-audio-test"
            run_dir.mkdir(parents=True)
            transcript_path = run_dir / "transcript.json"
            transcript_path.write_text(
                json.dumps(
                    {
                        "metadata": {"transcript_kind": "cleaned"},
                        "paragraphs": [
                            {
                                "id": 2,
                                "start": 12.0,
                                "end": 18.0,
                                "start_time": "00:00:12",
                                "end_time": "00:00:18",
                                "text": "An agent perceives its environment.",
                            }
                        ],
                        "removed_paragraphs": [{"id": 1, "reason": "background conversation"}],
                    }
                ),
                encoding="utf-8",
            )
            manager._merge_job_result(
                job.id,
                {
                    "run_dir": str(run_dir),
                    "transcript_path": str(transcript_path),
                    "lecture_name": "Lecture_01_Introduction",
                },
            )
            manager._finish_job(job.id, "completed", "Lecture notes are ready", "")

            library = LibraryStore(root / "library")
            subject = library.create_subject("AI")
            folder = library.create_folder(subject.id, "Lecture 01 — Introduction")
            recording = library.add_document(
                subject.id,
                source,
                filename="Lecture_01_Introduction_Recording.m4a",
                folder_id=folder.id,
            )
            library.add_document(
                subject.id,
                transcript_path,
                filename="Lecture_01_Introduction_Transcript_Cleaned.json",
                folder_id=folder.id,
            )

            bundle = _lecture_audio_bundle(library, manager, recording)
            visible = _visible_documents(library.list_documents(subject.id))

            self.assertIsNotNone(bundle)
            assert bundle is not None
            self.assertEqual([item["start"] for item in bundle.paragraphs], [12.0])
            self.assertEqual([item.original_name for item in visible], [recording.original_name])


if __name__ == "__main__":
    unittest.main()
