"""Queue visibility and controls for background tasks."""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from ..config import PipelineConfig
from ..jobs import FINAL_STATUSES, PRIORITIES, JobError, JobManager, JobRecord
from ..library import LibraryStore
from ..ollama_runtime import OllamaUnloadReport


STATUS_ICONS = {
    "queued": "🗓️",
    "running": "⚙️",
    "waiting": "⏸️",
    "deferred": "💤",
    "completed": "✅",
    "failed": "❌",
    "cancelled": "🚫",
}


def _render_job_actions(
    job: JobRecord,
    manager: JobManager,
    key_prefix: str,
    *,
    compact: bool = False,
) -> None:
    """Render lifecycle controls consistently on cards and the live log screen."""
    if job.status == "queued":
        labels = list(PRIORITIES)
        current_index = labels.index(job.priority_label)
        priority = st.selectbox(
            "Priority",
            labels,
            index=current_index,
            key=f"{key_prefix}_priority_{job.id}",
            label_visibility="collapsed" if compact else "visible",
        )
        action_targets = (st, st, st) if compact else st.columns(3)
        if action_targets[0].button(
            "Apply" if compact else "Update priority",
            key=f"{key_prefix}_apply_priority_{job.id}",
            help="Apply the selected priority" if compact else None,
            width="stretch",
        ):
            try:
                manager.update_priority(job.id, PRIORITIES[priority])
                st.rerun()
            except JobError as exc:
                st.error(str(exc))
        if action_targets[1].button(
            "Later" if compact else "Do later",
            key=f"{key_prefix}_defer_{job.id}",
            help="Move this task to Later" if compact else None,
            width="stretch",
        ):
            try:
                manager.defer_job(job.id)
                st.rerun()
            except JobError as exc:
                st.error(str(exc))
        if action_targets[2].button(
            "Cancel" if compact else "Cancel permanently",
            key=f"{key_prefix}_cancel_{job.id}",
            help="Cancel this task permanently" if compact else None,
            width="stretch",
        ):
            manager.cancel_job(job.id)
            st.rerun()
    elif job.status in {"running", "waiting"}:
        action_targets = (st, st) if compact else st.columns(2)
        if action_targets[0].button(
            "Later" if compact else "Stop safely · do later",
            key=f"{key_prefix}_defer_{job.id}",
            disabled=job.defer_requested or job.cancel_requested,
            help="Finishes the current safe checkpoint, preserves transcript and completed slide notes, then moves the task to Later.",
            width="stretch",
        ):
            try:
                manager.defer_job(job.id)
                st.rerun()
            except JobError as exc:
                st.error(str(exc))
        if action_targets[1].button(
            "Cancel" if compact else "Cancel permanently",
            key=f"{key_prefix}_cancel_{job.id}",
            disabled=job.cancel_requested,
            help="Cancel this task permanently" if compact else None,
            width="stretch",
        ):
            manager.cancel_job(job.id)
            st.rerun()
    elif job.status == "deferred":
        labels = list(PRIORITIES)
        current_index = labels.index(job.priority_label)
        priority = st.selectbox(
            "Priority when resumed",
            labels,
            index=current_index,
            key=f"{key_prefix}_resume_priority_{job.id}",
            label_visibility="collapsed" if compact else "visible",
        )
        action_targets = (st, st) if compact else st.columns(2)
        if action_targets[0].button(
            "Resume" if compact else "Resume from checkpoints",
            key=f"{key_prefix}_resume_{job.id}",
            type="primary",
            help="Resume from the saved checkpoints" if compact else None,
            width="stretch",
        ):
            try:
                manager.resume_job(job.id, PRIORITIES[priority])
                st.rerun()
            except JobError as exc:
                st.error(str(exc))
        if action_targets[1].button(
            "Cancel" if compact else "Cancel permanently",
            key=f"{key_prefix}_cancel_{job.id}",
            help="Cancel this task permanently" if compact else None,
            width="stretch",
        ):
            manager.cancel_job(job.id)
            st.rerun()
    elif job.status == "failed":
        if st.button(
            "Retry" if compact else "Start again with same parameters",
            key=f"{key_prefix}_retry_{job.id}",
            type="primary",
            help="Start again with the same parameters" if compact else None,
            width="stretch",
        ):
            try:
                retry_job = manager.retry_failed_job(job.id)
                st.session_state["job_log_id"] = retry_job.id
                if retry_job.payload.get("resume_run_directory") and job.stage == "cleanup":
                    st.session_state["job_log_notice"] = (
                        "Queued a retry from the saved cleanup checkpoints. "
                        "The failed job and its processing log are unchanged."
                    )
                else:
                    st.session_state["job_log_notice"] = (
                        "Queued a fresh retry with the same saved parameters. "
                        "The failed job and its processing log are unchanged."
                    )
                st.rerun()
            except JobError as exc:
                st.error(str(exc))


def _format_model_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


def _show_unload_result(report: OllamaUnloadReport) -> None:
    if report.stopped:
        st.success("Unloaded: " + ", ".join(report.stopped))
    if report.retained:
        st.info("Kept loaded for upcoming work: " + ", ".join(report.retained))
    if report.failures:
        st.warning(
            "Could not unload: "
            + "; ".join(f"{model} ({error})" for model, error in report.failures.items())
        )


def _render_ollama_controls(manager: JobManager) -> None:
    with st.expander("Ollama model memory", expanded=False):
        st.caption(
            "Unload model weights from memory without stopping the Ollama server. "
            "Automatic cleanup keeps models warm for activated or queued work and "
            "unloads them only after the final consumer finishes."
        )
        configured_auto_unload = manager.is_auto_unload_enabled()
        auto_unload = st.toggle(
            "Automatically unload models after each task",
            value=configured_auto_unload,
            key="auto_unload_ollama_toggle",
        )
        if auto_unload != configured_auto_unload:
            manager.set_auto_unload_enabled(auto_unload)

        host = st.text_input(
            "Ollama URL",
            value=PipelineConfig().ollama_host,
            key="ollama_memory_host",
        ).strip()
        try:
            models = manager.list_loaded_ollama_models(host)
        except JobError as exc:
            models = []
            st.caption(str(exc))

        if not models:
            st.info("No loaded models were reported by this Ollama server.")
        for model in models:
            details = _format_model_size(model.size_bytes)
            if model.vram_bytes:
                details += f" · {_format_model_size(model.vram_bytes)} in VRAM"
            model_column, stop_column = st.columns([3, 1])
            model_column.markdown(f"**{model.name}**")
            model_column.caption(details)
            if stop_column.button("Unload", key=f"unload_ollama_{model.name}", width="stretch"):
                try:
                    _show_unload_result(manager.stop_loaded_ollama_models(host, [model.name]))
                    st.rerun()
                except JobError as exc:
                    st.error(str(exc))

        if len(models) > 1 and st.button("Unload all models", width="stretch"):
            try:
                _show_unload_result(manager.stop_loaded_ollama_models(host))
                st.rerun()
            except JobError as exc:
                st.error(str(exc))

        last_cleanup = manager.last_ollama_cleanup()
        if last_cleanup:
            st.caption(last_cleanup)


def _render_result_preview(job: JobRecord, key_prefix: str) -> None:
    """Preview generated notes without duplicating File Manager downloads."""
    translated = job.kind == "translation"
    slide_review = job.kind == "slide_review"
    if translated:
        target = str(job.result.get("target_language", job.payload.get("target_language", "translation")))
        st.caption(f"Requested translation: English → {target}")
    markdown_path = Path(str(job.result.get("markdown_path", "")))
    if (translated or slide_review) and markdown_path.is_file():
        try:
            result_text = markdown_path.read_text(encoding="utf-8")
            visible_text = result_text[:40_000]
            if len(result_text) > 40_000:
                visible_text += "\n\n… remaining content omitted from preview …"
            st.text_area(
                "Translated notes preview" if translated else "Deep slide review preview",
                value=visible_text,
                height=420,
                disabled=True,
                key=f"{key_prefix}_result_preview_{job.id}",
            )
        except (OSError, UnicodeDecodeError) as exc:
            st.warning(f"The generated notes exist but could not be previewed: {exc}")
    if (translated or slide_review) and job.result.get("pdf_warning"):
        st.warning(str(job.result["pdf_warning"]))


def _render_subject_file_action(
    job: JobRecord,
    subject: tuple[str, str] | None,
    key_prefix: str,
    *,
    label: str,
) -> None:
    """Open the job's subject and lecture folder in the File Manager."""
    if subject is None:
        return
    subject_id, _ = subject
    if st.button(
        label,
        key=f"{key_prefix}_open_subject_{job.id}",
        help="Open this job's files in the File Manager.",
        width="stretch",
    ):
        folder_id = str(
            job.result.get("library_folder_id", job.payload.get("library_folder_id", "")) or ""
        ).strip()
        st.session_state["library_open_subject"] = subject_id
        if folder_id:
            st.session_state[f"library_browse_folder_{subject_id}"] = folder_id
        st.session_state["requested_workspace"] = "Library"
        st.rerun()


def _render_job(
    job: JobRecord,
    manager: JobManager,
    subject_names: dict[str, str],
    editable: bool = False,
) -> None:
    icon = STATUS_ICONS.get(job.status, "•")
    subject = manager.job_subject(job, subject_names)
    with st.container(border=True):
        details_column, actions_column = st.columns([6, 1.6], vertical_alignment="top")
        with details_column:
            st.markdown(f"**{icon} {job.title}**")
            st.caption(
                f"{job.status.title()} · {job.priority_label} priority · {job.kind} · "
                f"Subject: {subject[1] if subject else 'Not saved to a subject'} · job {job.id[:8]}"
            )
            status_text = f"{job.progress}% · {job.stage.title()} — {job.message}"
            if job.status in {"running", "waiting"}:
                st.progress(max(0, min(job.progress, 100)), text=status_text)
            else:
                st.caption(status_text)
            if job.error:
                st.error(job.error)

        with actions_column:
            log_column, files_column = st.columns(2)
            if log_column.button("Log", key=f"open_job_log_{job.id}", help="Open processing log", width="stretch"):
                st.session_state["job_log_id"] = job.id
                st.rerun()
            with files_column:
                _render_subject_file_action(job, subject, "card", label="Files")
            if editable or job.status == "failed":
                _render_job_actions(job, manager, "card", compact=True)


def _transcript_text(payload: dict[str, object]) -> str:
    paragraphs = payload.get("paragraphs", [])
    if isinstance(paragraphs, list) and paragraphs:
        lines = []
        for paragraph in paragraphs:
            if not isinstance(paragraph, dict):
                continue
            timestamp = f"{paragraph.get('start_time', '')}–{paragraph.get('end_time', '')}".strip("–")
            text = str(paragraph.get("text", "")).strip()
            if text:
                lines.append(f"[{timestamp}] {text}" if timestamp else text)
        return "\n\n".join(lines)
    segments = payload.get("segments", [])
    if not isinstance(segments, list):
        return ""
    return "\n".join(
        f"[{segment.get('start_time', '')}–{segment.get('end_time', '')}] {segment.get('text', '')}".strip()
        for segment in segments
        if isinstance(segment, dict) and str(segment.get("text", "")).strip()
    )


def _render_job_log(manager: JobManager, job_id: str) -> None:
    if st.button("← Back to Job Queue", width="content"):
        st.session_state.pop("job_log_id", None)
        st.rerun()
    try:
        job = manager.get_job(job_id)
    except JobError as exc:
        st.error(str(exc))
        st.session_state.pop("job_log_id", None)
        return

    log_title = {
        "translation": "Translation Processing Log",
        "slide_review": "Deep Slide Review Log",
    }.get(job.kind, "Lecture Processing Log")
    st.title(f"📋 {log_title}")
    notice = st.session_state.pop("job_log_notice", "")
    if notice:
        st.success(notice)
    icon = STATUS_ICONS.get(job.status, "•")
    st.subheader(f"{icon} {job.title}")
    subject = manager.job_subject(job)
    st.caption(
        f"{job.status.title()} · {job.priority_label} priority · job {job.id[:8]} · "
        "this screen refreshes every 2 seconds"
    )
    st.caption(f"Subject · {subject[1] if subject else 'Not saved to a subject'}")
    _render_subject_file_action(job, subject, "log", label="Open in File Manager")
    st.progress(max(0, min(job.progress, 100)), text=f"{job.progress}% · {job.stage.title()} — {job.message}")
    if job.error:
        st.error(job.error)
    if job.result.get("library_messages"):
        with st.expander("Storage details", expanded=False):
            for message in job.result["library_messages"]:
                st.write(f"- {message}")
    _render_job_actions(job, manager, "log")

    if job.kind in {"translation", "slide_review"} and job.status == "completed":
        st.subheader("Translated result" if job.kind == "translation" else "Deep slide review")
        _render_result_preview(job, "log")

    if job.kind == "lecture":
        st.subheader("Stored transcript")
        transcript_path = manager.get_transcript_path(job.id)
        if transcript_path is None:
            st.info("The stored transcript will appear here after the first recording chunk completes.")
        else:
            try:
                transcript_bytes = transcript_path.read_bytes()
                transcript = json.loads(transcript_bytes.decode("utf-8"))
                metadata = transcript.get("metadata", {}) if isinstance(transcript, dict) else {}
                transcript_text = _transcript_text(transcript) if isinstance(transcript, dict) else ""
                is_partial = bool(metadata.get("is_partial", False)) if isinstance(metadata, dict) else False
                completed_chunks = metadata.get("completed_chunks") if isinstance(metadata, dict) else None
                total_chunks = metadata.get("total_chunks") if isinstance(metadata, dict) else None
                transcript_kind = str(metadata.get("transcript_kind", "")) if isinstance(metadata, dict) else ""
                is_cleaned = transcript_kind == "cleaned" or transcript_path.name.startswith("transcript.cleanup")
                if is_cleaned:
                    label = "Partial cleaned transcript" if is_partial else "Cleaned, human-readable transcript"
                else:
                    label = "Partial raw transcript" if is_partial else "Raw Whisper transcript"
                if completed_chunks is not None and total_chunks is not None:
                    label += f" · {completed_chunks}/{total_chunks} recording chunks stored"
                st.caption(f"{label} · {transcript_path}")
                preview = transcript_text
                if len(preview) > 40_000:
                    preview = "… earlier transcript omitted from preview …\n\n" + preview[-40_000:]
                st.text_area(
                    "Transcript preview",
                    value=preview or "No speech text has been stored yet.",
                    height=320,
                    disabled=True,
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                st.warning(f"The transcript exists but could not be opened yet: {exc}")

    st.subheader("Processing timeline")
    events = manager.list_job_events(job.id)
    if not events:
        st.info("No processing events have been recorded yet.")
        return
    timeline_rows = [
        {
            "Time": event.created_at.replace("T", " ").replace("+00:00", " UTC"),
            "Stage": (event.stage or "task").replace("_", " ").title(),
            "Progress": f"{event.progress}%",
            "Level": event.level.title(),
            "Message": event.message,
        }
        for event in reversed(events)
    ]
    st.dataframe(
        timeline_rows,
        hide_index=True,
        width="stretch",
        height=min(360, max(80, 36 * (len(timeline_rows) + 1))),
        column_config={
            "Time": st.column_config.TextColumn(width="medium"),
            "Stage": st.column_config.TextColumn(width="small"),
            "Progress": st.column_config.TextColumn(width="small"),
            "Level": st.column_config.TextColumn(width="small"),
            "Message": st.column_config.TextColumn(width="large"),
        },
    )


@st.fragment(run_every="2s")
def render_jobs(manager: JobManager) -> None:
    selected_job_id = str(st.session_state.get("job_log_id", "")).strip()
    if selected_job_id:
        _render_job_log(manager, selected_job_id)
        return
    st.title("🗂️ Job Queue")
    st.caption(
        "Tasks run in priority order. Active lecture work can stop at a safe checkpoint, move to Later, "
        "and resume from its saved transcript and completed slide notes."
    )
    counts = manager.counts()
    planned_count = counts["queued"]
    active_count = counts["running"] + counts["waiting"]
    deferred_count = counts["deferred"]
    completed_count = counts["completed"]
    first, second, third, fourth = st.columns(4)
    first.metric("Planned", planned_count)
    second.metric("Active", active_count)
    third.metric("Later", deferred_count)
    fourth.metric("Completed", completed_count)
    _render_ollama_controls(manager)

    agent_active = manager.is_agent_active()
    if agent_active:
        st.warning(
            "The local agent is active. Transcription may continue, but lecture Qwen generation waits "
            "between model calls until the agent is deactivated."
        )
        if st.button("Deactivate local agent", width="stretch"):
            manager.set_agent_active(False)
            st.session_state["agent_active_toggle"] = False
            st.rerun()
    else:
        st.info("The local agent is inactive; queued lecture generation may use Qwen when it reaches that stage.")
    jobs = manager.list_jobs()
    subject_names = {
        subject.id: subject.name
        for subject in LibraryStore(manager.project_root / "library").list_subjects()
    }
    active = [job for job in jobs if job.status in {"running", "waiting"}]
    planned = [job for job in jobs if job.status == "queued"]
    deferred = [job for job in jobs if job.status == "deferred"]
    history = [job for job in jobs if job.status in FINAL_STATUSES]

    active_tab, planned_tab, deferred_tab, history_tab = st.tabs(
        [
            f"Doing ({len(active)})",
            f"Planned ({len(planned)})",
            f"Later ({len(deferred)})",
            f"History ({len(history)})",
        ]
    )
    with active_tab:
        if not active:
            st.info("No task is running right now.")
        for job in active:
            _render_job(job, manager, subject_names, editable=True)
    with planned_tab:
        if not planned:
            st.info("No tasks are waiting in the queue.")
        for job in planned:
            _render_job(job, manager, subject_names, editable=True)
    with deferred_tab:
        if not deferred:
            st.info("Tasks stopped for future processing will appear here with their saved checkpoints.")
        for job in deferred:
            _render_job(job, manager, subject_names, editable=True)
    with history_tab:
        if not history:
            st.info("Completed, failed, and cancelled tasks will appear here.")
        for job in history:
            _render_job(job, manager, subject_names)
