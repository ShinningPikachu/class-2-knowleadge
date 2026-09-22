"""Explicitly activated local agent with cited Q&A and confirmed file jobs."""

from __future__ import annotations

import streamlit as st

from ..config import PipelineConfig
from ..jobs import JobManager
from ..library import LibraryDocument, LibraryError, LibraryStore, Subject
from ..library_agent import AgentPlan, LibraryAgent, LibraryAgentError
from .common import render_subject_creator, render_upload_panel, subject_lookup


def _messages() -> list[dict[str, object]]:
    if "agent_messages" not in st.session_state:
        st.session_state["agent_messages"] = []
    return st.session_state["agent_messages"]


def _document_label(document: LibraryDocument) -> str:
    return f"{document.original_name} — {document.subject_name}"


def _stage_plan(plan: AgentPlan) -> None:
    st.session_state["pending_agent_action"] = {
        "action": plan.action,
        "document_id": plan.document_id,
        "new_name": plan.new_name,
        "target_subject_id": plan.target_subject_id,
        "subject_name": plan.subject_name,
    }


def _render_pending_action(library: LibraryStore) -> None:
    pending = st.session_state.get("pending_agent_action")
    if not pending:
        return
    action = str(pending.get("action", ""))
    try:
        if action == "rename_document":
            document = library.get_document(str(pending["document_id"]))
            description = (
                f"Rename **{document.original_name}** to **{pending['new_name']}** "
                f"in {document.subject_name}."
            )
        elif action == "move_document":
            document = library.get_document(str(pending["document_id"]))
            target = library.get_subject(str(pending["target_subject_id"]))
            description = f"Move **{document.original_name}** from {document.subject_name} to **{target.name}**."
        elif action == "create_subject":
            description = f"Create a new subject named **{pending['subject_name']}**."
        else:
            st.session_state.pop("pending_agent_action", None)
            return
    except LibraryError as exc:
        st.error(str(exc))
        st.session_state.pop("pending_agent_action", None)
        return

    with st.container(border=True):
        st.warning("Agent action awaiting confirmation")
        st.markdown(description)
        confirm, cancel = st.columns(2)
        if confirm.button("Confirm action", type="primary", use_container_width=True):
            try:
                if action == "rename_document":
                    updated = library.rename_document(str(pending["document_id"]), str(pending["new_name"]))
                    result = f"Renamed the file to **{updated.original_name}**."
                elif action == "move_document":
                    updated = library.move_document(str(pending["document_id"]), str(pending["target_subject_id"]))
                    result = f"Moved **{updated.original_name}** to **{updated.subject_name}**."
                else:
                    subject = library.create_subject(str(pending["subject_name"]))
                    result = f"Created the subject **{subject.name}**."
                _messages().append({"role": "assistant", "content": result, "sources": []})
                st.session_state.pop("pending_agent_action", None)
                st.rerun()
            except LibraryError as exc:
                st.error(str(exc))
        if cancel.button("Cancel", use_container_width=True):
            st.session_state.pop("pending_agent_action", None)
            st.rerun()


def _render_quick_jobs(library: LibraryStore, subjects: list[Subject], documents: list[LibraryDocument]) -> None:
    with st.expander("Quick file jobs"):
        if not documents:
            st.info("Add a document before using rename or move jobs.")
            return
        document_lookup = {document.id: document for document in documents}
        selected_id = st.selectbox(
            "Document",
            options=[document.id for document in documents],
            format_func=lambda value: _document_label(document_lookup[value]),
            key="agent_job_document",
        )
        selected = document_lookup[selected_id]
        new_name = st.text_input(
            "New file name",
            value=selected.original_name,
            key=f"agent_rename_{selected.id}",
            help="The original file extension is preserved.",
        )
        if st.button("Prepare rename", use_container_width=True):
            _stage_plan(AgentPlan(action="rename_document", document_id=selected.id, new_name=new_name))
            st.rerun()

        destinations = [subject for subject in subjects if subject.id != selected.subject_id]
        destination_lookup = subject_lookup(destinations)
        target_id = st.selectbox(
            "Move to subject",
            options=[subject.id for subject in destinations],
            format_func=lambda value: destination_lookup[value].name,
            disabled=not destinations,
            key=f"agent_move_{selected.id}",
        ) if destinations else ""
        if st.button("Prepare move", use_container_width=True, disabled=not destinations):
            _stage_plan(AgentPlan(action="move_document", document_id=selected.id, target_subject_id=target_id))
            st.rerun()


def _list_documents_answer(documents: list[LibraryDocument]) -> str:
    if not documents:
        return "No documents are stored in that scope yet."
    lines = [
        f"- **{document.original_name}** — {document.subject_name} "
        f"({document.status.replace('_', ' ')})"
        for document in documents
    ]
    return "Here are the matching library documents:\n\n" + "\n".join(lines)


def _handle_prompt(
    library: LibraryStore,
    config: PipelineConfig,
    prompt: str,
    scope: str,
    subjects: list[Subject],
    documents: list[LibraryDocument],
    manager: JobManager,
) -> tuple[str, list[dict[str, str]]]:
    config.validate()
    agent = LibraryAgent(config, chat_guard=manager.agent_qwen_slot)
    if agent.should_plan_action(prompt):
        plan = agent.plan(prompt, subjects, documents)
        if plan.action in {"rename_document", "move_document", "create_subject"}:
            _stage_plan(plan)
            return (
                plan.message or "I prepared the requested library change. Review and confirm it above before it runs.",
                [],
            )
        if plan.action == "list_documents":
            selected_scope = plan.target_subject_id or (None if scope == "all" else scope)
            return _list_documents_answer(library.list_documents(selected_scope)), []
        if plan.message and not plan.query:
            return plan.message, []
        prompt = plan.query or prompt

    results = library.search_documents(prompt, subject_id=None if scope == "all" else scope, limit=8)
    answer = agent.answer(prompt, results)
    sources = [
        {"marker": f"S{index}", "citation": result.citation, "text": result.text}
        for index, result in enumerate(results, start=1)
    ]
    return answer, sources


def render_agent(library: LibraryStore, config: PipelineConfig, manager: JobManager) -> None:
    st.title("🤖 Local Library Agent")
    st.caption("Activate the agent only when you need cited explanations or a simple library job.")
    scheduler_active = manager.is_agent_active()
    if "agent_active_toggle" not in st.session_state:
        st.session_state["agent_active_toggle"] = scheduler_active
    active = st.toggle(
        "Activate local agent",
        key="agent_active_toggle",
        help="The local model is contacted only after activation and when you submit a request.",
    )
    if active != scheduler_active:
        manager.set_agent_active(active)
    if not active:
        st.info("The agent is off. Your library remains available, but no local model is running for this screen.")
        return
    st.success(
        "Agent priority is active. Background transcription may continue, while lecture Qwen generation "
        "waits safely between model calls."
    )

    subjects = library.list_subjects()
    if not subjects:
        st.info("Create a subject and add documents before starting library jobs.")
        render_subject_creator(library, "agent")
        return
    documents = library.list_documents()
    lookup = subject_lookup(subjects)
    scope = st.selectbox(
        "Question scope",
        ["all"] + [subject.id for subject in subjects],
        format_func=lambda value: "All subjects" if value == "all" else lookup[value].name,
    )
    st.caption(
        'Try “Explain breadth-first search”, “List my documents”, '
        'or “Rename notes.txt to week-1-notes.txt”.'
    )

    with st.expander("Insert documents into a subject"):
        render_upload_panel(library, subjects, "agent")
    _render_quick_jobs(library, subjects, documents)
    _render_pending_action(library)

    for message in _messages():
        with st.chat_message(str(message["role"])):
            st.markdown(str(message["content"]))
            sources = message.get("sources") or []
            if sources:
                with st.expander("Sources used"):
                    for source in sources:
                        st.markdown(f"**[{source['marker']}] {source['citation']}**")
                        st.write(source["text"])

    prompt = st.chat_input("Ask a question or request a simple library job")
    if not prompt:
        return
    _messages().append({"role": "user", "content": prompt, "sources": []})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        with st.spinner("The local agent is working…"):
            try:
                answer, sources = _handle_prompt(
                    library,
                    config,
                    prompt,
                    scope,
                    subjects,
                    documents,
                    manager,
                )
            except (LibraryAgentError, LibraryError, ValueError) as exc:
                answer = f"I could not complete that request: {exc}"
                sources = []
            st.markdown(answer)
            if sources:
                with st.expander("Sources used"):
                    for source in sources:
                        st.markdown(f"**[{source['marker']}] {source['citation']}**")
                        st.write(source["text"])
    _messages().append({"role": "assistant", "content": answer, "sources": sources})
