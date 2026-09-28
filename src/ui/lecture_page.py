"""Lecture job submission page."""

from __future__ import annotations

import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import streamlit as st

from ..config import PipelineConfig
from ..jobs import JobError, JobManager
from ..library import LibraryStore
from ..utils import safe_filename
from .common import subject_lookup


def _save_temporary_upload(upload: Any, directory: Path) -> Path:
    destination = directory / safe_filename(upload.name)
    destination.write_bytes(upload.getbuffer())
    return destination


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
        help=(
            "A title is used exactly for the task and its destination library folder. "
            "Leave it blank to generate a meaningful name from the source files."
        ),
    )
    st.subheader("2. Destination")
    subjects = library.list_subjects()
    lookup = subject_lookup(subjects)
    save_subject = st.selectbox(
        "Save sources, transcript, Markdown notes, and PDF notes to",
        ["none"] + [subject.id for subject in subjects],
        format_func=lambda value: "Do not add to library" if value == "none" else lookup[value].name,
        disabled=not subjects,
        help="Selecting a subject creates the named lecture folder as soon as the task is queued.",
    )
    if not subjects:
        st.info("Create a subject in the Library workspace to save completed lecture files automatically.")
    st.subheader("3. Add to queue")
    if st.button("Queue Lecture Task", type="primary", width="stretch"):
        missing_upload = input_mode == "Upload files" and not (audio_upload or slides_upload)
        missing_path = input_mode == "Use local file paths" and not (recording_local.strip() or slides_local.strip())
        if missing_upload or missing_path:
            st.error("Provide a recording or a PDF/PPT/PPTX deck.")
        else:
            try:
                with ExitStack() as stack:
                    if input_mode == "Upload files":
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
                    )
                st.session_state["job_log_id"] = job.id
                st.session_state["job_log_notice"] = f"Queued '{job.title}'."
                st.session_state["requested_workspace"] = "Job Queue"
                st.rerun()
            except (JobError, ValueError) as exc:
                st.error(str(exc))
