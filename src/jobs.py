"""Persistent priority queue and resource coordinator for background work."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
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

from .agent import LectureAgent
from .config import PipelineConfig
from .embeddings import OllamaEmbedder
from .exporter import ExportError, export_markdown, export_pdf
from .lecture_library import save_lecture_result
from .lecture_naming import infer_lecture_identity
from .library import DuplicateDocumentError, LibraryError, LibraryStore
from .ollama_runtime import (
    OllamaRuntime,
    OllamaRuntimeError,
    OllamaUnloadReport,
    RunningOllamaModel,
    model_names_match,
)
from .pipeline import LecturePipeline
from .rag import LocalRAG
from .task_control import TaskControlSignal
from .translator import MarkdownTranslator, TranslationError
from .utils import safe_filename


PRIORITIES = {"High": 10, "Normal": 50, "Low": 90}
PRIORITY_LABELS = {value: label for label, value in PRIORITIES.items()}
FINAL_STATUSES = {"completed", "failed", "cancelled"}


class JobError(RuntimeError):
    """Raised when a background job cannot be queued or managed."""


class JobCancelled(TaskControlSignal):
    """Raised cooperatively when the user cancels a running job."""


class JobDeferred(TaskControlSignal):
    """Raised cooperatively when work should stop safely and continue later."""


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
    defer_requested: bool
    created_at: str
    updated_at: str
    started_at: str
    completed_at: str

    @property
    def priority_label(self) -> str:
        return PRIORITY_LABELS.get(self.priority, str(self.priority))


@dataclass(frozen=True)
class JobEvent:
    """One durable, timestamped update in a job's processing history."""

    id: int
    job_id: str
    created_at: str
    level: str
    stage: str
    progress: int
    message: str
    data: dict[str, Any]


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
                    defer_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    completed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_queue
                ON jobs(status, priority, created_at);

                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL DEFAULT 'info',
                    stage TEXT NOT NULL DEFAULT '',
                    progress INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL,
                    data_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_job_events_job
                ON job_events(job_id, id);

                CREATE TABLE IF NOT EXISTS scheduler_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            job_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "defer_requested" not in job_columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN defer_requested INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                INSERT INTO job_events(job_id, created_at, level, stage, progress, message, data_json)
                SELECT jobs.id,
                       jobs.updated_at,
                       CASE jobs.status
                           WHEN 'failed' THEN 'error'
                           WHEN 'cancelled' THEN 'warning'
                           ELSE 'info'
                       END,
                       jobs.stage,
                       jobs.progress,
                       CASE WHEN jobs.message = '' THEN 'Existing task added to the processing log'
                            ELSE jobs.message END,
                       '{}'
                FROM jobs
                WHERE NOT EXISTS (
                    SELECT 1 FROM job_events WHERE job_events.job_id = jobs.id
                )
                """
            )
            interrupted_rows = connection.execute(
                """
                SELECT id, payload_json, stage, progress, defer_requested
                FROM jobs WHERE status IN ('running', 'waiting')
                """
            ).fetchall()
            interrupted_payloads.extend(str(row["payload_json"]) for row in interrupted_rows)
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
                SET status = 'deferred', defer_requested = 0,
                    message = CASE
                        WHEN defer_requested = 1
                            THEN 'Stopped safely for later after application restart'
                        ELSE 'Application restarted; task moved to Later with saved checkpoints'
                    END,
                    error = '', updated_at = ?, completed_at = ''
                WHERE status IN ('running', 'waiting')
                """,
                (now,),
            )
            for row in interrupted_rows:
                was_deferred = bool(row["defer_requested"])
                self._append_event(
                    str(row["id"]),
                    "Task stopped safely for later during application restart"
                    if was_deferred
                    else "Application restarted; task moved to Later for checkpoint recovery",
                    level="warning",
                    stage=str(row["stage"]),
                    progress=int(row["progress"]),
                    data={"status": "deferred", "restart_recovery": True},
                    connection=connection,
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
        existing_lectures = sum(1 for item in self.list_jobs(limit=500) if item.kind == "lecture")
        identity = infer_lecture_identity(
            lecture_title,
            stored_audio,
            stored_presentation,
            default_number=existing_lectures + 1,
        )
        title = identity.display_title
        payload = {
            "config": asdict(config),
            "audio_path": str(stored_audio) if stored_audio else None,
            "presentation_path": str(stored_presentation) if stored_presentation else None,
            "lecture_title": identity.display_title,
            "lecture_name": identity.base_name,
            "source_lecture_title": lecture_title,
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
                self._append_event(
                    job_id,
                    f"Task queued with {PRIORITY_LABELS[priority]} priority",
                    stage="planned",
                    progress=0,
                    data={"priority": PRIORITY_LABELS[priority]},
                    connection=connection,
                )
            self._condition.notify_all()
        return self.get_job(job_id)

    def enqueue_translation(
        self,
        source_job_id: str,
        target_language: str,
        priority: int = PRIORITIES["Normal"],
    ) -> JobRecord:
        """Queue an explicit translation of already-completed English notes."""
        if priority not in PRIORITY_LABELS:
            raise JobError("Priority must be High, Normal, or Low.")
        try:
            target_language = MarkdownTranslator.validate_target_language(target_language)
        except TranslationError as exc:
            raise JobError(str(exc)) from exc
        source_job = self.get_job(source_job_id)
        if source_job.kind != "lecture" or source_job.status != "completed":
            raise JobError("Only completed lecture notes can be translated.")
        source_path = Path(str(source_job.result.get("markdown_path", ""))).expanduser().resolve()
        if not self._is_relative_to(source_path, self.project_root) or not source_path.is_file():
            raise JobError("The completed English Markdown notes could not be found safely.")
        try:
            config = PipelineConfig(**source_job.payload["config"])
            config.validate()
        except (KeyError, TypeError, ValueError) as exc:
            raise JobError(f"The source lecture model configuration is invalid: {exc}") from exc

        job_id = uuid4().hex
        (self.root / job_id).mkdir(parents=True, exist_ok=False)
        payload = {
            "config": asdict(config),
            "source_job_id": source_job.id,
            "source_markdown_path": str(source_path),
            "source_language": "English",
            "target_language": target_language,
        }
        title = f"Translate {source_job.title} → {target_language}"
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
                    ) VALUES (?, 'translation', ?, 'queued', ?, 'planned', 0, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        title,
                        priority,
                        "Waiting for its turn; no translation has been generated yet",
                        json.dumps(payload),
                        now,
                        now,
                    ),
                )
                self._append_event(
                    job_id,
                    f"On-demand {target_language} translation queued with {PRIORITY_LABELS[priority]} priority",
                    stage="planned",
                    progress=0,
                    data={
                        "priority": PRIORITY_LABELS[priority],
                        "source_job_id": source_job.id,
                        "target_language": target_language,
                    },
                    connection=connection,
                )
            self._condition.notify_all()
        return self.get_job(job_id)

    def enqueue_slide_review(
        self,
        source_job_id: str,
        slide_number: int,
        priority: int = PRIORITIES["Normal"],
        library_subject_id: str | None = None,
        library_folder_id: str | None = None,
        lecture_name: str | None = None,
    ) -> JobRecord:
        """Queue an expensive, source-grounded deep review for one completed slide."""
        if priority not in PRIORITY_LABELS:
            raise JobError("Priority must be High, Normal, or Low.")
        source_job = self.get_job(source_job_id)
        if source_job.kind != "lecture" or source_job.status != "completed":
            raise JobError("A slide can be deep-reviewed only after its lecture notes are completed.")
        run_dir = Path(str(source_job.result.get("run_dir", ""))).expanduser().resolve()
        allowed_roots = [self.project_root]
        resume_value = str(source_job.payload.get("resume_run_directory", "") or "").strip()
        if resume_value:
            allowed_roots.append(Path(resume_value).expanduser().resolve())
        if not any(self._is_relative_to(run_dir, root) for root in allowed_roots) or not run_dir.is_dir():
            raise JobError("The source lecture run folder could not be found safely.")
        try:
            slide_payload = json.loads((run_dir / "slides.json").read_text(encoding="utf-8"))
            slides = slide_payload["slides"]
            selected = next(item for item in slides if int(item["slide"]) == int(slide_number))
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError, StopIteration) as exc:
            raise JobError(f"Slide {slide_number} is not available in the completed lecture run.") from exc

        try:
            source_config = PipelineConfig(**source_job.payload["config"])
            deep_config = replace(
                source_config,
                note_generation_profile="deep",
                ollama_thinking="high",
                quality_review=True,
                note_max_output_tokens=max(4_096, source_config.note_max_output_tokens),
            )
            deep_config.validate()
        except (KeyError, TypeError, ValueError) as exc:
            raise JobError(f"The source lecture model configuration is invalid: {exc}") from exc

        number = int(selected["slide"])
        slide_title = str(selected.get("title", f"Slide {number}")).strip() or f"Slide {number}"
        if library_subject_id:
            library = LibraryStore(self.project_root / "library")
            try:
                library.get_subject(library_subject_id)
                if library_folder_id and library.get_folder(library_folder_id).subject_id != library_subject_id:
                    raise JobError("The selected lecture folder does not belong to the library subject.")
            except LibraryError as exc:
                raise JobError(f"The selected library destination is unavailable: {exc}") from exc
        job_id = uuid4().hex
        (self.root / job_id).mkdir(parents=True, exist_ok=False)
        payload = {
            "config": asdict(deep_config),
            "source_job_id": source_job.id,
            "run_dir": str(run_dir),
            "slide_number": number,
            "slide_title": slide_title,
            "library_subject_id": library_subject_id,
            "library_folder_id": library_folder_id,
            "lecture_name": lecture_name,
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
                    ) VALUES (?, 'slide_review', ?, 'queued', ?, 'planned', 0, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        f"Deep review · slide {number}: {slide_title}",
                        priority,
                        "Waiting for its turn; baseline lecture notes remain unchanged",
                        json.dumps(payload),
                        now,
                        now,
                    ),
                )
                self._append_event(
                    job_id,
                    f"Deep review for slide {number} queued with {PRIORITY_LABELS[priority]} priority",
                    stage="planned",
                    progress=0,
                    data={"source_job_id": source_job.id, "slide_number": number},
                    connection=connection,
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
                        WHEN 'deferred' THEN 3
                        ELSE 4
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

    def list_job_events(self, job_id: str, limit: int | None = None) -> list[JobEvent]:
        """Return durable events in chronological order, without truncation by default."""
        with self._database_lock, self._connect() as connection:
            if limit is None:
                rows = connection.execute(
                    "SELECT * FROM job_events WHERE job_id = ? ORDER BY id ASC",
                    (job_id,),
                ).fetchall()
            else:
                bounded_limit = max(1, min(limit, 10_000))
                rows = connection.execute(
                    """
                    SELECT * FROM (
                        SELECT * FROM job_events WHERE job_id = ? ORDER BY id DESC LIMIT ?
                    ) ORDER BY id ASC
                    """,
                    (job_id, bounded_limit),
                ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def get_transcript_path(self, job_id: str) -> Path | None:
        """Find a completed or partial transcript belonging to a lecture job."""
        job = self.get_job(job_id)
        run_value = str(job.result.get("run_dir", "")).strip()
        candidates = [str(job.result.get("transcript_path", "")).strip()]
        if run_value:
            candidates.append(str(Path(run_value) / "transcript.json"))
        candidates.append(str(job.result.get("cleaned_partial_transcript_path", "")).strip())
        if run_value:
            candidates.append(str(Path(run_value) / "transcript.cleanup.partial.json"))
        candidates.append(str(job.result.get("partial_transcript_path", "")).strip())
        if run_value:
            candidates.append(str(Path(run_value) / "transcript.partial.json"))

        allowed_roots = [self.project_root]
        resume_value = str(job.payload.get("resume_run_directory", "") or "").strip()
        if resume_value:
            allowed_roots.append(Path(resume_value).expanduser().resolve())
        for raw_candidate in candidates:
            if not raw_candidate:
                continue
            candidate = Path(raw_candidate).expanduser().resolve()
            if not any(self._is_relative_to(candidate, root) for root in allowed_roots):
                continue
            if candidate.is_file():
                return candidate
        return None

    def get_raw_transcript_path(self, job_id: str) -> Path | None:
        """Find the untouched Whisper transcript retained for audit and comparison."""
        job = self.get_job(job_id)
        run_value = str(job.result.get("run_dir", "")).strip()
        candidates = [str(job.result.get("raw_transcript_path", "")).strip()]
        if run_value:
            candidates.append(str(Path(run_value) / "transcript.raw.json"))
        allowed_roots = [self.project_root]
        resume_value = str(job.payload.get("resume_run_directory", "") or "").strip()
        if resume_value:
            allowed_roots.append(Path(resume_value).expanduser().resolve())
        for raw_candidate in candidates:
            if not raw_candidate:
                continue
            candidate = Path(raw_candidate).expanduser().resolve()
            if any(self._is_relative_to(candidate, root) for root in allowed_roots) and candidate.is_file():
                return candidate
        return None

    def update_priority(self, job_id: str, priority: int) -> JobRecord:
        if priority not in PRIORITY_LABELS:
            raise JobError("Priority must be High, Normal, or Low.")
        with self._database_lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET priority = ?, updated_at = ? WHERE id = ? AND status = 'queued'",
                (priority, self._timestamp(), job_id),
            )
            if cursor.rowcount == 1:
                self._append_event(
                    job_id,
                    f"Priority changed to {PRIORITY_LABELS[priority]}",
                    stage="planned",
                    progress=0,
                    data={"priority": PRIORITY_LABELS[priority]},
                    connection=connection,
                )
        if cursor.rowcount != 1:
            raise JobError("Only planned jobs can have their priority changed.")
        with self._condition:
            self._condition.notify_all()
        return self.get_job(job_id)

    def cancel_job(self, job_id: str) -> JobRecord:
        now = self._timestamp()
        changed = False
        with self._database_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, stage, progress FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobError("The selected job no longer exists.")
            status = str(row["status"])
            if status in {"queued", "deferred"}:
                cancel_message = (
                    "Cancelled without further processing"
                    if status == "deferred"
                    else "Cancelled before start"
                )
                connection.execute(
                    """
                    UPDATE jobs SET status = 'cancelled', message = ?,
                                    cancel_requested = 1, defer_requested = 0,
                                    updated_at = ?, completed_at = ?
                    WHERE id = ?
                    """,
                    (cancel_message, now, now, job_id),
                )
                self._append_event(
                    job_id,
                    cancel_message,
                    level="warning",
                    stage=str(row["stage"]),
                    progress=int(row["progress"]),
                    connection=connection,
                )
                changed = True
            elif status not in FINAL_STATUSES:
                connection.execute(
                    """
                    UPDATE jobs
                    SET cancel_requested = 1, message = 'Cancellation requested', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, job_id),
                )
                self._append_event(
                    job_id,
                    "Cancellation requested; the current safe step will finish first",
                    level="warning",
                    stage=str(row["stage"]),
                    progress=int(row["progress"]),
                    connection=connection,
                )
                changed = True
        if changed:
            with self._condition:
                self._condition.notify_all()
        return self.get_job(job_id)

    def defer_job(self, job_id: str) -> JobRecord:
        """Stop a planned or active task safely and retain it for future work."""
        now = self._timestamp()
        with self._database_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, stage, progress FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobError("The selected job no longer exists.")
            status = str(row["status"])
            if status in FINAL_STATUSES:
                raise JobError("Completed, failed, or cancelled tasks cannot be moved to later.")
            event_message = ""
            if status == "queued":
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'deferred', message = 'Saved for future processing',
                        defer_requested = 0, updated_at = ?, completed_at = ''
                    WHERE id = ?
                    """,
                    (now, job_id),
                )
                event_message = "Planned task moved to Later"
            elif status != "deferred":
                connection.execute(
                    """
                    UPDATE jobs
                    SET defer_requested = 1,
                        message = 'Safe stop requested; finishing the current checkpoint',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, job_id),
                )
                event_message = "Safe stop requested; current checkpoint will finish before deferring"
            if event_message:
                self._append_event(
                    job_id,
                    event_message,
                    level="warning",
                    stage=str(row["stage"]),
                    progress=int(row["progress"]),
                    data={"requested_status": "deferred"},
                    connection=connection,
                )
        with self._condition:
            self._condition.notify_all()
        return self.get_job(job_id)

    def resume_job(self, job_id: str, priority: int | None = None) -> JobRecord:
        """Return a deferred task to the priority queue using its saved checkpoints."""
        if priority is not None and priority not in PRIORITY_LABELS:
            raise JobError("Priority must be High, Normal, or Low.")
        with self._condition:
            while self._ollama_maintenance:
                self._condition.wait(timeout=0.5)
            with self._database_lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT status, priority FROM jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise JobError("The selected job no longer exists.")
                if str(row["status"]) != "deferred":
                    raise JobError("Only tasks in Later can be resumed.")
                selected_priority = int(row["priority"]) if priority is None else priority
                now = self._timestamp()
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'queued', priority = ?, stage = 'planned', progress = 0,
                        message = 'Queued to continue from saved checkpoints',
                        error = '', cancel_requested = 0, defer_requested = 0,
                        started_at = '', completed_at = '', updated_at = ?
                    WHERE id = ?
                    """,
                    (selected_priority, now, job_id),
                )
                self._append_event(
                    job_id,
                    f"Task returned to the queue with {PRIORITY_LABELS[selected_priority]} priority",
                    stage="planned",
                    progress=0,
                    data={"priority": PRIORITY_LABELS[selected_priority], "resuming": True},
                    connection=connection,
                )
            self._condition.notify_all()
        return self.get_job(job_id)

    def counts(self) -> dict[str, int]:
        values = {
            "queued": 0,
            "running": 0,
            "waiting": 0,
            "deferred": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
        }
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
        """Pause queued Qwen work while the interactive agent remains active."""
        waiting_marked = False
        current_stage = self.get_job(job_id).stage
        qwen_stage = current_stage if current_stage in {"cleanup", "notes", "translation"} else "notes"
        activity = {
            "cleanup": "transcript cleanup",
            "notes": "lecture summarization",
            "translation": "on-demand translation",
        }[qwen_stage]
        with self._condition:
            while (
                self._agent_active or self._qwen_owner is not None or self._ollama_maintenance
            ) and not self._stop_requested:
                self._raise_if_interrupted(job_id)
                if not waiting_marked:
                    self._update_job(
                        job_id,
                        status="waiting",
                        stage=qwen_stage,
                        message=f"Paused {activity} before the next Qwen call while the local agent is active",
                    )
                    waiting_marked = True
                self._condition.wait(timeout=0.5)
            self._raise_if_interrupted(job_id)
            self._qwen_owner = f"lecture:{job_id}"
            if waiting_marked:
                self._update_job(
                    job_id,
                    status="running",
                    stage=qwen_stage,
                    message=f"Local agent released Qwen; {activity} resumed",
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
            except JobDeferred as exc:
                self._mark_job_deferred(job.id, str(exc))
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
            self._append_event(
                str(row["id"]),
                "Worker claimed the task and started processing",
                stage="starting",
                progress=1,
                connection=connection,
            )
        return self.get_job(str(row["id"]))

    def _execute_job(self, job: JobRecord) -> None:
        if job.kind == "lecture":
            self._execute_lecture_job(job)
            return
        if job.kind == "slide_review":
            self._execute_slide_review_job(job)
            return
        if job.kind == "translation":
            self._execute_translation_job(job)
            return
        raise JobError(f"Unsupported job type: {job.kind}")

    def _execute_lecture_job(self, job: JobRecord) -> None:
        payload = job.payload
        config = PipelineConfig(**payload["config"])
        pipeline = LecturePipeline(
            config,
            self.project_root,
            qwen_guard=lambda: self.lecture_qwen_slot(job.id),
        )
        run_registered = False

        def on_stage(stage: str, message: str) -> None:
            nonlocal run_registered
            run_dir = getattr(pipeline, "current_run_dir", None)
            if run_dir is not None and not run_registered:
                self._merge_job_result(
                    job.id,
                    {
                        "run_dir": str(run_dir),
                        "transcript_path": str(Path(run_dir) / "transcript.json"),
                        "raw_transcript_path": str(Path(run_dir) / "transcript.raw.json"),
                        "transcript_text_path": str(Path(run_dir) / "transcript.txt"),
                        "slide_summaries_path": str(Path(run_dir) / "slide_summaries.json"),
                        "manifest_path": str(Path(run_dir) / "lecture_manifest.json"),
                        "cleaned_partial_transcript_path": str(
                            Path(run_dir) / "transcript.cleanup.partial.json"
                        ),
                        "partial_transcript_path": str(Path(run_dir) / "transcript.partial.json"),
                        "partial_notes_path": str(Path(run_dir) / "lecture_notes.partial.md"),
                        "notes_checkpoint_dir": str(Path(run_dir) / "notes_checkpoints"),
                    },
                )
                run_registered = True
            self._raise_if_interrupted(job.id)
            self._update_job(
                job.id,
                status="running",
                stage=stage,
                progress=self._stage_progress(stage, message),
                message=message,
            )

        resume_directory = payload.get("resume_run_directory")
        if not resume_directory:
            saved_run_directory = str(job.result.get("run_dir", "")).strip()
            if saved_run_directory and Path(saved_run_directory).is_dir():
                resume_directory = saved_run_directory
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
        self._raise_if_interrupted(job.id)

        library_messages: list[str] = []
        if payload.get("subject_id"):
            library = LibraryStore(self.project_root / "library")
            library_messages = save_lecture_result(
                library,
                payload["subject_id"],
                result,
                getattr(result, "lecture_title", None) or payload.get("lecture_title"),
            )
        result_payload = {
            "run_id": result.run_id,
            "run_dir": str(result.run_dir),
            "markdown_path": str(result.markdown_path),
            "pdf_path": str(result.pdf_path),
            "transcript_path": str(result.transcript_path),
            "raw_transcript_path": str(result.raw_transcript_path),
            "transcript_text_path": str(
                getattr(result, "transcript_text_path", result.run_dir / "transcript.txt")
            ),
            "cleaned_partial_transcript_path": str(result.run_dir / "transcript.cleanup.partial.json"),
            "partial_transcript_path": str(result.run_dir / "transcript.partial.json"),
            "slides_path": str(result.slides_path),
            "slide_summaries_path": str(
                getattr(result, "slide_summaries_path", result.run_dir / "slide_summaries.json")
            ),
            "alignment_path": str(result.alignment_path),
            "quality_report_path": str(result.quality_report_path),
            "manifest_path": str(
                getattr(result, "manifest_path", result.run_dir / "lecture_manifest.json")
            ),
            "lecture_name": str(getattr(result, "lecture_name", payload.get("lecture_name", ""))),
            "lecture_title": str(getattr(result, "lecture_title", payload.get("lecture_title", ""))),
            "indexed_chunks": result.indexed_chunks,
            "output_language": "English",
            "library_messages": library_messages,
        }
        now = self._timestamp()
        completed = False
        with self._database_lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'completed', stage = 'complete', progress = 100,
                                message = 'Lecture notes are ready', result_json = ?,
                                defer_requested = 0, updated_at = ?, completed_at = ?
                WHERE id = ? AND cancel_requested = 0 AND defer_requested = 0
                """,
                (json.dumps(result_payload), now, now, job.id),
            )
            completed = cursor.rowcount == 1
            if completed:
                self._append_event(
                    job.id,
                    "Lecture notes and transcript are ready",
                    stage="complete",
                    progress=100,
                    connection=connection,
                )
        if not completed:
            self._raise_if_interrupted(job.id)
            raise JobError("The task could not be finalized because its state changed.")

    def _execute_slide_review_job(self, job: JobRecord) -> None:
        """Use the expensive profile for one selected slide without changing baseline notes."""
        payload = job.payload
        config = PipelineConfig(**payload["config"])
        config.validate()
        source_job = self.get_job(str(payload["source_job_id"]))
        if source_job.kind != "lecture" or source_job.status != "completed":
            raise JobError("The source lecture must remain completed while its slide is reviewed.")
        run_dir = Path(str(payload["run_dir"])).expanduser().resolve()
        allowed_roots = [self.project_root]
        resume_value = str(source_job.payload.get("resume_run_directory", "") or "").strip()
        if resume_value:
            allowed_roots.append(Path(resume_value).expanduser().resolve())
        if not any(self._is_relative_to(run_dir, root) for root in allowed_roots) or not run_dir.is_dir():
            raise JobError("The source lecture run folder is missing or outside the project.")
        try:
            slide_payload = json.loads((run_dir / "slides.json").read_text(encoding="utf-8"))
            alignment_payload = json.loads((run_dir / "alignment.json").read_text(encoding="utf-8"))
            number = int(payload["slide_number"])
            slide = next(item for item in slide_payload["slides"] if int(item["slide"]) == number)
            aligned = next(
                (item for item in alignment_payload.get("slides", []) if int(item["slide"]) == number),
                {"slide": number, "paragraphs": []},
            )
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError, StopIteration) as exc:
            raise JobError(f"The stored evidence for slide {payload.get('slide_number', '?')} is unavailable.") from exc

        review_dir = run_dir / "slide_reviews"
        checkpoint_path = review_dir / "checkpoints" / f"{job.id}.json"
        markdown_path = review_dir / f"slide_{number:04d}_{job.id[:8]}.deep.md"
        pdf_path: Path | None = markdown_path.with_suffix(".pdf")
        self._merge_job_result(
            job.id,
            {
                "source_job_id": source_job.id,
                "slide_number": number,
                "run_dir": str(run_dir),
                "checkpoint_path": str(checkpoint_path),
                "markdown_path": str(markdown_path),
                "pdf_path": str(pdf_path),
            },
        )
        self._raise_if_interrupted(job.id)
        self._update_job(
            job.id,
            status="running",
            stage="preflight",
            progress=2,
            message=f"Checking local models for the deep review of slide {number}",
        )
        embedder = OllamaEmbedder(config)
        embedder.verify_local_models()
        rag = LocalRAG(run_dir / "database", embedder)
        agent = LectureAgent(
            config,
            rag,
            chat_guard=lambda: self.lecture_qwen_slot(job.id),
        )
        self._raise_if_interrupted(job.id)
        self._update_job(
            job.id,
            status="running",
            stage="notes",
            progress=84,
            message=f"Deep reasoning and factual audit for slide {number} (1/1)",
        )
        body = agent.generate_slide_note(slide, aligned, checkpoint_path=checkpoint_path)
        self._raise_if_interrupted(job.id)

        title = str(slide.get("title", f"Slide {number}")).strip() or f"Slide {number}"
        markdown = "\n".join(
            [
                f"# Deep Review — Slide {number}: {title}",
                "",
                "> Generated on demand from the stored slide and its aligned professor transcript. "
                "The baseline lecture notes are unchanged.",
                "",
                body,
                "",
            ]
        )
        self._update_job(
            job.id,
            status="running",
            stage="export",
            progress=97,
            message=f"Exporting the deep review for slide {number}",
        )
        export_markdown(markdown, markdown_path)
        pdf_warning = ""
        try:
            export_pdf(markdown, pdf_path)
        except ExportError as exc:
            pdf_warning = str(exc)
            pdf_path = None
        self._raise_if_interrupted(job.id)

        library_messages: list[str] = []
        if payload.get("library_subject_id"):
            library = LibraryStore(self.project_root / "library")
            base_name = safe_filename(
                str(payload.get("lecture_name") or source_job.result.get("lecture_name") or "Lecture")
            )
            library_candidates = [(pdf_path, ".pdf")] if pdf_path is not None else []
            for path, suffix in library_candidates:
                filename = f"{base_name}_Slide_{number:03d}_Deep_Review_{job.id[:8]}{suffix}"
                try:
                    stored = library.add_document(
                        str(payload["library_subject_id"]),
                        path,
                        filename=filename,
                        folder_id=str(payload.get("library_folder_id") or "") or None,
                    )
                    library_messages.append(f"Saved {stored.original_name} to the lecture folder.")
                except DuplicateDocumentError as exc:
                    library_messages.append(str(exc))
                except LibraryError as exc:
                    library_messages.append(f"Could not add {filename} to the lecture folder: {exc}")

        result_payload = {
            "source_job_id": source_job.id,
            "slide_number": number,
            "run_dir": str(run_dir),
            "checkpoint_path": str(checkpoint_path),
            "markdown_path": str(markdown_path),
            "pdf_path": str(pdf_path) if pdf_path is not None else "",
            "pdf_warning": pdf_warning,
            "generation_profile": "deep",
            "library_messages": library_messages,
        }
        now = self._timestamp()
        message = f"Deep review for slide {number} is ready"
        if pdf_warning:
            message += " as Markdown; PDF rendering was unavailable"
        with self._database_lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'completed', stage = 'complete', progress = 100,
                                message = ?, result_json = ?, defer_requested = 0,
                                updated_at = ?, completed_at = ?
                WHERE id = ? AND cancel_requested = 0 AND defer_requested = 0
                """,
                (message, json.dumps(result_payload), now, now, job.id),
            )
            if cursor.rowcount == 1:
                self._append_event(
                    job.id,
                    message,
                    stage="complete",
                    progress=100,
                    data={"source_job_id": source_job.id, "slide_number": number},
                    connection=connection,
                )
                return
        self._raise_if_interrupted(job.id)
        raise JobError("The deep slide review could not be finalized because its state changed.")

    def _execute_translation_job(self, job: JobRecord) -> None:
        """Translate completed Markdown only after the user has queued this job."""
        payload = job.payload
        config = PipelineConfig(**payload["config"])
        config.validate()
        target_language = MarkdownTranslator.validate_target_language(str(payload["target_language"]))
        source_path = Path(str(payload["source_markdown_path"])).expanduser().resolve()
        if not self._is_relative_to(source_path, self.project_root) or not source_path.is_file():
            raise JobError("The completed English Markdown notes are missing or outside the project.")
        source_job = self.get_job(str(payload["source_job_id"]))
        if source_job.kind != "lecture" or source_job.status != "completed":
            raise JobError("The source lecture must remain completed before translation can run.")

        language_slug = safe_filename(target_language.casefold().replace(" ", "_"), "translation")
        translation_dir = source_path.parent / "translations"
        markdown_path = translation_dir / f"{source_path.stem}.{language_slug}.md"
        pdf_path: Path | None = markdown_path.with_suffix(".pdf")
        self._merge_job_result(
            job.id,
            {
                "source_job_id": source_job.id,
                "source_markdown_path": str(source_path),
                "target_language": target_language,
                "markdown_path": str(markdown_path),
                "pdf_path": str(pdf_path),
            },
        )
        self._raise_if_interrupted(job.id)
        self._update_job(
            job.id,
            status="running",
            stage="translation",
            progress=5,
            message=f"Translating the finished English notes into {target_language}",
        )

        def translation_progress(index: int, total: int, message: str) -> None:
            self._raise_if_interrupted(job.id)
            self._update_job(
                job.id,
                status="running",
                stage="translation",
                progress=self._stage_progress("translation", f"{message} ({index}/{total})"),
                message=f"{message} ({index}/{total})",
            )

        source_markdown = source_path.read_text(encoding="utf-8")
        translated = MarkdownTranslator(
            config,
            chat_guard=lambda: self.lecture_qwen_slot(job.id),
        ).translate(
            source_markdown,
            target_language,
            markdown_path,
            progress=translation_progress,
        )
        self._raise_if_interrupted(job.id)
        self._update_job(
            job.id,
            status="running",
            stage="export",
            progress=97,
            message=f"Rendering the requested {target_language} translation",
        )
        pdf_warning = ""
        try:
            export_pdf(translated, pdf_path)
        except ExportError as exc:
            pdf_warning = str(exc)
            pdf_path = None
        self._raise_if_interrupted(job.id)

        result_payload = {
            "source_job_id": source_job.id,
            "source_markdown_path": str(source_path),
            "source_language": "English",
            "target_language": target_language,
            "markdown_path": str(markdown_path),
            "pdf_path": str(pdf_path) if pdf_path is not None else "",
            "pdf_warning": pdf_warning,
        }
        now = self._timestamp()
        message = f"{target_language} translation is ready"
        if pdf_warning:
            message += " as Markdown; PDF rendering was unavailable"
        with self._database_lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'completed', stage = 'complete', progress = 100,
                                message = ?, result_json = ?, defer_requested = 0,
                                updated_at = ?, completed_at = ?
                WHERE id = ? AND cancel_requested = 0 AND defer_requested = 0
                """,
                (message, json.dumps(result_payload), now, now, job.id),
            )
            if cursor.rowcount == 1:
                self._append_event(
                    job.id,
                    message,
                    stage="complete",
                    progress=100,
                    data={"target_language": target_language},
                    connection=connection,
                )
                return
        self._raise_if_interrupted(job.id)
        raise JobError("The translation could not be finalized because its state changed.")

    def _raise_if_interrupted(self, job_id: str) -> None:
        with self._database_lock, self._connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested, defer_requested FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row and bool(row["cancel_requested"]):
            raise JobCancelled("Cancelled by the user.")
        if row and bool(row["defer_requested"]):
            raise JobDeferred("Stopped safely and saved for future processing.")
        if self._stop_requested:
            raise JobDeferred("Application stopped; task saved for future processing.")

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
            row = connection.execute(
                "SELECT stage, progress, message FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is not None:
                self._append_event(
                    job_id,
                    str(row["message"]),
                    stage=str(row["stage"]),
                    progress=int(row["progress"]),
                    connection=connection,
                )

    def _merge_job_result(self, job_id: str, values: dict[str, Any]) -> None:
        """Persist artifact locations before a task reaches its terminal state."""
        with self._database_lock, self._connect() as connection:
            row = connection.execute(
                "SELECT result_json FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return
            try:
                result = json.loads(str(row["result_json"]))
            except (TypeError, json.JSONDecodeError):
                result = {}
            result.update(values)
            connection.execute(
                "UPDATE jobs SET result_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(result), self._timestamp(), job_id),
            )

    def _finish_job(self, job_id: str, status: str, message: str, error: str) -> None:
        now = self._timestamp()
        with self._database_lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = ?, message = ?, error = ?, defer_requested = 0,
                                updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (status, message, error[:4000], now, now, job_id),
            )
            row = connection.execute(
                "SELECT stage, progress FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is not None:
                self._append_event(
                    job_id,
                    error or message,
                    level="error" if status == "failed" else "warning" if status == "cancelled" else "info",
                    stage=str(row["stage"]),
                    progress=int(row["progress"]),
                    data={"status": status},
                    connection=connection,
                )

    def _mark_job_deferred(self, job_id: str, message: str) -> None:
        """Finish a cooperative safe-stop without treating it as a failure."""
        now = self._timestamp()
        with self._database_lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'deferred', message = ?, error = '',
                    cancel_requested = 0, defer_requested = 0,
                    updated_at = ?, completed_at = ''
                WHERE id = ?
                """,
                (message, now, job_id),
            )
            row = connection.execute(
                "SELECT stage, progress FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is not None:
                self._append_event(
                    job_id,
                    message,
                    level="warning",
                    stage=str(row["stage"]),
                    progress=int(row["progress"]),
                    data={"status": "deferred", "checkpoint_preserved": True},
                    connection=connection,
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
            models = [config.llm_model] if job.kind == "translation" else [config.llm_model, config.embedding_model]
            report = self._cleanup_unused_configured_models(config, models)
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
                "SELECT kind, payload_json FROM jobs WHERE status IN ('queued', 'running', 'waiting')"
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
                config = PipelineConfig(**payload["config"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            requirements.append((config.ollama_host, config.llm_model))
            if str(row["kind"]) != "translation":
                requirements.append((config.ollama_host, config.embedding_model))
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

    def _append_event(
        self,
        job_id: str,
        message: str,
        *,
        level: str = "info",
        stage: str = "",
        progress: int = 0,
        data: dict[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Store an event in the caller's transaction or in a short new one."""
        values = (
            job_id,
            self._timestamp(),
            level,
            stage,
            max(0, min(int(progress), 100)),
            message,
            json.dumps(data or {}, ensure_ascii=False),
        )
        statement = """
            INSERT INTO job_events(job_id, created_at, level, stage, progress, message, data_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        if connection is not None:
            connection.execute(statement, values)
            return
        with self._database_lock, self._connect() as event_connection:
            event_connection.execute(statement, values)

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        """Python 3.9-compatible containment check for artifact paths."""
        try:
            path.relative_to(root.resolve())
            return True
        except ValueError:
            return False

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
            "cleanup": 62,
            "slides": 70,
            "alignment": 74,
            "quality": 78,
            "rag": 82,
            "notes": 84,
            "translation": 5,
            "export": 98,
            "complete": 100,
        }
        if stage == "recording":
            match = re.search(r"(\d+)/(\d+) chunks complete", message)
            if match:
                return min(60, 5 + round(55 * int(match.group(1)) / max(1, int(match.group(2)))))
            return 5
        if stage == "cleanup":
            match = re.search(r"\((\d+)/(\d+)\)", message)
            if match:
                return min(68, 60 + round(8 * int(match.group(1)) / max(1, int(match.group(2)))))
        if stage == "notes":
            match = re.search(r"\((\d+)/(\d+)\)", message)
            if match:
                return min(96, 84 + round(12 * int(match.group(1)) / max(1, int(match.group(2)))))
        if stage == "translation":
            match = re.search(r"\((\d+)/(\d+)\)", message)
            if match:
                return min(95, 5 + round(90 * int(match.group(1)) / max(1, int(match.group(2)))))
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
            defer_requested=bool(row["defer_requested"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            started_at=str(row["started_at"]),
            completed_at=str(row["completed_at"]),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> JobEvent:
        try:
            data = json.loads(str(row["data_json"]))
        except (TypeError, json.JSONDecodeError):
            data = {}
        return JobEvent(
            id=int(row["id"]),
            job_id=str(row["job_id"]),
            created_at=str(row["created_at"]),
            level=str(row["level"]),
            stage=str(row["stage"]),
            progress=int(row["progress"]),
            message=str(row["message"]),
            data=data,
        )

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()
