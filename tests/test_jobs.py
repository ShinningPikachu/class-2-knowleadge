"""Tests for priority ordering and Qwen resource coordination."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.config import PipelineConfig
from src.jobs import PRIORITIES, JobManager


class JobManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "lecture.pdf"
        self.source.write_bytes(b"lecture")
        self.manager = JobManager(self.root, autostart=False)
        self.addCleanup(self.manager.stop)

    def _enqueue(self, title: str, priority: int):
        return self.manager.enqueue_lecture(
            config=PipelineConfig(),
            audio_path=None,
            presentation_path=self.source,
            lecture_title=title,
            subject_id=None,
            priority=priority,
        )

    def test_jobs_are_claimed_by_priority_then_creation_order(self) -> None:
        low = self._enqueue("Low", PRIORITIES["Low"])
        high_first = self._enqueue("High first", PRIORITIES["High"])
        high_second = self._enqueue("High second", PRIORITIES["High"])

        first = self.manager._claim_next_job()
        self.assertIsNotNone(first)
        self.assertEqual(first.id, high_first.id)  # type: ignore[union-attr]
        self.manager._finish_job(first.id, "completed", "done", "")  # type: ignore[union-attr]
        second = self.manager._claim_next_job()
        self.assertEqual(second.id, high_second.id)  # type: ignore[union-attr]
        self.manager._finish_job(second.id, "completed", "done", "")  # type: ignore[union-attr]
        third = self.manager._claim_next_job()
        self.assertEqual(third.id, low.id)  # type: ignore[union-attr]

    def test_planned_job_priority_can_change_and_job_can_cancel(self) -> None:
        job = self._enqueue("Lecture", PRIORITIES["Low"])
        updated = self.manager.update_priority(job.id, PRIORITIES["High"])
        self.assertEqual(updated.priority_label, "High")
        cancelled = self.manager.cancel_job(job.id)
        self.assertEqual(cancelled.status, "cancelled")

    def test_agent_activation_pauses_lecture_qwen_slot(self) -> None:
        job = self._enqueue("Lecture", PRIORITIES["Normal"])
        claimed = self.manager._claim_next_job()
        self.assertEqual(claimed.id, job.id)  # type: ignore[union-attr]
        self.manager.set_agent_active(True)
        acquired = threading.Event()
        released = threading.Event()

        def use_lecture_qwen() -> None:
            with self.manager.lecture_qwen_slot(job.id):
                acquired.set()
            released.set()

        worker = threading.Thread(target=use_lecture_qwen)
        worker.start()
        time.sleep(0.15)
        self.assertFalse(acquired.is_set())
        waiting = self.manager.get_job(job.id)
        self.assertEqual(waiting.status, "waiting")
        self.assertIn("local agent", waiting.message)

        self.manager.set_agent_active(False)
        self.assertTrue(acquired.wait(timeout=2))
        self.assertTrue(released.wait(timeout=2))
        worker.join(timeout=2)
        self.assertEqual(self.manager.get_job(job.id).status, "running")

    def test_agent_activation_is_persisted(self) -> None:
        self.manager.set_agent_active(True)
        reopened = JobManager(self.root, autostart=False)
        self.addCleanup(reopened.stop)
        self.assertTrue(reopened.is_agent_active())

    def test_background_worker_completes_a_lecture_job(self) -> None:
        output_root = self.root / "fake-run"
        output_root.mkdir()
        for name in (
            "lecture_notes.md",
            "lecture_notes.pdf",
            "transcript.json",
            "slides.json",
            "alignment.json",
            "quality.json",
        ):
            (output_root / name).write_text("test", encoding="utf-8")

        class FakePipeline:
            def __init__(self, *_args: object, qwen_guard=None, **_kwargs: object) -> None:
                self.qwen_guard = qwen_guard

            def run(self, *_args: object, on_stage=None, **_kwargs: object):
                on_stage("recording", "Transcription progress: 1/1 chunks complete")
                on_stage("notes", "Writing study notes (1/1)")
                with self.qwen_guard():
                    pass
                return SimpleNamespace(
                    run_id="fake-run",
                    run_dir=output_root,
                    markdown_path=output_root / "lecture_notes.md",
                    pdf_path=output_root / "lecture_notes.pdf",
                    transcript_path=output_root / "transcript.json",
                    slides_path=output_root / "slides.json",
                    alignment_path=output_root / "alignment.json",
                    quality_report_path=output_root / "quality.json",
                    indexed_chunks=3,
                )

        job = self._enqueue("Background lecture", PRIORITIES["Normal"])
        with patch("src.jobs.LecturePipeline", FakePipeline):
            self.manager.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                current = self.manager.get_job(job.id)
                if current.status in {"completed", "failed"}:
                    break
                time.sleep(0.02)
        current = self.manager.get_job(job.id)
        self.assertEqual(current.status, "completed", current.error)
        self.assertEqual(current.progress, 100)
        self.assertEqual(current.result["indexed_chunks"], 3)


if __name__ == "__main__":
    unittest.main()
