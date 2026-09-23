"""Queue visibility and controls for background tasks."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from ..config import PipelineConfig
from ..jobs import FINAL_STATUSES, PRIORITIES, JobError, JobManager, JobRecord
from ..ollama_runtime import OllamaUnloadReport


STATUS_ICONS = {
    "queued": "🗓️",
    "running": "⚙️",
    "waiting": "⏸️",
    "completed": "✅",
    "failed": "❌",
    "cancelled": "🚫",
}


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
        if job.result.get("library_messages"):
            with st.expander("Library storage details"):
                for message in job.result["library_messages"]:
                    st.write(f"- {message}")
        if job.status == "completed":
            markdown_path = Path(str(job.result.get("markdown_path", "")))
            pdf_path = Path(str(job.result.get("pdf_path", "")))
            left, right = st.columns(2)
            if markdown_path.is_file():
                left.download_button(
                    "Download Markdown",
                    data=markdown_path.read_bytes(),
                    file_name=markdown_path.name,
                    mime="text/markdown",
                    key=f"job_md_{job.id}",
                    use_container_width=True,
                )
            if pdf_path.is_file():
                right.download_button(
                    "Download PDF",
                    data=pdf_path.read_bytes(),
                    file_name=pdf_path.name,
                    mime="application/pdf",
                    key=f"job_pdf_{job.id}",
                    use_container_width=True,
                )

        if editable and job.status == "queued":
            labels = list(PRIORITIES)
            current_index = labels.index(job.priority_label)
            priority = st.selectbox(
                "Priority",
                labels,
                index=current_index,
                key=f"priority_{job.id}",
            )
            apply_column, cancel_column = st.columns(2)
            if apply_column.button("Update priority", key=f"apply_priority_{job.id}", use_container_width=True):
                try:
                    manager.update_priority(job.id, PRIORITIES[priority])
                    st.rerun()
                except JobError as exc:
                    st.error(str(exc))
            if cancel_column.button("Cancel task", key=f"cancel_{job.id}", use_container_width=True):
                manager.cancel_job(job.id)
                st.rerun()
        elif editable and job.status in {"running", "waiting"}:
            if st.button(
                "Request cancellation",
                key=f"cancel_{job.id}",
                disabled=job.cancel_requested,
                use_container_width=True,
            ):
                manager.cancel_job(job.id)
                st.rerun()


@st.fragment(run_every="2s")
def render_jobs(manager: JobManager) -> None:
    st.title("🗂️ Job Queue")
    st.caption("Tasks run in priority order. The active task reports its current stage and resource wait state.")
    counts = manager.counts()
    planned_count = counts["queued"]
    active_count = counts["running"] + counts["waiting"]
    completed_count = counts["completed"]
    first, second, third = st.columns(3)
    first.metric("Planned", planned_count)
    second.metric("Active", active_count)
    third.metric("Completed", completed_count)
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
    history = [job for job in jobs if job.status in FINAL_STATUSES]

    active_tab, planned_tab, history_tab = st.tabs(
        [f"Doing ({len(active)})", f"Planned ({len(planned)})", f"History ({len(history)})"]
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
    with history_tab:
        if not history:
            st.info("Completed, failed, and cancelled tasks will appear here.")
        for job in history:
            _render_job(job, manager)
