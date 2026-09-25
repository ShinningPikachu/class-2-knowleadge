"""Lecture job submission page."""

from __future__ import annotations

import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import streamlit as st

from ..config import PipelineConfig
from ..jobs import PRIORITIES, JobError, JobManager
from ..library import LibraryStore
from ..utils import safe_filename
from .common import subject_lookup


def _save_temporary_upload(upload: Any, directory: Path) -> Path:
    destination = directory / safe_filename(upload.name)
    destination.write_bytes(upload.getbuffer())
    return destination


def _render_recent_jobs(manager: JobManager) -> None:
    jobs = [job for job in manager.list_jobs(limit=20) if job.kind == "lecture"][:5]
    if not jobs:
        return
    with st.expander("Recent lecture tasks"):
        for job in jobs:
            details, action = st.columns([4, 1])
            details.markdown(
                f"**{job.title}** — {job.status.title()} · {job.progress}% · "
                f"{job.stage.replace('_', ' ').title()}"
            )
            details.caption(job.message)
            if action.button("Open log", key=f"recent_lecture_log_{job.id}", use_container_width=True):
                st.session_state["job_log_id"] = job.id
                st.session_state["requested_workspace"] = "Job Queue"
                st.rerun()


def render_lecture_processor(
    library: LibraryStore,
    config: PipelineConfig,
    manager: JobManager,
) -> None:
    st.title("🎓 Queue Lecture Notes")
    st.caption(
        "Add a lecture task and continue using the app. Transcription runs in the background; "
        "Qwen generation coordinates with the local agent."
    )

    st.subheader("1. Lecture sources")
    input_mode = st.radio("Input method", ["Upload files", "Use local file paths"], horizontal=True)
    audio_upload = None
    slides_upload = None
    recording_local = ""
    slides_local = ""
    input_left, input_right = st.columns(2)
    if input_mode == "Upload files":
        with input_left:
            audio_upload = st.file_uploader(
                "Lecture recording (optional)",
                type=["mp3", "wav", "m4a", "aac", "flac", "ogg", "opus", "mp4", "mov", "mkv", "webm", "m4v"],
                help="For video, the audio track is transcribed.",
            )
        with input_right:
            slides_upload = st.file_uploader("Lecture slides (optional)", type=["pdf", "ppt", "pptx"])
    else:
        with input_left:
            recording_local = st.text_input("Local audio/video path (optional)", placeholder="/path/to/lecture.mp4")
        with input_right:
            slides_local = st.text_input("Local slide-deck path (optional)", placeholder="/path/to/lecture.pdf")
        st.caption("Local inputs are copied into durable job storage before the task is queued.")

    lecture_title = st.text_input(
        "Lecture title (optional)",
        placeholder="e.g. Lecture 01 — Introduction",
        help="Leave blank to generate a meaningful Lecture_01_Topic name from the source files.",
    )
    with st.expander("Resume an interrupted pipeline run"):
        resume_run_directory = st.text_input(
            "Run folder",
            placeholder="/path/to/class-2-knowleadge/runs/lecture_...",
            help="The background task reuses completed transcription, cleanup, and per-slide note checkpoints.",
        )

    st.subheader("2. Destination and priority")
    subjects = library.list_subjects()
    lookup = subject_lookup(subjects)
    save_subject = st.selectbox(
        "Save sources, transcript, Markdown notes, and PDF notes to",
        ["none"] + [subject.id for subject in subjects],
        format_func=lambda value: "Do not add to library" if value == "none" else lookup[value].name,
        disabled=not subjects,
        help="Create subjects from the Library workspace.",
    )
    if not subjects:
        st.info("Create a subject in the Library workspace to save completed lecture files automatically.")
    priority_label = st.selectbox(
        "Task priority",
        list(PRIORITIES),
        index=list(PRIORITIES).index("Normal"),
        help="High-priority planned tasks are selected before Normal and Low tasks.",
    )

    st.subheader("3. Add to queue")
    if st.button("Queue Lecture Task", type="primary", use_container_width=True):
        missing_upload = input_mode == "Upload files" and not (audio_upload or slides_upload)
        missing_path = input_mode == "Use local file paths" and not (recording_local.strip() or slides_local.strip())
        if not resume_run_directory.strip() and (missing_upload or missing_path):
            st.error("Provide a recording, a PDF/PPT/PPTX deck, or a run folder to resume.")
        else:
            try:
                with ExitStack() as stack:
                    if resume_run_directory.strip():
                        audio_path = None
                        presentation_path = None
                    elif input_mode == "Upload files":
                        temporary_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="lecture_job_")))
                        audio_path = _save_temporary_upload(audio_upload, temporary_dir) if audio_upload else None
                        presentation_path = (
                            _save_temporary_upload(slides_upload, temporary_dir)
                            if slides_upload
                            else None
                        )
                    else:
                        audio_path = Path(recording_local).expanduser().resolve() if recording_local.strip() else None
                        presentation_path = Path(slides_local).expanduser().resolve() if slides_local.strip() else None
                    job = manager.enqueue_lecture(
                        config=config,
                        audio_path=audio_path,
                        presentation_path=presentation_path,
                        lecture_title=lecture_title.strip() or None,
                        subject_id=None if save_subject == "none" else save_subject,
                        priority=PRIORITIES[priority_label],
                        resume_run_directory=resume_run_directory.strip() or None,
                    )
                st.session_state["job_log_id"] = job.id
                st.session_state["job_log_notice"] = (
                    f"Queued '{job.title}' with {job.priority_label} priority."
                )
                st.session_state["requested_workspace"] = "Job Queue"
                st.rerun()
            except (JobError, ValueError) as exc:
                st.error(str(exc))
    _render_recent_jobs(manager)
