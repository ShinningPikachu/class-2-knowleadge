"""Shared Streamlit controls for subjects, uploads, search, and model settings."""

from __future__ import annotations

from typing import Any

import streamlit as st

from ..config import PipelineConfig
from ..library import DuplicateDocumentError, LibraryError, LibraryStore, SearchResult, Subject


def format_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


def subject_lookup(subjects: list[Subject]) -> dict[str, Subject]:
    return {subject.id: subject for subject in subjects}


def render_subject_creator(library: LibraryStore, key_prefix: str) -> None:
    with st.expander("Create a subject", expanded=not library.list_subjects()):
        with st.form(f"{key_prefix}_create_subject", clear_on_submit=True):
            name = st.text_input("Subject name", placeholder="e.g. Artificial Intelligence")
            description = st.text_area("Description (optional)", placeholder="Course, semester, or useful context")
            submitted = st.form_submit_button("Create subject", type="primary", use_container_width=True)
        if submitted:
            try:
                subject = library.create_subject(name, description)
                st.success(f"Created {subject.name}.")
                st.rerun()
            except LibraryError as exc:
                st.error(str(exc))


def store_uploads(library: LibraryStore, subject_id: str, uploads: list[Any]) -> tuple[int, list[str]]:
    stored = 0
    messages: list[str] = []
    for upload in uploads:
        try:
            document = library.add_document_bytes(subject_id, upload.name, bytes(upload.getbuffer()))
            stored += 1
            if document.status == "indexed":
                messages.append(f"Indexed {document.original_name}.")
            elif document.status == "extraction_failed":
                messages.append(
                    f"Stored {document.original_name}, but text extraction failed: {document.extraction_error}"
                )
            else:
                messages.append(f"Stored {document.original_name}; this file type is not text-indexed yet.")
        except DuplicateDocumentError as exc:
            messages.append(str(exc))
        except LibraryError as exc:
            messages.append(f"Could not add {upload.name}: {exc}")
    return stored, messages


def render_upload_panel(library: LibraryStore, subjects: list[Subject], key_prefix: str) -> None:
    if not subjects:
        st.info("Create a subject before adding documents.")
        return
    lookup = subject_lookup(subjects)
    subject_id = st.selectbox(
        "Destination subject",
        options=[subject.id for subject in subjects],
        format_func=lambda value: lookup[value].name,
        key=f"{key_prefix}_upload_subject",
    )
    uploads = st.file_uploader(
        "Choose one or more documents",
        accept_multiple_files=True,
        key=f"{key_prefix}_uploads",
        help="PDF, PPTX, Markdown, text, CSV, and JSON are searchable immediately. Other files are safely stored.",
    )
    if st.button("Add documents", type="primary", disabled=not uploads, key=f"{key_prefix}_upload_button"):
        stored, messages = store_uploads(library, subject_id, list(uploads or []))
        if stored:
            st.success(f"Added {stored} document{'s' if stored != 1 else ''}.")
        for message in messages:
            st.write(f"- {message}")


def render_search_results(results: list[SearchResult]) -> None:
    if not results:
        st.info("No matching documents or indexed passages were found.")
        return
    for result in results:
        with st.container(border=True):
            st.markdown(f"**{result.document_name}** · {result.subject_name} · {result.locator}")
            st.write(result.text)


def render_model_settings(workspace: str) -> PipelineConfig:
    with st.sidebar:
        st.divider()
        st.header("Local model settings")
        llm_model = st.text_input("Assistant model", value="qwen3.5:27b")
        embedding_model = st.text_input("Embedding model", value="nomic-embed-text")
        ollama_host = st.text_input("Ollama URL", value="http://127.0.0.1:11434")
        values: dict[str, Any] = {
            "llm_model": llm_model.strip(),
            "embedding_model": embedding_model.strip(),
            "ollama_host": ollama_host.strip(),
        }
        if workspace == "Lecture Notes":
            values["whisper_model"] = st.text_input("faster-whisper model or local path", value="large-v3").strip()
            note_profile = st.selectbox(
                "Lecture note depth",
                ["Fast baseline", "Deep reviewed"],
                help=(
                    "Fast baseline uses one bounded, non-thinking call per slide. Deep reviewed uses high "
                    "reasoning plus a second factual audit and can take much longer. Individual baseline "
                    "slides can be deep-reviewed later from the completed job."
                ),
            )
            values["note_generation_profile"] = "fast" if note_profile == "Fast baseline" else "deep"
            values["note_max_output_tokens"] = 1_200 if note_profile == "Fast baseline" else 4_096
            language = st.text_input(
                "Spoken language (ISO code)",
                value="en",
                help="English is the default. This controls speech recognition only; translation is requested after completion.",
            )
            values["language"] = language.strip() or None
            with st.expander("Advanced processing settings"):
                values["llm_temperature"] = st.slider("LLM temperature", 0.0, 1.0, 0.10, 0.05)
                values["ollama_num_ctx"] = int(
                    st.select_slider("Ollama context window", [4096, 8192, 16384, 32768], value=16384)
                )
                values["whisper_device"] = st.selectbox("Whisper device", ["auto", "cpu", "cuda"], index=0)
                values["whisper_compute_type"] = st.selectbox(
                    "Whisper compute type", ["float32", "int8", "float16"], index=0
                )
                values["whisper_parallel_workers"] = int(
                    st.selectbox("Parallel transcription workers", [1, 2], index=1)
                )
                values["enable_transcript_cleanup"] = st.checkbox(
                    "Repair noisy transcript with local Qwen",
                    value=True,
                    help=(
                        "Preserves the raw transcript and creates a grounded, punctuated, human-readable copy "
                        "before alignment and note generation."
                    ),
                )
                values["transcript_cleanup_batch_chars"] = int(
                    st.number_input("Transcript cleanup batch size", 2000, 24000, 12000, 1000)
                )
                values["enable_ocr"] = st.checkbox("Use local Tesseract OCR", value=True)
                values["semantic_alignment_threshold"] = st.slider(
                    "Semantic alignment threshold", 0.0, 0.8, 0.20, 0.01
                )
                values["media_chunk_seconds"] = int(st.slider("Recording chunk length (minutes)", 10, 45, 30, 5) * 60)
                values["media_overlap_seconds"] = int(st.slider("Chunk boundary overlap (seconds)", 5, 30, 15, 5))
                values["chunk_size"] = int(st.number_input("RAG chunk size (characters)", 200, 3000, 900, 50))
                values["chunk_overlap"] = int(st.number_input("RAG overlap (characters)", 0, 1000, 140, 10))
                values["max_slide_context_chars"] = int(
                    st.number_input("Maximum context per slide", 2000, 50000, 28000, 1000)
                )
    return PipelineConfig(**values)
