"""Persistent priority queue and resource coordinator for background work."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import sqlite3
import threading
from typing import Any, Iterator
from urllib.parse import urlparse
from uuid import uuid4

from .config import PipelineConfig
from .lecture_library import save_lecture_result
from .library import LibraryStore
from .ollama_runtime import (
    OllamaRuntime,
    OllamaRuntimeError,
    OllamaUnloadReport,
    RunningOllamaModel,
    model_names_match,
)
from .pipeline import LecturePipeline
from .utils import safe_filename


PRIORITIES = {"High": 10, "Normal": 50, "Low": 90}
PRIORITY_LABELS = {value: label for label, value in PRIORITIES.items()}
FINAL_STATUSES = {"completed", "failed", "cancelled"}


class JobError(RuntimeError):
    """Raised when a background job cannot be queued or managed."""


class JobCancelled(RuntimeError):
    """Raised cooperatively when the user cancels a running job."""


@dataclass(frozen=True)
class JobRecord:
    id: str
    kind: str
    title: str
    status: str
    priority: int
    stage: str
    progress: int
    message: str
    payload: dict[str, Any]
    result: dict[str, Any]
    error: str
    cancel_requested: bool
    created_at: str
    updated_at: str
    started_at: str
    completed_at: str

    @property
    def priority_label(self) -> str:
        return PRIORITY_LABELS.get(self.priority, str(self.priority))


class JobManager:
    """Run queued lecture jobs in priority order and arbitrate local Qwen use."""

    def __init__(self, project_root: str | Path, autostart: bool = True) -> None:
        self.project_root = Path(project_root).resolve()
        self.root = self.project_root / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.database_path = self.root / "jobs.sqlite3"
        self._database_lock = threading.RLock()
        self._condition = threading.Condition(threading.RLock())
        self._qwen_owner: str | None = None
        self._ollama_maintenance = False
        self._last_ollama_cleanup = ""
        self._stop_requested = False
        self._worker: threading.Thread | None = None
        interrupted_configs = self._initialize_schema()
        self._agent_active = self._load_state_flag("agent_active", default=False)
        self._auto_unload_ollama = self._load_state_flag("auto_unload_ollama", default=True)
        self._last_agent_config = self._load_config_state("agent_last_config")
        if self._auto_unload_ollama and interrupted_configs:
            self._recover_interrupted_ollama(interrupted_configs)
        if autostart:
            self.start()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize_schema(self) -> list[PipelineConfig]:
        interrupted_payloads: list[str] = []
        with self._database_lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    stage TEXT NOT NULL DEFAULT '',
                    progress INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    completed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_queue
                ON jobs(status, priority, created_at);

                CREATE TABLE IF NOT EXISTS scheduler_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            interrupted_payloads.extend(
                str(row["payload_json"])
                for row in connection.execute(
                    "SELECT payload_json FROM jobs WHERE status IN ('running', 'waiting')"
                ).fetchall()
            )
            agent_session = connection.execute(
                "SELECT value FROM scheduler_state WHERE key = 'agent_ollama_session'"
            ).fetchone()
            if agent_session:
                try:
                    session_config = json.loads(str(agent_session["value"]))
                    interrupted_payloads.append(json.dumps({"config": session_config}))
                except (TypeError, json.JSONDecodeError):
                    pass
                connection.execute("DELETE FROM scheduler_state WHERE key = 'agent_ollama_session'")
            now = self._timestamp()
            connection.execute(
                """
                UPDATE jobs
                SET status = 'failed', error = ?, message = ?, updated_at = ?, completed_at = ?
                WHERE status IN ('running', 'waiting')
                """,
                (
                    "The application stopped before this task completed.",
                    "Interrupted by application restart",
                    now,
                    now,
                ),
            )
        configs: list[PipelineConfig] = []
        for raw_payload in interrupted_payloads:
            try:
                payload = json.loads(raw_payload)
                configs.append(PipelineConfig(**payload["config"]))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return configs

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop_requested = False
        self._worker = threading.Thread(target=self._worker_loop, name="class-knowledge-jobs", daemon=True)
        self._worker.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_requested = True
        with self._condition:
            self._condition.notify_all()
        if self._worker:
            self._worker.join(timeout=timeout)

    def enqueue_lecture(
        self,
        config: PipelineConfig,
        audio_path: str | Path | None,
        presentation_path: str | Path | None,
        lecture_title: str | None,
        subject_id: str | None,
        priority: int = PRIORITIES["Normal"],
        resume_run_directory: str | None = None,
    ) -> JobRecord:
        """Copy volatile inputs into job storage and enqueue a lecture task."""
        config.validate()
        if priority not in PRIORITY_LABELS:
            raise JobError("Priority must be High, Normal, or Low.")
        if not resume_run_directory and audio_path is None and presentation_path is None:
            raise JobError("A lecture job needs a recording, a slide deck, or a run folder to resume.")

        job_id = uuid4().hex
        job_root = self.root / job_id
        input_root = job_root / "input"
        input_root.mkdir(parents=True, exist_ok=False)
        stored_audio = self._copy_job_input(audio_path, input_root, "recording")
        stored_presentation = self._copy_job_input(presentation_path, input_root, "slides")
        title = (lecture_title or "Lecture notes").strip() or "Lecture notes"
        payload = {
            "config": asdict(config),
            "audio_path": str(stored_audio) if stored_audio else None,
            "presentation_path": str(stored_presentation) if stored_presentation else None,
            "lecture_title": lecture_title,
            "subject_id": subject_id,
            "resume_run_directory": resume_run_directory,
        }
        now = self._timestamp()
        with self._condition:
            while self._ollama_maintenance:
                self._condition.wait(timeout=0.5)
            with self._database_lock, self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO jobs(
                        id, kind, title, status, priority, stage, progress, message,
                        payload_json, created_at, updated_at
                    ) VALUES (?, 'lecture', ?, 'queued', ?, 'planned', 0, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        title,
                        priority,
                        "Waiting for its turn",
                        json.dumps(payload),
                        now,
                        now,
                    ),
                )
            self._condition.notify_all()
        return self.get_job(job_id)

    def _copy_job_input(self, source: str | Path | None, input_root: Path, label: str) -> Path | None:
        if source is None:
            return None
        source_path = Path(source)
        if not source_path.is_file():
            raise JobError(f"Job input not found: {source_path}")
        destination = input_root / f"{label}_{safe_filename(source_path.name)}"
        try:
            shutil.copy2(source_path, destination)
        except OSError as exc:
            raise JobError(f"Could not stage {source_path.name}: {exc}") from exc
        return destination

    def list_jobs(self, limit: int = 100) -> list[JobRecord]:
        with self._database_lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                ORDER BY
                    CASE status
                        WHEN 'running' THEN 0
                        WHEN 'waiting' THEN 1
                        WHEN 'queued' THEN 2
                        ELSE 3
                    END,
                    priority ASC,
                    created_at DESC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def get_job(self, job_id: str) -> JobRecord:
        with self._database_lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobError("The selected job no longer exists.")
        return self._job_from_row(row)

    def update_priority(self, job_id: str, priority: int) -> JobRecord:
        if priority not in PRIORITY_LABELS:
            raise JobError("Priority must be High, Normal, or Low.")
        with self._database_lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET priority = ?, updated_at = ? WHERE id = ? AND status = 'queued'",
                (priority, self._timestamp(), job_id),
            )
        if cursor.rowcount != 1:
            raise JobError("Only planned jobs can have their priority changed.")
        with self._condition:
            self._condition.notify_all()
        return self.get_job(job_id)

    def cancel_job(self, job_id: str) -> JobRecord:
        job = self.get_job(job_id)
        if job.status in FINAL_STATUSES:
            return job
        now = self._timestamp()
        with self._database_lock, self._connect() as connection:
            if job.status == "queued":
                connection.execute(
                    """
                    UPDATE jobs SET status = 'cancelled', message = 'Cancelled before start',
                                    cancel_requested = 1, updated_at = ?, completed_at = ?
                    WHERE id = ?
                    """,
                    (now, now, job_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE jobs
                    SET cancel_requested = 1, message = 'Cancellation requested', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, job_id),
                )
        with self._condition:
            self._condition.notify_all()
        return self.get_job(job_id)

    def counts(self) -> dict[str, int]:
        values = {"queued": 0, "running": 0, "waiting": 0, "completed": 0, "failed": 0, "cancelled": 0}
        with self._database_lock, self._connect() as connection:
            rows = connection.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status").fetchall()
        for row in rows:
            values[str(row["status"])] = int(row["count"])
        return values

    def is_agent_active(self) -> bool:
        with self._condition:
            return self._agent_active

    def is_auto_unload_enabled(self) -> bool:
        with self._condition:
            return self._auto_unload_ollama

    def set_auto_unload_enabled(self, enabled: bool) -> None:
        with self._condition:
            self._auto_unload_ollama = bool(enabled)
            self._set_state("auto_unload_ollama", "1" if enabled else "0")
            self._condition.notify_all()

    def last_ollama_cleanup(self) -> str:
        with self._condition:
            return self._last_ollama_cleanup

    def list_loaded_ollama_models(self, host: str) -> list[RunningOllamaModel]:
        """Inspect the configured local Ollama server without changing it."""
        self._validate_ollama_host(host)
        runtime: OllamaRuntime | None = None
        try:
            runtime = OllamaRuntime(host, timeout=1.5)
            return runtime.running_models()
        except OllamaRuntimeError as exc:
            raise JobError(str(exc)) from exc
        finally:
            self._close_runtime(runtime)

    def stop_loaded_ollama_models(
        self,
        host: str,
        models: list[str] | None = None,
    ) -> OllamaUnloadReport:
        """Manually unload idle models without terminating the Ollama server."""
        self._validate_ollama_host(host)
        with self._condition:
            if self._ollama_maintenance or self._qwen_owner is not None:
                raise JobError(
                    "An Ollama operation is currently active. Wait for it to finish before unloading models."
                )
            self._ollama_maintenance = True
            if self._has_active_jobs():
                self._ollama_maintenance = False
                self._condition.notify_all()
                raise JobError("Models cannot be unloaded manually while a lecture task is active.")
        runtime: OllamaRuntime | None = None
        try:
            runtime = OllamaRuntime(host, timeout=3.0)
            targets = models
            if targets is None:
                targets = [model.name for model in runtime.running_models()]
            required = self._models_needed_by_pending_work(host, targets)
            if required:
                raise JobError(
                    "Models needed by activated or queued work cannot be unloaded: "
                    + ", ".join(required)
                )
            report = runtime.unload_models(targets)
            self._remember_cleanup(report)
            return report
        except OllamaRuntimeError as exc:
            raise JobError(str(exc)) from exc
        finally:
            self._close_runtime(runtime)
            with self._condition:
                self._ollama_maintenance = False
                self._condition.notify_all()

    def set_agent_active(self, active: bool, config: PipelineConfig | None = None) -> None:
        cleanup_config: PipelineConfig | None = None
        with self._condition:
            if active:
                while self._ollama_maintenance:
                    self._condition.wait(timeout=0.5)
            was_active = self._agent_active
            self._agent_active = bool(active)
            self._set_state("agent_active", "1" if active else "0")
            if active and config is not None:
                self._last_agent_config = config
                self._set_state("agent_last_config", json.dumps(asdict(config)))
            if (
                was_active
                and not active
                and self._auto_unload_ollama
                and self._last_agent_config is not None
                and self._qwen_owner is None
            ):
                while self._ollama_maintenance:
                    self._condition.wait(timeout=0.5)
                self._ollama_maintenance = True
                cleanup_config = self._last_agent_config
            self._condition.notify_all()
        if cleanup_config is not None:
            try:
                report = self._cleanup_unused_configured_models(
                    cleanup_config,
                    [cleanup_config.llm_model],
                )
                self._remember_cleanup(report, prefix="Agent deactivation")
            finally:
                with self._condition:
                    self._ollama_maintenance = False
                    self._condition.notify_all()

    @contextmanager
    def agent_qwen_slot(self, config: PipelineConfig | None = None) -> Iterator[None]:
        """Give an interactive call exclusive Qwen access and release it afterward."""
        with self._condition:
            while (self._qwen_owner is not None or self._ollama_maintenance) and not self._stop_requested:
                self._condition.wait(timeout=0.5)
            if self._stop_requested:
                raise JobCancelled("The job manager is stopping.")
            self._qwen_owner = "agent"
            if config is not None:
                self._last_agent_config = config
                self._set_state("agent_last_config", json.dumps(asdict(config)))
                self._set_state("agent_ollama_session", json.dumps(asdict(config)))
        try:
            yield
        finally:
            if config is not None and self.is_auto_unload_enabled():
                self._remember_cleanup(
                    self._cleanup_unused_configured_models(config, [config.llm_model])
                )
            if config is not None:
                self._delete_state("agent_ollama_session")
            with self._condition:
                if self._qwen_owner == "agent":
                    self._qwen_owner = None
                self._condition.notify_all()

    @contextmanager
    def lecture_qwen_slot(self, job_id: str) -> Iterator[None]:
        """Pause lecture Qwen work while the interactive agent remains active."""
        waiting_marked = False
        with self._condition:
            while (
                self._agent_active or self._qwen_owner is not None or self._ollama_maintenance
            ) and not self._stop_requested:
                self._raise_if_cancelled(job_id)
                if not waiting_marked:
                    self._update_job(
                        job_id,
                        status="waiting",
                        stage="notes",
                        message="Paused before the next Qwen call while the local agent is active",
                    )
                    waiting_marked = True
                self._condition.wait(timeout=0.5)
            self._raise_if_cancelled(job_id)
            if self._stop_requested:
                raise JobCancelled("The job manager is stopping.")
            self._qwen_owner = f"lecture:{job_id}"
            if waiting_marked:
                self._update_job(
                    job_id,
                    status="running",
                    stage="notes",
                    message="Local agent released Qwen; lecture generation resumed",
                )
        try:
            yield
        finally:
            with self._condition:
                if self._qwen_owner == f"lecture:{job_id}":
                    self._qwen_owner = None
                self._condition.notify_all()

    def _load_state_flag(self, key: str, default: bool) -> bool:
        with self._database_lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM scheduler_state WHERE key = ?",
                (key,),
            ).fetchone()
        return default if row is None else str(row["value"]) == "1"

    def _load_config_state(self, key: str) -> PipelineConfig | None:
        with self._database_lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM scheduler_state WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            return PipelineConfig(**json.loads(str(row["value"])))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def _set_state(self, key: str, value: str) -> None:
        with self._database_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO scheduler_state(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def _delete_state(self, key: str) -> None:
        with self._database_lock, self._connect() as connection:
            connection.execute("DELETE FROM scheduler_state WHERE key = ?", (key,))

    def _worker_loop(self) -> None:
        while not self._stop_requested:
            with self._condition:
                while self._ollama_maintenance and not self._stop_requested:
                    self._condition.wait(timeout=0.5)
                if self._stop_requested:
                    return
                job = self._claim_next_job()
            if job is None:
                with self._condition:
                    self._condition.wait(timeout=1.0)
                continue
            try:
                self._execute_job(job)
            except JobCancelled as exc:
                self._finish_job(job.id, "cancelled", str(exc), error="")
            except Exception as exc:
                self._finish_job(job.id, "failed", "Task failed", error=str(exc))
            finally:
                self._cleanup_after_job(job)

    def _claim_next_job(self) -> JobRecord | None:
        with self._database_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY priority ASC, created_at ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            now = self._timestamp()
            connection.execute(
                """
                UPDATE jobs SET status = 'running', stage = 'starting', progress = 1,
                                message = 'Starting task', started_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, row["id"]),
            )
        return self.get_job(str(row["id"]))

    def _execute_job(self, job: JobRecord) -> None:
        if job.kind != "lecture":
            raise JobError(f"Unsupported job type: {job.kind}")
        payload = job.payload
        config = PipelineConfig(**payload["config"])
        pipeline = LecturePipeline(
            config,
            self.project_root,
            qwen_guard=lambda: self.lecture_qwen_slot(job.id),
        )

        def on_stage(stage: str, message: str) -> None:
            self._raise_if_cancelled(job.id)
            self._update_job(
                job.id,
                status="running",
                stage=stage,
                progress=self._stage_progress(stage, message),
                message=message,
            )

        resume_directory = payload.get("resume_run_directory")
        if resume_directory:
            result = pipeline.resume(
                resume_directory,
                payload.get("lecture_title"),
                on_stage=on_stage,
            )
        else:
            result = pipeline.run(
                payload.get("audio_path"),
                payload.get("presentation_path"),
                payload.get("lecture_title"),
                on_stage=on_stage,
            )
        self._raise_if_cancelled(job.id)

        library_messages: list[str] = []
        if payload.get("subject_id"):
            library = LibraryStore(self.project_root / "library")
            library_messages = save_lecture_result(
                library,
                payload["subject_id"],
                result,
                payload.get("lecture_title"),
            )
        result_payload = {
            "run_id": result.run_id,
            "run_dir": str(result.run_dir),
            "markdown_path": str(result.markdown_path),
            "pdf_path": str(result.pdf_path),
            "transcript_path": str(result.transcript_path),
            "slides_path": str(result.slides_path),
            "alignment_path": str(result.alignment_path),
            "quality_report_path": str(result.quality_report_path),
            "indexed_chunks": result.indexed_chunks,
            "library_messages": library_messages,
        }
        now = self._timestamp()
        with self._database_lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = 'completed', stage = 'complete', progress = 100,
                                message = 'Lecture notes are ready', result_json = ?,
                                updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (json.dumps(result_payload), now, now, job.id),
            )

    def _raise_if_cancelled(self, job_id: str) -> None:
        with self._database_lock, self._connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row and bool(row["cancel_requested"]):
            raise JobCancelled("Cancelled by the user.")

    def _update_job(self, job_id: str, **values: Any) -> None:
        allowed = {"status", "stage", "progress", "message"}
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            return
        updates["updated_at"] = self._timestamp()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._database_lock, self._connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ?",
                (*updates.values(), job_id),
            )

    def _finish_job(self, job_id: str, status: str, message: str, error: str) -> None:
        now = self._timestamp()
        with self._database_lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = ?, message = ?, error = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (status, message, error[:4000], now, now, job_id),
            )

    def _cleanup_after_job(self, job: JobRecord) -> None:
        """Release this job's Ollama models after every terminal outcome."""
        if not self.is_auto_unload_enabled():
            return
        try:
            config = PipelineConfig(**job.payload["config"])
        except (KeyError, TypeError, ValueError):
            return

        with self._condition:
            while self._qwen_owner is not None or self._ollama_maintenance:
                self._condition.wait(timeout=0.5)
            self._ollama_maintenance = True
        try:
            report = self._cleanup_unused_configured_models(
                config,
                [config.llm_model, config.embedding_model],
            )
            self._remember_cleanup(report)
            self._annotate_job_cleanup(job.id, report)
        finally:
            with self._condition:
                self._ollama_maintenance = False
                self._condition.notify_all()

    def _cleanup_unused_configured_models(
        self,
        config: PipelineConfig,
        models: list[str],
    ) -> OllamaUnloadReport:
        required = self._models_needed_by_pending_work(config.ollama_host, models)
        to_unload = [model for model in models if model not in required]
        report = (
            self._unload_configured_models(config, to_unload)
            if to_unload
            else OllamaUnloadReport()
        )
        report.retained.extend(required)
        return report

    @staticmethod
    def _unload_configured_models(config: PipelineConfig, models: list[str]) -> OllamaUnloadReport:
        runtime: OllamaRuntime | None = None
        try:
            runtime = OllamaRuntime(config.ollama_host, timeout=3.0)
            return runtime.unload_if_running(models)
        except OllamaRuntimeError as exc:
            return OllamaUnloadReport(failures={model: str(exc) for model in models})
        finally:
            JobManager._close_runtime(runtime)

    def _annotate_job_cleanup(self, job_id: str, report: OllamaUnloadReport) -> None:
        try:
            job = self.get_job(job_id)
        except JobError:
            return
        if report.failures:
            suffix = "Ollama cleanup could not be confirmed"
        elif report.retained and report.stopped:
            suffix = "Unused Ollama models unloaded; shared models kept warm"
        elif report.retained:
            suffix = "Ollama models kept warm for upcoming tasks"
        elif report.stopped:
            suffix = "Ollama models unloaded"
        else:
            suffix = "No Ollama models were loaded"
        base = job.message.rstrip(" .")
        self._update_job(job_id, message=f"{base} · {suffix}")

    def _recover_interrupted_ollama(self, configs: list[PipelineConfig]) -> None:
        """Clean model memory left by work interrupted before its finally block."""
        grouped: dict[str, list[str]] = {}
        for config in configs:
            grouped.setdefault(config.ollama_host, []).extend([config.llm_model, config.embedding_model])
        reports: list[OllamaUnloadReport] = []
        for host, models in grouped.items():
            deduplicated = list(dict.fromkeys(models))
            required = self._models_needed_by_pending_work(host, deduplicated)
            to_unload = [model for model in deduplicated if model not in required]
            if not to_unload:
                reports.append(OllamaUnloadReport(retained=required))
                continue
            runtime: OllamaRuntime | None = None
            try:
                runtime = OllamaRuntime(host, timeout=2.0)
                report = runtime.unload_if_running(to_unload)
                report.retained.extend(required)
                reports.append(report)
            except OllamaRuntimeError as exc:
                reports.append(
                    OllamaUnloadReport(
                        retained=required,
                        failures={model: str(exc) for model in to_unload},
                    )
                )
            finally:
                self._close_runtime(runtime)
        if reports:
            combined = OllamaUnloadReport(
                stopped=[model for report in reports for model in report.stopped],
                retained=[model for report in reports for model in report.retained],
                failures={model: error for report in reports for model, error in report.failures.items()},
            )
            self._remember_cleanup(combined, prefix="Restart recovery")

    def _remember_cleanup(self, report: OllamaUnloadReport, prefix: str = "Model cleanup") -> None:
        parts: list[str] = []
        if report.stopped:
            parts.append("unloaded " + ", ".join(report.stopped))
        if report.retained:
            parts.append("kept warm for upcoming work: " + ", ".join(report.retained))
        if report.failures:
            parts.append("could not unload " + ", ".join(report.failures))
        if not parts:
            parts.append("no loaded models found")
        with self._condition:
            self._last_ollama_cleanup = f"{prefix}: {'; '.join(parts)}."

    def _models_needed_by_pending_work(self, host: str, models: list[str]) -> list[str]:
        requirements = self._pending_model_requirements()
        return [
            model
            for model in dict.fromkeys(models)
            if any(
                self._same_ollama_host(host, required_host)
                and model_names_match(model, required_model)
                for required_host, required_model in requirements
            )
        ]

    def _pending_model_requirements(self) -> list[tuple[str, str]]:
        requirements: list[tuple[str, str]] = []
        with self._database_lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM jobs WHERE status IN ('queued', 'running', 'waiting')"
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
                config = PipelineConfig(**payload["config"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            requirements.extend(
                [
                    (config.ollama_host, config.llm_model),
                    (config.ollama_host, config.embedding_model),
                ]
            )
        with self._condition:
            if self._agent_active and self._last_agent_config is not None:
                requirements.append(
                    (self._last_agent_config.ollama_host, self._last_agent_config.llm_model)
                )
        return requirements

    @staticmethod
    def _same_ollama_host(left: str, right: str) -> bool:
        def key(value: str) -> tuple[str, str, int]:
            parsed = urlparse(value)
            hostname = (parsed.hostname or "").lower()
            if hostname in {"localhost", "127.0.0.1", "::1"}:
                hostname = "loopback"
            port = parsed.port or (443 if parsed.scheme == "https" else 11_434)
            return parsed.scheme.lower(), hostname, port

        return key(left) == key(right)

    def _has_active_jobs(self) -> bool:
        with self._database_lock, self._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('running', 'waiting')"
            ).fetchone()[0]
        return bool(count)

    @staticmethod
    def _close_runtime(runtime: Any | None) -> None:
        close = getattr(runtime, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    @staticmethod
    def _validate_ollama_host(host: str) -> None:
        try:
            PipelineConfig(ollama_host=host).validate()
        except ValueError as exc:
            raise JobError(str(exc)) from exc

    @staticmethod
    def _stage_progress(stage: str, message: str) -> int:
        base = {
            "preflight": 2,
            "slides": 64,
            "alignment": 70,
            "quality": 76,
            "rag": 82,
            "notes": 84,
            "export": 98,
            "complete": 100,
        }
        if stage == "recording":
            match = re.search(r"(\d+)/(\d+) chunks complete", message)
            if match:
                return min(60, 5 + round(55 * int(match.group(1)) / max(1, int(match.group(2)))))
            return 5
        if stage == "notes":
            match = re.search(r"\((\d+)/(\d+)\)", message)
            if match:
                return min(96, 84 + round(12 * int(match.group(1)) / max(1, int(match.group(2)))))
        return base.get(stage, 1)

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=str(row["id"]),
            kind=str(row["kind"]),
            title=str(row["title"]),
            status=str(row["status"]),
            priority=int(row["priority"]),
            stage=str(row["stage"]),
            progress=int(row["progress"]),
            message=str(row["message"]),
            payload=json.loads(str(row["payload_json"])),
            result=json.loads(str(row["result_json"])),
            error=str(row["error"]),
            cancel_requested=bool(row["cancel_requested"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            started_at=str(row["started_at"]),
            completed_at=str(row["completed_at"]),
        )

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()
