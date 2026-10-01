"""Explicitly activated local agent with cited Q&A and confirmed file changes."""

from __future__ import annotations

from dataclasses import replace

import streamlit as st

from ..config import PipelineConfig
from ..codex_agent import CodexLibraryAgent
from ..jobs import JobManager
from ..library import LibraryDocument, LibraryError, LibraryStore, Subject
from ..library_agent import AgentPlan, LibraryAgent, LibraryAgentError
from .common import subject_lookup


LOCAL_REASONING_LEVELS = {
    "Light — faster": "light",
    "Balanced": "balanced",
    "Deep — more thorough": "deep",
}
CODEX_MODELS = {
    "Use my Codex default": "",
    "GPT-5.3 Codex": "gpt-5.3-codex",
}
CODEX_REASONING_LEVELS = {
    "Low — faster": "low",
    "Medium — balanced": "medium",
    "High — more thorough": "high",
    "Extra high — slowest": "xhigh",
}
AGENT_PROVIDERS = {
    "Local Ollama — private and offline": "ollama",
    "Codex — signed-in account": "codex",
}
AGENT_PROVIDER_LABELS = {value: label for label, value in AGENT_PROVIDERS.items()}


def _option_index(options: dict[str, str], value: str) -> int:
    """Use the first option when an old saved value is no longer offered."""
    values = list(options.values())
    return values.index(value) if value in values else 0


def _messages() -> list[dict[str, object]]:
    if "agent_messages" not in st.session_state:
        st.session_state["agent_messages"] = []
    return st.session_state["agent_messages"]


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
        if confirm.button("Confirm action", type="primary", width="stretch"):
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
        if cancel.button("Cancel", width="stretch"):
            st.session_state.pop("pending_agent_action", None)
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
    agent = (
        CodexLibraryAgent(config)
        if config.agent_provider == "codex"
        else LibraryAgent(config, chat_guard=lambda: manager.agent_qwen_slot(config))
    )
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
    st.title("🤖 Library Agent")
    st.caption("Activate the agent only when you need cited explanations or a simple library job.")
    selected_provider = st.selectbox(
        "Agent provider",
        options=list(AGENT_PROVIDERS),
        index=list(AGENT_PROVIDERS).index(AGENT_PROVIDER_LABELS[config.agent_provider]),
        help="Ollama stays on this computer. Codex uses the signed-in local Codex CLI.",
    )
    agent_config = replace(config, agent_provider=AGENT_PROVIDERS[selected_provider])
    codex_ready = True
    if agent_config.agent_provider == "codex":
        codex_left, codex_right = st.columns(2)
        selected_model = codex_left.selectbox(
            "Codex model",
            options=list(CODEX_MODELS),
            index=_option_index(CODEX_MODELS, agent_config.codex_model),
            help="Use your account default, or explicitly select the current Codex model.",
        )
        selected_reasoning = codex_right.selectbox(
            "Codex reasoning",
            options=list(CODEX_REASONING_LEVELS),
            index=_option_index(CODEX_REASONING_LEVELS, agent_config.codex_reasoning_effort),
            help="Higher reasoning levels take longer but can help with harder requests.",
        )
        agent_config = replace(
            agent_config,
            codex_model=CODEX_MODELS[selected_model],
            codex_reasoning_effort=CODEX_REASONING_LEVELS[selected_reasoning],
        )
        codex_ready, codex_status = CodexLibraryAgent.status()
        (st.success if codex_ready else st.warning)(codex_status)
        st.caption(
            "Codex receives only the current question and retrieved excerpts. It runs in an empty, "
            "read-only temporary workspace and cannot edit your library."
        )
    else:
        local_reasoning, local_model = st.columns([1, 2])
        selected_level = local_reasoning.selectbox(
            "Local reasoning",
            options=list(LOCAL_REASONING_LEVELS),
            index=_option_index(LOCAL_REASONING_LEVELS, agent_config.agent_reasoning_level),
            help=(
                "Light uses less model reasoning for quick, simple requests. "
                "Balanced and Deep use progressively more reasoning and can take longer."
            ),
        )
        local_model.caption(f"Using local model `{agent_config.llm_model}`")
        agent_config = replace(agent_config, agent_reasoning_level=LOCAL_REASONING_LEVELS[selected_level])
    scheduler_active = manager.is_agent_active()
    if "agent_active_toggle" not in st.session_state:
        st.session_state["agent_active_toggle"] = scheduler_active
    active = st.toggle(
        "Activate agent",
        key="agent_active_toggle",
        help="The selected provider is contacted only after activation and when you submit a request.",
        disabled=agent_config.agent_provider == "codex" and not codex_ready and not scheduler_active,
    )
    if active:
        manager.set_agent_active(True, agent_config)
    elif scheduler_active:
        manager.set_agent_active(False)
    if not active:
        st.info("The agent is off. Your library remains available, but no model is running for this screen.")
        return
    if agent_config.agent_provider == "codex":
        st.success("Codex is active in read-only mode. Lecture Qwen generation can continue normally.")
    else:
        st.success(
            "Agent priority is active. Background transcription may continue, while lecture Qwen generation "
            "waits safely between model calls."
        )

    subjects = library.list_subjects()
    if not subjects:
        st.info("Add a subject from the Library subject selector before starting library jobs.")
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
                    agent_config,
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
