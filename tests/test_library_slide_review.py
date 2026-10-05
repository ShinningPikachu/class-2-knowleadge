"""Tests for slide summaries embedded in the existing library file preview."""

from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from src.config import PipelineConfig
from src.jobs import JobManager
from src.library import LibraryStore
from src.ui.library_page import (
    _exact_slide_summary,
    _slide_explanation_translations,
    _slide_content_translations,
    _slide_content_markdown,
    _saved_lecture_translations,
    _translated_slide_sections,
    _lecture_audio_bundle,
    _lecture_slide_bundle,
    _matching_lecture_job,
    _visible_documents,
)


class LibrarySlideReviewTest(unittest.TestCase):
    def test_translations_follow_the_exact_slide_and_current_explanation(self) -> None:
        slide = {"slide": 2}
        bundle = SimpleNamespace(source_job=SimpleNamespace(id="lecture-a"), summaries={2: "Original explanation."})

        def job(source="lecture-a", number=2, text="Original explanation.", status="completed"):
            return SimpleNamespace(kind="translation", status=status,
                payload={"source_job_id": source, "slide_number": number,
                         "source_explanation": text, "target_language": "French"},
                result={"translated_explanation": "Explication française."})

        jobs = [job(source="lecture-b"), job(number=1), job(text="Old explanation."), job(), job(status="running")]
        manager = SimpleNamespace(list_jobs=lambda **kwargs: jobs)
        explanations, pending = _slide_explanation_translations(manager, bundle, slide)
        self.assertEqual(explanations, {"English": "Original explanation.", "French": "Explication française."})
        self.assertEqual(len(pending), 1)


    def test_content_language_switch_uses_full_matching_slide_text(self) -> None:
        slide = {"slide": 3, "title": "Agents", "content": "All slide text, including the final line."}
        source = _slide_content_markdown(slide)
        bundle = SimpleNamespace(source_job=SimpleNamespace(id="lecture-a"))
        def job(content=source, number=3):
            return SimpleNamespace(kind="translation", status="completed",
                payload={"source_job_id": "lecture-a", "slide_number": number,
                         "source_slide_content": content, "target_language": "Chinese (Simplified)"},
                result={"translated_slide_content": "## 智能体\n\n完整内容。"})
        manager = SimpleNamespace(list_jobs=lambda **kwargs: [job(content="Outdated"), job(number=4), job()])
        result = _slide_content_translations(manager, bundle, slide)
        self.assertEqual(result["English"], source)
        self.assertEqual(result["Chinese (Simplified)"], "## 智能体\n\n完整内容。")

    def test_existing_chinese_notes_are_loaded_from_the_lecture_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notes = root / "Notes.chinese_simplified.md"
            notes.write_text("# 中文笔记\n\n## 幻灯片 1：标题\n\n第一张的完整内容。\n\n## 幻灯片 3：标题\n\n第三张的内容。", encoding="utf-8")
            related = [SimpleNamespace(original_name=notes.name, stored_path=notes)]
            saved = _saved_lecture_translations(related, None)
            self.assertEqual(set(saved), {1, 3})
            bundle = SimpleNamespace(source_job=None, saved_translations=saved)
            manager = SimpleNamespace(list_jobs=lambda **kwargs: [])
            first = _slide_content_translations(manager, bundle, {"slide": 1, "content": "English"})
            second = _slide_content_translations(manager, bundle, {"slide": 2, "content": "English"})
            self.assertEqual(list(first), ["English", "Chinese (Simplified)"])
            self.assertIn("第一张的完整内容", first["Chinese (Simplified)"])
            self.assertEqual(list(second), ["English"])

    def test_run_translation_metadata_discovers_existing_language(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "lecture_notes.md"
            source.write_text("## Slide 1: Title\n\nEnglish.")
            translated = root / "translations" / "lecture_notes.chinese_simplified.md"
            translated.parent.mkdir()
            translated.write_text("## 幻灯片 1：标题\n\n中文内容。", encoding="utf-8")
            translated.with_suffix(".md.metadata.json").write_text(json.dumps({"target_language": "Chinese (Simplified)"}))
            job = SimpleNamespace(result={"markdown_path": str(source)})
            saved = _saved_lecture_translations([], job)
            self.assertIn("中文内容", saved[1]["Chinese (Simplified)"])

    def test_duplicate_slide_numbers_do_not_attach_ambiguous_translation(self) -> None:
        self.assertEqual(_translated_slide_sections("## 幻灯片 1：甲\n\n甲。\n\n## 幻灯片 1：乙\n\n乙。"), {})

    def test_language_preference_survives_navigation_and_missing_translation(self) -> None:
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_string("""
import streamlit as st
from src.ui.library_page import _render_slide_text_language
slide = st.session_state.get("slide", 1)
contents = {"English": "English text"}
if slide != 2:
    contents["Chinese (Simplified)"] = "中文"
_render_slide_text_language("lecture-a", slide, contents)
if st.button("Next"):
    st.session_state["slide"] = slide + 1
    st.rerun()
""").run()
        app.selectbox[0].select("Chinese (Simplified)").run()
        self.assertEqual(app.selectbox[0].value, "Chinese (Simplified)")
        app.button[0].click().run()
        self.assertEqual(app.selectbox[0].options, ["English"])
        self.assertEqual(app.selectbox[0].value, "English")
        app.button[0].click().run()
        self.assertEqual(app.selectbox[0].value, "Chinese (Simplified)")
        app.selectbox[0].select("English").run()
        app.button[0].click().run()
        self.assertEqual(app.selectbox[0].value, "English")
        self.assertFalse(app.exception)

    def test_library_pdf_resolves_its_exact_slide_summary_and_source_job(self) -> None:
        import fitz

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "introduction.pdf"
            with fitz.open() as pdf:
                page = pdf.new_page()
                page.insert_text((72, 72), "Intelligent agents")
                pdf.save(source)

            library = LibraryStore(root / "library")
            subject = library.create_subject("AI")
            manager = JobManager(root, autostart=False)
            self.addCleanup(manager.stop)
            job = manager.enqueue_lecture(
                PipelineConfig(),
                audio_path=None,
                presentation_path=source,
                lecture_title="Introduction",
                subject_id=subject.id,
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

            folder = library.get_folder(str(job.payload["library_folder_id"]))
            document = library.add_document(subject.id, source, folder_id=folder.id)

            bundle = _lecture_slide_bundle(library, manager, document)

            self.assertIsNotNone(bundle)
            assert bundle is not None
            self.assertEqual(bundle.source_job.id, job.id)  # type: ignore[union-attr]
            self.assertEqual(
                _exact_slide_summary(bundle, bundle.slides[0]),
                "An agent perceives its environment and selects actions.",
            )

    def test_generic_lecture_folder_never_links_to_a_different_subject(self) -> None:
        """Regression: every subject can have a folder named ``Lecture 01``."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "sources"
            source_root.mkdir()

            def create_slide(path: Path, text: str) -> None:
                # The resolver uses the immutable file checksum, so a small
                # byte fixture is sufficient and keeps this regression test
                # independent of the optional PDF preview runtime.
                path.write_bytes(text.encode("utf-8"))

            ai_source = source_root / "ai" / "Slides.pdf"
            speech_source = source_root / "speech" / "Slides.pdf"
            ai_source.parent.mkdir()
            speech_source.parent.mkdir()
            create_slide(ai_source, "Artificial intelligence")
            create_slide(speech_source, "Speech science")

            library = LibraryStore(root / "library")
            ai = library.create_subject("Artificial Intelligence")
            speech = library.create_subject("Speech Science")
            manager = JobManager(root, autostart=False)
            self.addCleanup(manager.stop)

            ai_job = manager.enqueue_lecture(
                PipelineConfig(), None, ai_source, "Lecture 01", ai.id
            )
            speech_job = manager.enqueue_lecture(
                PipelineConfig(), None, speech_source, "Lecture 01", speech.id
            )
            for job in (ai_job, speech_job):
                manager._finish_job(job.id, "completed", "Lecture notes are ready", "")

            ai_folder = library.get_folder(str(ai_job.payload["library_folder_id"]))
            ai_document = library.add_document(
                ai.id, ai_source, filename="Slides.pdf", folder_id=ai_folder.id
            )

            self.assertEqual(ai_folder.name, "Lecture 01")
            self.assertEqual(
                _matching_lecture_job(manager, ai_document).id,  # type: ignore[union-attr]
                ai_job.id,
            )

            unrelated_document = library.add_document_bytes(
                ai.id,
                "Additional slides.pdf",
                b"A different deck added to the same folder",
                folder_id=ai_folder.id,
            )
            self.assertIsNone(_matching_lecture_job(manager, unrelated_document))

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
