"""Tests for priority ordering and Qwen resource coordination."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.config import PipelineConfig
from src.jobs import PRIORITIES, JobDeferred, JobError, JobManager
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

    def test_existing_queue_database_is_migrated_for_safe_defer(self) -> None:
        legacy_root = self.root / "legacy-project"
        legacy_jobs = legacy_root / "jobs"
        legacy_jobs.mkdir(parents=True)
        database_path = legacy_jobs / "jobs.sqlite3"
        now = "2026-01-01T00:00:00+00:00"
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                CREATE TABLE jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                    status TEXT NOT NULL, priority INTEGER NOT NULL,
                    stage TEXT NOT NULL DEFAULT '', progress INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '', completed_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                """
                INSERT INTO jobs(
                    id, kind, title, status, priority, stage, progress, message,
                    payload_json, created_at, updated_at
                ) VALUES ('legacy', 'lecture', 'Legacy task', 'queued', 50, 'planned', 0,
                          'Waiting', ?, ?, ?)
                """,
                (json.dumps({"config": PipelineConfig().__dict__}), now, now),
            )

        migrated = JobManager(legacy_root, autostart=False)
        self.addCleanup(migrated.stop)
        job = migrated.get_job("legacy")
        self.assertFalse(job.defer_requested)
        self.assertEqual(migrated.defer_job(job.id).status, "deferred")

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

    def test_planned_job_can_move_to_later_and_return_to_priority_queue(self) -> None:
        job = self._enqueue("Future lecture", PRIORITIES["Low"])

        deferred = self.manager.defer_job(job.id)
        self.assertEqual(deferred.status, "deferred")
        self.assertEqual(self.manager.counts()["deferred"], 1)
        self.assertIsNone(self.manager._claim_next_job())

        resumed = self.manager.resume_job(job.id, PRIORITIES["High"])
        self.assertEqual(resumed.status, "queued")
        self.assertEqual(resumed.priority_label, "High")
        claimed = self.manager._claim_next_job()
        self.assertEqual(claimed.id, job.id)  # type: ignore[union-attr]
        messages = [event.message for event in self.manager.list_job_events(job.id)]
        self.assertTrue(any("moved to Later" in message for message in messages))
        self.assertTrue(any("returned to the queue" in message for message in messages))

    def test_running_job_defers_cooperatively_and_preserves_partial_transcript(self) -> None:
        job = self._enqueue("Long recording", PRIORITIES["Normal"])
        self.manager._claim_next_job()
        run_directory = self.root / "runs" / "long-recording"
        run_directory.mkdir(parents=True)
        partial_path = run_directory / "transcript.partial.json"
        partial_path.write_text('{"paragraphs": [{"text": "Saved speech"}]}', encoding="utf-8")
        self.manager._merge_job_result(
            job.id,
            {"run_dir": str(run_directory), "partial_transcript_path": str(partial_path)},
        )

        stopping = self.manager.defer_job(job.id)
        self.assertEqual(stopping.status, "running")
        self.assertTrue(stopping.defer_requested)
        with self.assertRaises(JobDeferred):
            self.manager._raise_if_interrupted(job.id)
        self.manager._mark_job_deferred(job.id, "Stopped safely and saved for future processing.")

        deferred = self.manager.get_job(job.id)
        self.assertEqual(deferred.status, "deferred")
        self.assertFalse(deferred.defer_requested)
        self.assertEqual(self.manager.get_transcript_path(job.id), partial_path.resolve())

    def test_resumed_job_uses_saved_run_instead_of_starting_over(self) -> None:
        job = self._enqueue("Resume recording summary", PRIORITIES["Normal"])
        self.manager._claim_next_job()
        run_directory = self.root / "runs" / "resume-recording"
        run_directory.mkdir(parents=True)
        self.manager._merge_job_result(job.id, {"run_dir": str(run_directory)})
        self.manager._mark_job_deferred(job.id, "Saved for later")
        self.manager.resume_job(job.id)
        resumed_job = self.manager._claim_next_job()
        calls: list[Path] = []

        for name in (
            "lecture_notes.md",
            "lecture_notes.pdf",
            "transcript.json",
            "transcript.raw.json",
            "slides.json",
            "alignment.json",
            "quality.json",
        ):
            (run_directory / name).write_text("test", encoding="utf-8")

        class FakePipeline:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.current_run_dir = None

            def run(self, *_args: object, **_kwargs: object):
                raise AssertionError("A resumed job must not start a new run")

            def resume(self, saved_run: str, *_args: object, on_stage=None, **_kwargs: object):
                self.current_run_dir = Path(saved_run)
                calls.append(self.current_run_dir)
                on_stage("recording", "Transcription progress: 1/1 chunks complete")
                on_stage("notes", "Writing study notes (1/1)")
                return SimpleNamespace(
                    run_id=run_directory.name,
                    run_dir=run_directory,
                    markdown_path=run_directory / "lecture_notes.md",
                    pdf_path=run_directory / "lecture_notes.pdf",
                    transcript_path=run_directory / "transcript.json",
                    raw_transcript_path=run_directory / "transcript.raw.json",
                    slides_path=run_directory / "slides.json",
                    alignment_path=run_directory / "alignment.json",
                    quality_report_path=run_directory / "quality.json",
                    indexed_chunks=2,
                )

        with patch("src.jobs.LecturePipeline", FakePipeline):
            self.manager._execute_job(resumed_job)  # type: ignore[arg-type]

        self.assertEqual(calls, [run_directory])
        self.assertEqual(self.manager.get_job(job.id).status, "completed")

    def test_worker_moves_a_safe_stop_signal_to_later_instead_of_failed(self) -> None:
        self.manager.set_auto_unload_enabled(False)
        run_directory = self.root / "runs" / "worker-safe-stop"
        run_directory.mkdir(parents=True)
        entered_processing = threading.Event()
        continue_processing = threading.Event()

        class FakePipeline:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.current_run_dir = run_directory

            def run(self, *_args: object, on_stage=None, **_kwargs: object):
                on_stage("recording", "Transcription started")
                entered_processing.set()
                continue_processing.wait(timeout=2)
                on_stage("recording", "Transcription progress: 1/2 chunks complete")
                raise AssertionError("The safe-stop callback should interrupt the pipeline")

        job = self._enqueue("Worker safe stop", PRIORITIES["Normal"])
        with patch("src.jobs.LecturePipeline", FakePipeline):
            self.manager.start()
            self.assertTrue(entered_processing.wait(timeout=2))
            stopping = self.manager.defer_job(job.id)
            self.assertTrue(stopping.defer_requested)
            continue_processing.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and self.manager.get_job(job.id).status != "deferred":
                time.sleep(0.02)

        deferred = self.manager.get_job(job.id)
        self.assertEqual(deferred.status, "deferred", deferred.error)
        self.assertEqual(deferred.error, "")
        self.assertIn("saved for future", deferred.message)

    def test_processing_log_and_partial_transcript_survive_a_failed_job(self) -> None:
        job = self._enqueue("Interrupted recording", PRIORITIES["Normal"])
        self.manager._claim_next_job()
        run_directory = self.root / "runs" / "interrupted-recording"
        run_directory.mkdir(parents=True)
        partial_path = run_directory / "transcript.partial.json"
        partial_path.write_text(
            json.dumps(
                {
                    "metadata": {"is_partial": True, "completed_chunks": 1, "total_chunks": 3},
                    "segments": [],
                    "paragraphs": [{"text": "Durable partial lecture transcript."}],
                }
            ),
            encoding="utf-8",
        )
        self.manager._merge_job_result(
            job.id,
            {
                "run_dir": str(run_directory),
                "partial_transcript_path": str(partial_path),
            },
        )
        self.manager._update_job(
            job.id,
            status="running",
            stage="recording",
            progress=23,
            message="Transcription progress: 1/3 chunks complete",
        )
        self.manager._finish_job(job.id, "failed", "Task failed", "simulated failure")

        self.assertEqual(self.manager.get_transcript_path(job.id), partial_path.resolve())
        events = self.manager.list_job_events(job.id)
        self.assertEqual(events[0].stage, "planned")
        self.assertTrue(any(event.stage == "recording" and event.progress == 23 for event in events))
        self.assertEqual(events[-1].level, "error")
        self.assertIn("simulated failure", events[-1].message)

        reopened = JobManager(self.root, autostart=False)
        self.addCleanup(reopened.stop)
        self.assertEqual(reopened.get_transcript_path(job.id), partial_path.resolve())
        self.assertEqual([event.id for event in reopened.list_job_events(job.id)], [event.id for event in events])

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

    def test_translation_is_an_explicit_qwen_only_queue_job(self) -> None:
        self.manager.set_auto_unload_enabled(False)
        source_job = self._enqueue("English lecture", PRIORITIES["Normal"])
        claimed = self.manager._claim_next_job()
        self.assertEqual(claimed.id, source_job.id)  # type: ignore[union-attr]
        run_directory = self.root / "runs" / "english-lecture"
        run_directory.mkdir(parents=True)
        source_markdown = run_directory / "lecture_notes.md"
        source_markdown.write_text("# English lecture\n\nOriginal English notes.\n", encoding="utf-8")
        self.manager._merge_job_result(source_job.id, {"markdown_path": str(source_markdown)})
        self.manager._finish_job(source_job.id, "completed", "Lecture notes are ready", "")

        translation_job = self.manager.enqueue_translation(
            source_job.id,
            "Chinese (Simplified)",
            priority=PRIORITIES["High"],
        )

        self.assertEqual(translation_job.kind, "translation")
        self.assertEqual(translation_job.payload["source_language"], "English")
        self.assertEqual(translation_job.payload["target_language"], "Chinese (Simplified)")
        self.assertFalse((run_directory / "translations").exists())
        self.assertEqual(
            self.manager._pending_model_requirements(),
            [(PipelineConfig().ollama_host, PipelineConfig().llm_model)],
        )

        class FakeTranslator:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            @staticmethod
            def validate_target_language(value: str) -> str:
                return value

            def translate(self, _source: str, _target: str, output: Path, progress=None) -> str:
                translated = "# 英语讲座\n\n中文笔记。\n"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(translated, encoding="utf-8")
                progress(1, 1, "Translated translation batch 1/1")
                return translated

        def fake_export(_markdown: str, output: Path) -> Path:
            output.write_bytes(b"translated pdf")
            return output

        claimed_translation = self.manager._claim_next_job()
        self.assertEqual(claimed_translation.id, translation_job.id)  # type: ignore[union-attr]
        with patch("src.jobs.MarkdownTranslator", FakeTranslator), patch("src.jobs.export_pdf", fake_export):
            self.manager._execute_job(claimed_translation)  # type: ignore[arg-type]

        completed = self.manager.get_job(translation_job.id)
        self.assertEqual(completed.status, "completed", completed.error)
        self.assertEqual(completed.result["source_language"], "English")
        self.assertEqual(completed.result["target_language"], "Chinese (Simplified)")
        self.assertTrue(Path(completed.result["markdown_path"]).is_file())
        self.assertTrue(Path(completed.result["pdf_path"]).is_file())

    def test_restart_moves_an_interrupted_job_to_later_and_recovers_models(self) -> None:
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
        self.assertEqual(recovered.status, "deferred")
        self.assertEqual(recovered.error, "")
        self.assertIn("moved to Later", recovered.message)
        self.assertEqual(
            cleanup_calls,
            [(PipelineConfig().llm_model, PipelineConfig().embedding_model)],
        )
        self.assertIn("Restart recovery", reopened.last_ollama_cleanup())

    def test_restart_keeps_a_requested_safe_stop_in_later(self) -> None:
        self.manager.set_auto_unload_enabled(False)
        job = self._enqueue("Stop during shutdown", PRIORITIES["Normal"])
        self.manager._claim_next_job()
        self.manager.defer_job(job.id)

        reopened = JobManager(self.root, autostart=False)
        self.addCleanup(reopened.stop)

        recovered = reopened.get_job(job.id)
        self.assertEqual(recovered.status, "deferred")
        self.assertFalse(recovered.defer_requested)
        self.assertIn("later", recovered.message.lower())
        self.assertEqual(reopened.list_job_events(job.id)[-1].data["status"], "deferred")

    def test_background_worker_completes_a_lecture_job(self) -> None:
        output_root = self.root / "fake-run"
        output_root.mkdir()
        for name in (
            "lecture_notes.md",
            "lecture_notes.pdf",
            "transcript.json",
            "transcript.raw.json",
            "slides.json",
            "alignment.json",
            "quality.json",
        ):
            (output_root / name).write_text("test", encoding="utf-8")

        class FakePipeline:
            def __init__(self, *_args: object, qwen_guard=None, **_kwargs: object) -> None:
                self.qwen_guard = qwen_guard
                self.current_run_dir = output_root

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
                    raw_transcript_path=output_root / "transcript.raw.json",
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
        self.assertEqual(self.manager.get_transcript_path(job.id), (output_root / "transcript.json").resolve())
        self.assertEqual(
            self.manager.get_raw_transcript_path(job.id),
            (output_root / "transcript.raw.json").resolve(),
        )
        self.assertEqual(self.manager.list_job_events(job.id)[-2].stage, "complete")
        self.assertEqual(
            cleanup_calls,
            [(PipelineConfig().llm_model, PipelineConfig().embedding_model)],
        )
        self.assertIn("Ollama models unloaded", current.message)


if __name__ == "__main__":
    unittest.main()
