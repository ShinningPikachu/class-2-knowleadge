"""Subject-library page."""

from __future__ import annotations

import streamlit as st

from ..library import LibraryStore
from .common import format_size, render_search_results, render_subject_creator, render_upload_panel, subject_lookup


def render_library(library: LibraryStore) -> None:
    st.title("📚 Subject Library")
    st.caption("Create a subject, add course material, and keep every source and generated result together.")
    stats = library.stats()
    first, second, third = st.columns(3)
    first.metric("Subjects", stats["subjects"])
    second.metric("Documents", stats["documents"])
    third.metric("Searchable", stats["indexed_documents"])

    render_subject_creator(library, "library")
    subjects = library.list_subjects()
    if not subjects:
        st.info("Your library is empty. Create the first subject above to get started.")
        return

    lookup = subject_lookup(subjects)
    selected_id = st.selectbox(
        "Open subject",
        options=[subject.id for subject in subjects],
        format_func=lambda value: lookup[value].name,
        key="library_open_subject",
    )
    subject = lookup[selected_id]
    st.subheader(subject.name)
    if subject.description:
        st.write(subject.description)

    documents_tab, upload_tab, search_tab = st.tabs(["Documents", "Add documents", "Search"])
    with documents_tab:
        documents = library.list_documents(subject.id)
        if not documents:
            st.info("This subject does not contain documents yet. Open Add documents to insert the first files.")
        for document in documents:
            with st.container(border=True):
                name_col, state_col = st.columns([3, 1])
                name_col.markdown(f"**{document.original_name}**")
                state_col.write(document.status.replace("_", " ").title())
                st.caption(f"{format_size(document.size_bytes)} · added {document.created_at[:10]}")
                if document.extraction_error:
                    st.warning(document.extraction_error)
                with st.expander("Local file details"):
                    st.code(str(document.stored_path))
    with upload_tab:
        render_upload_panel(library, [subject], "library")
    with search_tab:
        query = st.text_input("Search this subject", placeholder="Enter a file name, topic, definition, or phrase")
        if query.strip():
            render_search_results(library.search_documents(query, subject_id=subject.id, limit=12))
