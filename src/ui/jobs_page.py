"""Queue visibility and controls for background tasks."""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from ..config import PipelineConfig
from ..jobs import FINAL_STATUSES, PRIORITIES, JobError, JobManager, JobRecord
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


def _render_job_actions(job: JobRecord, manager: JobManager, key_prefix: str) -> None:
    """Render lifecycle controls consistently on cards and the live log screen."""
    if job.status == "queued":
        labels = list(PRIORITIES)
        current_index = labels.index(job.priority_label)
        priority = st.selectbox(
            "Priority",
            labels,
            index=current_index,
            key=f"{key_prefix}_priority_{job.id}",
        )
        apply_column, later_column, cancel_column = st.columns(3)
        if apply_column.button(
            "Update priority",
            key=f"{key_prefix}_apply_priority_{job.id}",
            use_container_width=True,
        ):
            try:
                manager.update_priority(job.id, PRIORITIES[priority])
                st.rerun()
            except JobError as exc:
                st.error(str(exc))
        if later_column.button(
            "Do later",
            key=f"{key_prefix}_defer_{job.id}",
            use_container_width=True,
        ):
            try:
                manager.defer_job(job.id)
                st.rerun()
            except JobError as exc:
                st.error(str(exc))
        if cancel_column.button(
            "Cancel permanently",
            key=f"{key_prefix}_cancel_{job.id}",
            use_container_width=True,
        ):
            manager.cancel_job(job.id)
            st.rerun()
    elif job.status in {"running", "waiting"}:
        stop_column, cancel_column = st.columns(2)
        if stop_column.button(
            "Stop safely · do later",
            key=f"{key_prefix}_defer_{job.id}",
            disabled=job.defer_requested or job.cancel_requested,
            help="Finishes the current safe checkpoint, preserves transcript and completed slide notes, then moves the task to Later.",
            use_container_width=True,
        ):
            try:
                manager.defer_job(job.id)
                st.rerun()
            except JobError as exc:
                st.error(str(exc))
        if cancel_column.button(
            "Cancel permanently",
            key=f"{key_prefix}_cancel_{job.id}",
            disabled=job.cancel_requested,
            use_container_width=True,
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
        )
        resume_column, cancel_column = st.columns(2)
        if resume_column.button(
            "Resume from checkpoints",
            key=f"{key_prefix}_resume_{job.id}",
            type="primary",
            use_container_width=True,
        ):
            try:
                manager.resume_job(job.id, PRIORITIES[priority])
                st.rerun()
            except JobError as exc:
                st.error(str(exc))
        if cancel_column.button(
            "Cancel permanently",
            key=f"{key_prefix}_cancel_{job.id}",
            use_container_width=True,
        ):
            manager.cancel_job(job.id)
            st.rerun()


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
            if stop_column.button("Unload", key=f"unload_ollama_{model.name}", use_container_width=True):
                try:
                    _show_unload_result(manager.stop_loaded_ollama_models(host, [model.name]))
                    st.rerun()
                except JobError as exc:
                    st.error(str(exc))

        if len(models) > 1 and st.button("Unload all models", use_container_width=True):
            try:
                _show_unload_result(manager.stop_loaded_ollama_models(host))
                st.rerun()
            except JobError as exc:
                st.error(str(exc))

        last_cleanup = manager.last_ollama_cleanup()
        if last_cleanup:
            st.caption(last_cleanup)


def _render_completed_files(job: JobRecord, key_prefix: str, *, preview: bool = False) -> None:
    markdown_path = Path(str(job.result.get("markdown_path", "")))
    pdf_path = Path(str(job.result.get("pdf_path", "")))
    translated = job.kind == "translation"
    slide_review = job.kind == "slide_review"
    if translated:
        target = str(job.result.get("target_language", job.payload.get("target_language", "translation")))
        st.caption(f"Requested translation: English → {target}")
    if preview and (translated or slide_review) and markdown_path.is_file():
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
    left, right = st.columns(2)
    if markdown_path.is_file():
        left.download_button(
            "Download translated Markdown" if translated else "Download deep review" if slide_review else "Download Markdown",
            data=markdown_path.read_bytes(),
            file_name=markdown_path.name,
            mime="text/markdown",
            key=f"{key_prefix}_md_{job.id}",
            use_container_width=True,
        )
    if pdf_path.is_file():
        right.download_button(
            "Download translated PDF" if translated else "Download deep-review PDF" if slide_review else "Download PDF",
            data=pdf_path.read_bytes(),
            file_name=pdf_path.name,
            mime="application/pdf",
            key=f"{key_prefix}_pdf_{job.id}",
            use_container_width=True,
        )
    if (translated or slide_review) and job.result.get("pdf_warning"):
        st.warning(str(job.result["pdf_warning"]))


def _render_translation_request(job: JobRecord, manager: JobManager, key_prefix: str) -> None:
    if job.kind != "lecture" or job.status != "completed":
        return
    with st.expander("Translate finished notes on demand", expanded=False):
        st.caption(
            "The canonical transcript and notes stay in English. Nothing is translated until you submit this request."
        )
        with st.form(f"{key_prefix}_translation_form_{job.id}"):
            language_option = st.selectbox(
                "Target language",
                [
                    "Chinese (Simplified)",
                    "Chinese (Traditional)",
                    "French",
                    "Dutch",
                    "German",
                    "Spanish",
                    "Japanese",
                    "Korean",
                    "Other language",
                ],
            )
            custom_language = st.text_input(
                "Other target language",
                placeholder="e.g. Italian",
                disabled=language_option != "Other language",
            )
            priority_label = st.selectbox(
                "Translation priority",
                list(PRIORITIES),
                index=list(PRIORITIES).index("Normal"),
            )
            submitted = st.form_submit_button(
                "Queue translation",
                type="primary",
                use_container_width=True,
            )
        if submitted:
            target_language = custom_language.strip() if language_option == "Other language" else language_option
            try:
                translation_job = manager.enqueue_translation(
                    job.id,
                    target_language,
                    priority=PRIORITIES[priority_label],
                )
                st.session_state["job_log_id"] = translation_job.id
                st.session_state["job_log_notice"] = (
                    f"Queued an on-demand {target_language} translation. The English originals are unchanged."
                )
                st.rerun()
            except JobError as exc:
                st.error(str(exc))


def _render_slide_review_request(job: JobRecord, manager: JobManager, key_prefix: str) -> None:
    if job.kind != "lecture" or job.status != "completed":
        return
    slides_path = Path(str(job.result.get("slides_path", "")))
    try:
        payload = json.loads(slides_path.read_text(encoding="utf-8"))
        choices = [
            (int(slide["slide"]), str(slide.get("title", "")).strip())
            for slide in payload.get("slides", [])
        ]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        choices = []
    if not choices:
        return
    with st.expander("Deep-review one slide on demand", expanded=False):
        st.caption(
            "Uses high reasoning and a second factual audit only for the selected slide. "
            "The fast baseline notes remain unchanged."
        )
        with st.form(f"{key_prefix}_slide_review_form_{job.id}"):
            selected = st.selectbox(
                "Slide",
                choices,
                format_func=lambda item: f"Slide {item[0]}: {item[1]}" if item[1] else f"Slide {item[0]}",
            )
            priority_label = st.selectbox(
                "Deep-review priority",
                list(PRIORITIES),
                index=list(PRIORITIES).index("Normal"),
            )
            submitted = st.form_submit_button(
                "Queue deep review",
                type="primary",
                use_container_width=True,
            )
        if submitted:
            try:
                review_job = manager.enqueue_slide_review(
                    job.id,
                    selected[0],
                    priority=PRIORITIES[priority_label],
                )
                st.session_state["job_log_id"] = review_job.id
                st.session_state["job_log_notice"] = (
                    f"Queued a deep review of slide {selected[0]}. The baseline notes are unchanged."
                )
                st.rerun()
            except JobError as exc:
                st.error(str(exc))


def _render_job(job: JobRecord, manager: JobManager, editable: bool = False) -> None:
    icon = STATUS_ICONS.get(job.status, "•")
    with st.container(border=True):
        title_column, status_column = st.columns([3, 1])
        title_column.markdown(f"**{icon} {job.title}**")
        status_column.write(job.status.title())
        st.caption(f"{job.priority_label} priority · {job.kind} · job {job.id[:8]}")
        st.progress(max(0, min(job.progress, 100)), text=f"{job.progress}% · {job.stage.title()} — {job.message}")

        if job.error:
            st.error(job.error)
        if st.button("Open processing log", key=f"open_job_log_{job.id}", use_container_width=True):
            st.session_state["job_log_id"] = job.id
            st.rerun()
        if job.result.get("library_messages"):
            with st.expander("Library storage details"):
                for message in job.result["library_messages"]:
                    st.write(f"- {message}")
        if job.status == "completed":
            _render_completed_files(job, "job")
            _render_slide_review_request(job, manager, "card")
            _render_translation_request(job, manager, "card")

        if editable:
            _render_job_actions(job, manager, "card")


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
    if st.button("← Back to Job Queue", use_container_width=False):
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
    st.caption(
        f"{job.status.title()} · {job.priority_label} priority · job {job.id[:8]} · "
        "this screen refreshes every 2 seconds"
    )
    st.progress(max(0, min(job.progress, 100)), text=f"{job.progress}% · {job.stage.title()} — {job.message}")
    if job.error:
        st.error(job.error)
    _render_job_actions(job, manager, "log")

    if job.kind in {"translation", "slide_review"} and job.status == "completed":
        st.subheader("Translated result" if job.kind == "translation" else "Deep slide review")
        _render_completed_files(job, "log", preview=True)

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
                raw_transcript_path = manager.get_raw_transcript_path(job.id)
                download_columns = st.columns(3 if raw_transcript_path else 2)
                json_column, text_column = download_columns[:2]
                json_column.download_button(
                    "Download transcript JSON",
                    data=transcript_bytes,
                    file_name=transcript_path.name,
                    mime="application/json",
                    key=f"job_transcript_json_{job.id}",
                    use_container_width=True,
                )
                text_column.download_button(
                    "Download transcript text",
                    data=transcript_text.encode("utf-8"),
                    file_name=f"{transcript_path.stem}.txt",
                    mime="text/plain",
                    key=f"job_transcript_text_{job.id}",
                    use_container_width=True,
                )
                if raw_transcript_path:
                    download_columns[2].download_button(
                        "Download raw Whisper JSON",
                        data=raw_transcript_path.read_bytes(),
                        file_name=raw_transcript_path.name,
                        mime="application/json",
                        key=f"job_raw_transcript_{job.id}",
                        use_container_width=True,
                    )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                st.warning(f"The transcript exists but could not be opened yet: {exc}")
        _render_translation_request(job, manager, "log")
        _render_slide_review_request(job, manager, "log")

    st.subheader("Processing timeline")
    events = manager.list_job_events(job.id)
    if not events:
        st.info("No processing events have been recorded yet.")
        return
    event_payload = [
        {
            "id": event.id,
            "created_at": event.created_at,
            "level": event.level,
            "stage": event.stage,
            "progress": event.progress,
            "message": event.message,
            "data": event.data,
        }
        for event in events
    ]
    event_text = "\n".join(
        f"{event.created_at} | {event.level.upper():7} | {event.progress:3}% | "
        f"{event.stage or 'task'} | {event.message}"
        for event in events
    )
    json_column, text_column = st.columns(2)
    json_column.download_button(
        "Download log JSON",
        data=json.dumps(event_payload, indent=2, ensure_ascii=False).encode("utf-8"),
        file_name=f"{job.id}_processing_log.json",
        mime="application/json",
        key=f"job_log_json_{job.id}",
        use_container_width=True,
    )
    text_column.download_button(
        "Download log text",
        data=event_text.encode("utf-8"),
        file_name=f"{job.id}_processing_log.txt",
        mime="text/plain",
        key=f"job_log_text_{job.id}",
        use_container_width=True,
    )
    for event in reversed(events):
        with st.container(border=True):
            st.markdown(f"**{event.progress}% · {(event.stage or 'task').replace('_', ' ').title()}**")
            st.write(event.message)
            st.caption(f"{event.created_at} · {event.level.title()}")


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
        if st.button("Deactivate local agent", use_container_width=True):
            manager.set_agent_active(False)
            st.session_state["agent_active_toggle"] = False
            st.rerun()
    else:
        st.info("The local agent is inactive; queued lecture generation may use Qwen when it reaches that stage.")
    jobs = manager.list_jobs()
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
            _render_job(job, manager, editable=True)
    with planned_tab:
        if not planned:
            st.info("No tasks are waiting in the queue.")
        for job in planned:
            _render_job(job, manager, editable=True)
    with deferred_tab:
        if not deferred:
            st.info("Tasks stopped for future processing will appear here with their saved checkpoints.")
        for job in deferred:
            _render_job(job, manager, editable=True)
    with history_tab:
        if not history:
            st.info("Completed, failed, and cancelled tasks will appear here.")
        for job in history:
            _render_job(job, manager)
