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
from uuid import uuid4

from .config import PipelineConfig
from .lecture_library import save_lecture_result
from .library import LibraryStore
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
        self._stop_requested = False
        self._worker: threading.Thread | None = None
        self._initialize_schema()
        self._agent_active = self._load_agent_active()
        if autostart:
            self.start()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize_schema(self) -> None:
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
        with self._condition:
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

    def set_agent_active(self, active: bool) -> None:
        with self._condition:
            self._agent_active = bool(active)
            with self._database_lock, self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO scheduler_state(key, value) VALUES ('agent_active', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    ("1" if active else "0",),
                )
            self._condition.notify_all()

    @contextmanager
    def agent_qwen_slot(self) -> Iterator[None]:
        """Give an interactive agent call exclusive access to the Qwen model."""
        with self._condition:
            while self._qwen_owner is not None and not self._stop_requested:
                self._condition.wait(timeout=0.5)
            if self._stop_requested:
                raise JobCancelled("The job manager is stopping.")
            self._qwen_owner = "agent"
        try:
            yield
        finally:
            with self._condition:
                if self._qwen_owner == "agent":
                    self._qwen_owner = None
                self._condition.notify_all()

    @contextmanager
    def lecture_qwen_slot(self, job_id: str) -> Iterator[None]:
        """Pause lecture Qwen work while the interactive agent remains active."""
        waiting_marked = False
        with self._condition:
            while (self._agent_active or self._qwen_owner is not None) and not self._stop_requested:
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

    def _load_agent_active(self) -> bool:
        with self._database_lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM scheduler_state WHERE key = 'agent_active'"
            ).fetchone()
        return bool(row and row["value"] == "1")

    def _worker_loop(self) -> None:
        while not self._stop_requested:
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
