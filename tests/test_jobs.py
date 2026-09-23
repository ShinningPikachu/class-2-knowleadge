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
from src.jobs import PRIORITIES, JobError, JobManager
from src.ollama_runtime import OllamaUnloadReport, RunningOllamaModel


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

    def test_auto_unload_setting_is_persisted(self) -> None:
        self.manager.set_auto_unload_enabled(False)
        reopened = JobManager(self.root, autostart=False)
        self.addCleanup(reopened.stop)
        self.assertFalse(reopened.is_auto_unload_enabled())

    def test_agent_call_unloads_qwen_before_releasing_its_slot(self) -> None:
        events: list[tuple[str, tuple[str, ...]]] = []

        class FakeRuntime:
            def __init__(self, _host: str, timeout: float) -> None:
                self.timeout = timeout

            def unload_if_running(self, models: list[str]) -> OllamaUnloadReport:
                events.append(("unload", tuple(models)))
                return OllamaUnloadReport(stopped=list(models))

        config = PipelineConfig()
        with patch("src.jobs.OllamaRuntime", FakeRuntime):
            with self.manager.agent_qwen_slot(config):
                self.assertEqual(events, [])

        self.assertEqual(events, [("unload", (config.llm_model,))])
        self.assertIn(config.llm_model, self.manager.last_ollama_cleanup())

    def test_agent_keeps_qwen_warm_until_deactivation(self) -> None:
        cleanup_calls: list[tuple[str, ...]] = []

        class FakeRuntime:
            def __init__(self, _host: str, timeout: float) -> None:
                self.timeout = timeout

            def unload_if_running(self, models: list[str]) -> OllamaUnloadReport:
                cleanup_calls.append(tuple(models))
                return OllamaUnloadReport(stopped=list(models))

        config = PipelineConfig()
        self.manager.set_agent_active(True)
        with patch("src.jobs.OllamaRuntime", FakeRuntime):
            with self.manager.agent_qwen_slot(config):
                pass
            self.assertEqual(cleanup_calls, [])
            self.assertIn("kept warm", self.manager.last_ollama_cleanup())

            self.manager.set_agent_active(False)

        self.assertEqual(cleanup_calls, [(config.llm_model,)])
        self.assertIn("Agent deactivation", self.manager.last_ollama_cleanup())

    def test_activated_agent_reserves_its_model_before_the_first_call(self) -> None:
        config = PipelineConfig()
        self.manager.set_agent_active(True, config)

        required = self.manager._models_needed_by_pending_work(
            config.ollama_host,
            [config.llm_model, config.embedding_model],
        )

        self.assertEqual(required, [config.llm_model])

    def test_manual_unload_is_blocked_while_a_job_is_active(self) -> None:
        job = self._enqueue("Lecture", PRIORITIES["Normal"])
        self.manager._claim_next_job()
        with self.assertRaisesRegex(JobError, "lecture task is active"):
            self.manager.stop_loaded_ollama_models(PipelineConfig().ollama_host, ["qwen3.5:27b"])
        self.manager._finish_job(job.id, "cancelled", "done", "")

    def test_manual_unload_lists_and_stops_idle_models(self) -> None:
        calls: list[tuple[str, ...]] = []

        class FakeRuntime:
            def __init__(self, _host: str, timeout: float) -> None:
                self.timeout = timeout

            def running_models(self) -> list[RunningOllamaModel]:
                return [RunningOllamaModel("qwen3.5:27b", size_bytes=100)]

            def unload_models(self, models: list[str]) -> OllamaUnloadReport:
                calls.append(tuple(models))
                return OllamaUnloadReport(stopped=list(models))

        with patch("src.jobs.OllamaRuntime", FakeRuntime):
            models = self.manager.list_loaded_ollama_models(PipelineConfig().ollama_host)
            report = self.manager.stop_loaded_ollama_models(
                PipelineConfig().ollama_host,
                [models[0].name],
            )

        self.assertEqual(calls, [("qwen3.5:27b",)])
        self.assertEqual(report.stopped, ["qwen3.5:27b"])

    def test_manual_unload_is_blocked_for_a_queued_model(self) -> None:
        class FakeRuntime:
            def __init__(self, _host: str, timeout: float) -> None:
                self.timeout = timeout

        self._enqueue("Upcoming", PRIORITIES["Normal"])
        with patch("src.jobs.OllamaRuntime", FakeRuntime):
            with self.assertRaisesRegex(JobError, "queued work"):
                self.manager.stop_loaded_ollama_models(
                    PipelineConfig().ollama_host,
                    [PipelineConfig().llm_model],
                )

    def test_completed_job_keeps_models_warm_for_the_next_job(self) -> None:
        cleanup_calls: list[tuple[str, ...]] = []

        class FakeRuntime:
            def __init__(self, _host: str, timeout: float) -> None:
                self.timeout = timeout

            def unload_if_running(self, models: list[str]) -> OllamaUnloadReport:
                cleanup_calls.append(tuple(models))
                return OllamaUnloadReport(stopped=list(models))

        first = self._enqueue("First", PRIORITIES["High"])
        second = self._enqueue("Second", PRIORITIES["Normal"])
        claimed_first = self.manager._claim_next_job()
        self.assertEqual(claimed_first.id, first.id)  # type: ignore[union-attr]
        self.manager._finish_job(first.id, "completed", "done", "")

        with patch("src.jobs.OllamaRuntime", FakeRuntime):
            self.manager._cleanup_after_job(first)
            self.assertEqual(cleanup_calls, [])
            self.assertIn("kept warm", self.manager.last_ollama_cleanup())

            claimed_second = self.manager._claim_next_job()
            self.assertEqual(claimed_second.id, second.id)  # type: ignore[union-attr]
            self.manager._finish_job(second.id, "completed", "done", "")
            self.manager._cleanup_after_job(second)

        self.assertEqual(
            cleanup_calls,
            [(PipelineConfig().llm_model, PipelineConfig().embedding_model)],
        )

    def test_enqueue_waits_until_model_cleanup_finishes(self) -> None:
        enqueue_finished = threading.Event()

        with self.manager._condition:
            self.manager._ollama_maintenance = True

        def enqueue() -> None:
            self._enqueue("Queued during cleanup", PRIORITIES["Normal"])
            enqueue_finished.set()

        worker = threading.Thread(target=enqueue)
        worker.start()
        time.sleep(0.1)
        self.assertFalse(enqueue_finished.is_set())

        with self.manager._condition:
            self.manager._ollama_maintenance = False
            self.manager._condition.notify_all()

        self.assertTrue(enqueue_finished.wait(timeout=2))
        worker.join(timeout=2)

    def test_cleanup_releases_only_models_not_shared_with_queued_work(self) -> None:
        cleanup_calls: list[tuple[str, ...]] = []

        class FakeRuntime:
            def __init__(self, _host: str, timeout: float) -> None:
                self.timeout = timeout

            def unload_if_running(self, models: list[str]) -> OllamaUnloadReport:
                cleanup_calls.append(tuple(models))
                return OllamaUnloadReport(stopped=list(models))

        current = self._enqueue("Current", PRIORITIES["High"])
        upcoming_config = PipelineConfig(llm_model="another-local-model")
        self.manager.enqueue_lecture(
            config=upcoming_config,
            audio_path=None,
            presentation_path=self.source,
            lecture_title="Upcoming",
            subject_id=None,
            priority=PRIORITIES["Normal"],
        )
        self.manager._claim_next_job()
        self.manager._finish_job(current.id, "completed", "done", "")

        with patch("src.jobs.OllamaRuntime", FakeRuntime):
            self.manager._cleanup_after_job(current)

        self.assertEqual(cleanup_calls, [(PipelineConfig().llm_model,)])
        self.assertIn(PipelineConfig().embedding_model, self.manager.last_ollama_cleanup())

    def test_restart_recovers_models_from_an_interrupted_job(self) -> None:
        cleanup_calls: list[tuple[str, ...]] = []

        class FakeRuntime:
            def __init__(self, _host: str, timeout: float) -> None:
                self.timeout = timeout

            def unload_if_running(self, models: list[str]) -> OllamaUnloadReport:
                cleanup_calls.append(tuple(models))
                return OllamaUnloadReport(stopped=list(models))

        job = self._enqueue("Interrupted", PRIORITIES["Normal"])
        self.manager._claim_next_job()
        with patch("src.jobs.OllamaRuntime", FakeRuntime):
            reopened = JobManager(self.root, autostart=False)
        self.addCleanup(reopened.stop)

        recovered = reopened.get_job(job.id)
        self.assertEqual(recovered.status, "failed")
        self.assertIn("application stopped", recovered.error)
        self.assertEqual(
            cleanup_calls,
            [(PipelineConfig().llm_model, PipelineConfig().embedding_model)],
        )
        self.assertIn("Restart recovery", reopened.last_ollama_cleanup())

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

        cleanup_calls: list[tuple[str, ...]] = []

        class FakeRuntime:
            def __init__(self, _host: str, timeout: float) -> None:
                self.timeout = timeout

            def unload_if_running(self, models: list[str]) -> OllamaUnloadReport:
                cleanup_calls.append(tuple(models))
                return OllamaUnloadReport(stopped=list(models))

        job = self._enqueue("Background lecture", PRIORITIES["Normal"])
        with patch("src.jobs.LecturePipeline", FakePipeline), patch("src.jobs.OllamaRuntime", FakeRuntime):
            self.manager.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                current = self.manager.get_job(job.id)
                if current.status in {"completed", "failed"} and self.manager.last_ollama_cleanup():
                    break
                time.sleep(0.02)
        current = self.manager.get_job(job.id)
        self.assertEqual(current.status, "completed", current.error)
        self.assertEqual(current.progress, 100)
        self.assertEqual(current.result["indexed_chunks"], 3)
        self.assertEqual(
            cleanup_calls,
            [(PipelineConfig().llm_model, PipelineConfig().embedding_model)],
        )
        self.assertIn("Ollama models unloaded", current.message)


if __name__ == "__main__":
    unittest.main()
