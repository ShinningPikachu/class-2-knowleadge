"""Subject-library page."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

import streamlit as st

from ..jobs import JobError, JobManager, JobRecord
from ..library import LibraryDocument, LibraryError, LibraryFolder, LibraryStore
from .common import format_size, render_search_results, render_subject_creator, render_upload_panel, subject_lookup
from .file_manager_component import file_icon, render_file_manager


TEXT_PREVIEW_SUFFIXES = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".yaml",
    ".yml",
    ".py",
    ".html",
    ".htm",
    ".rtf",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
AUDIO_SUFFIXES = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


@dataclass(frozen=True)
class _LectureSlideBundle:
    slides: list[dict[str, Any]]
    summaries: dict[int, str]
    alignment: dict[int, list[dict[str, Any]]]
    source_job: JobRecord | None


def _load_json_file(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def _related_documents(library: LibraryStore, document: LibraryDocument) -> list[LibraryDocument]:
    return [
        item
        for item in library.list_documents(document.subject_id)
        if item.folder_id == document.folder_id
    ]


def _find_related_artifact(
    documents: list[LibraryDocument],
    required_tokens: tuple[str, ...],
) -> Path | None:
    for item in documents:
        normalized = re.sub(r"[^a-z0-9]+", "_", item.original_name.casefold()).strip("_")
        if all(token in normalized for token in required_tokens):
            return item.stored_path
    return None


def _source_names_match(stored_name: str, library_name: str) -> bool:
    stored = stored_name.casefold()
    target = library_name.casefold()
    return stored == target or any(
        stored.removeprefix(prefix) == target
        for prefix in ("slides_", "recording_")
    )


def _matches_document_content(path: Path, document: LibraryDocument) -> bool:
    try:
        if path.suffix.lower() != document.stored_path.suffix.lower() or path.stat().st_size != document.size_bytes:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest() == document.sha256
    except OSError:
        return False


def _matching_lecture_job(manager: JobManager, document: LibraryDocument) -> JobRecord | None:
    """Resolve a library deck back to the completed lecture that created it."""
    name = document.original_name.casefold()
    folder = document.folder_name.casefold()
    for job in manager.list_jobs(limit=500):
        if job.kind != "lecture" or job.status != "completed":
            continue
        lecture_name = str(job.result.get("lecture_name", job.payload.get("lecture_name", ""))).casefold()
        lecture_title = str(job.result.get("lecture_title", job.payload.get("lecture_title", ""))).casefold()
        if lecture_name and name.startswith(lecture_name):
            return job
        if folder and lecture_title and folder == lecture_title:
            return job
        run_dir = Path(str(job.result.get("run_dir", "")))
        if run_dir.is_dir():
            try:
                input_files = list((run_dir / "input").iterdir())
                if any(_source_names_match(path.name, name) for path in input_files):
                    return job
                if any(_matches_document_content(path, document) for path in input_files):
                    return job
            except OSError:
                continue
    return None


def _result_path(job: JobRecord | None, key: str) -> Path | None:
    if job is None:
        return None
    value = str(job.result.get(key, "")).strip()
    return Path(value) if value else None


def _is_slide_deck(
    document: LibraryDocument,
    slides: list[dict[str, Any]],
    source_job: JobRecord | None,
) -> bool:
    suffix = document.stored_path.suffix.lower()
    if suffix in {".ppt", ".pptx"}:
        return True
    if suffix != ".pdf" or not slides or "notes" in document.original_name.casefold():
        return False
    normalized = re.sub(r"[^a-z0-9]+", "_", document.original_name.casefold())
    if "slides" in normalized:
        return True
    if source_job is not None:
        run_dir = Path(str(source_job.result.get("run_dir", "")))
        try:
            if any(
                _source_names_match(path.name, document.original_name)
                for path in (run_dir / "input").iterdir()
            ):
                return True
        except OSError:
            pass
    try:
        import fitz

        with fitz.open(document.stored_path) as pdf:
            return len(pdf) == len(slides)
    except Exception:
        return False


def _lecture_slide_bundle(
    library: LibraryStore,
    manager: JobManager,
    document: LibraryDocument,
) -> _LectureSlideBundle | None:
    if document.stored_path.suffix.lower() not in {".pdf", ".ppt", ".pptx"}:
        return None
    related = _related_documents(library, document)
    source_job = _matching_lecture_job(manager, document)
    slides_path = _find_related_artifact(related, ("slides", "extracted")) or _result_path(
        source_job, "slides_path"
    )
    slide_payload = _load_json_file(slides_path)
    slides = [item for item in slide_payload.get("slides", []) if isinstance(item, dict)]
    if not _is_slide_deck(document, slides, source_job):
        return None

    summaries_path = _find_related_artifact(related, ("slide", "summaries")) or _result_path(
        source_job, "slide_summaries_path"
    )
    summaries_payload = _load_json_file(summaries_path)
    summaries = {
        int(item["slide"]): str(item.get("summary", "")).strip()
        for item in summaries_payload.get("slides", [])
        if isinstance(item, dict) and str(item.get("slide", "")).isdigit()
    }
    alignment_path = _find_related_artifact(related, ("alignment",)) or _result_path(
        source_job, "alignment_path"
    )
    alignment_payload = _load_json_file(alignment_path)
    alignment = {
        int(item["slide"]): [value for value in item.get("paragraphs", []) if isinstance(value, dict)]
        for item in alignment_payload.get("slides", [])
        if isinstance(item, dict) and str(item.get("slide", "")).isdigit()
    }
    return _LectureSlideBundle(slides, summaries, alignment, source_job)


def _render_pdf(document: LibraryDocument) -> None:
    """Render a stored PDF without exposing its local filesystem path to the browser."""
    try:
        pdf_data = document.stored_path.read_bytes()
    except OSError as exc:
        st.error(f"Could not open this PDF: {exc}")
        return

    try:
        st.pdf(pdf_data, height=680, key=f"pdf_viewer_{document.id}")
    except Exception:
        st.error(
            "The local PDF viewer is unavailable. Reinstall the project dependencies "
            "with pip install -r requirements.txt, then restart the website."
        )


def _read_text_preview(path: Path, maximum_bytes: int = 750_000) -> tuple[str, bool]:
    with path.open("rb") as source:
        data = source.read(maximum_bytes + 1)
    truncated = len(data) > maximum_bytes
    return data[:maximum_bytes].decode("utf-8", errors="replace"), truncated


def _render_powerpoint_preview(document: LibraryDocument) -> None:
    try:
        from pptx import Presentation

        deck = Presentation(document.stored_path)
        if not deck.slides:
            st.info("This presentation does not contain slides.")
            return
        for slide_number, slide in enumerate(deck.slides, start=1):
            parts = [
                str(shape.text).strip()
                for shape in slide.shapes
                if getattr(shape, "has_text_frame", False) and str(shape.text).strip()
            ]
            title = parts[0].splitlines()[0] if parts else f"Slide {slide_number}"
            with st.expander(f"Slide {slide_number}: {title}", expanded=slide_number == 1):
                st.text("\n\n".join(parts) if parts else "(No extractable text on this slide.)")
    except Exception as exc:
        st.warning(f"Could not preview this PowerPoint file: {exc}")


def _limit_words(text: str, maximum: int) -> str:
    words = text.split()
    if len(words) <= maximum:
        return text.strip()
    return " ".join(words[:maximum]).rstrip(" ,;:") + "…"


def _exact_slide_summary(bundle: _LectureSlideBundle, slide: dict[str, Any]) -> str:
    number = int(slide.get("slide", 0))
    stored = bundle.summaries.get(number, "").strip()
    if stored:
        return _limit_words(stored, 90)

    slide_text = " ".join(str(slide.get("content", "")).split())
    professor_text = " ".join(
        " ".join(str(item.get("text", "")).split())
        for item in bundle.alignment.get(number, [])
        if str(item.get("text", "")).strip()
    )
    parts: list[str] = []
    if slide_text:
        parts.append(_limit_words(slide_text, 60))
    if professor_text and professor_text.casefold() not in slide_text.casefold():
        parts.append("Professor explanation: " + _limit_words(professor_text, 30))
    return " ".join(parts) or "No source-grounded summary is available for this slide."


def _render_current_slide(document: LibraryDocument, slide: dict[str, Any]) -> None:
    number = int(slide.get("slide", 1))
    suffix = document.stored_path.suffix.lower()
    if suffix == ".pdf":
        try:
            import fitz

            with fitz.open(document.stored_path) as pdf:
                page = pdf[number - 1]
                image = page.get_pixmap(matrix=fitz.Matrix(1.45, 1.45), alpha=False).tobytes("png")
            st.image(image, use_container_width=True)
            return
        except Exception as exc:
            st.warning(f"Could not render slide {number}: {exc}")

    preview_path = Path(str(slide.get("preview_image", "")))
    if preview_path.is_file():
        st.image(str(preview_path), use_container_width=True)
        return
    st.markdown(f"#### {slide.get('title', f'Slide {number}')}")
    st.text(str(slide.get("content", "")) or "(No extractable slide text.)")


def _matching_deep_reviews(
    manager: JobManager,
    source_job: JobRecord,
    slide_number: int,
) -> list[JobRecord]:
    matches = [
        job
        for job in manager.list_jobs(limit=500)
        if job.kind == "slide_review"
        and job.payload.get("source_job_id") == source_job.id
        and int(job.payload.get("slide_number", -1)) == slide_number
    ]
    return sorted(matches, key=lambda job: job.created_at, reverse=True)


def _render_deep_review_control(
    manager: JobManager,
    document: LibraryDocument,
    bundle: _LectureSlideBundle,
    slide_number: int,
) -> None:
    source_job = bundle.source_job
    reviews = _matching_deep_reviews(manager, source_job, slide_number) if source_job else []
    latest = reviews[0] if reviews else None
    active = bool(latest and latest.status in {"queued", "running", "waiting", "deferred"})
    if latest and latest.status == "completed":
        markdown_path = Path(str(latest.result.get("markdown_path", "")))
        if markdown_path.is_file():
            with st.expander("Detailed explanation", expanded=True):
                st.markdown(markdown_path.read_text(encoding="utf-8"))
    elif latest and active:
        st.info(f"Deep Review: {latest.status} · {latest.progress}% — {latest.message}")
        if st.button(
            "Refresh review status",
            key=f"refresh_deep_review_{document.id}_{slide_number}",
            use_container_width=True,
        ):
            st.rerun()

    help_text = (
        "Generate a detailed, source-grounded explanation for only this slide."
        if source_job
        else "The completed lecture job for this file could not be located."
    )
    if st.button(
        "Deep Review",
        type="primary",
        disabled=source_job is None or active,
        help=help_text,
        key=f"library_deep_review_{document.id}_{slide_number}",
        use_container_width=True,
    ):
        try:
            review_job = manager.enqueue_slide_review(
                source_job.id,
                slide_number,
                library_subject_id=document.subject_id,
                library_folder_id=document.folder_id,
                lecture_name=str(
                    source_job.result.get("lecture_name", source_job.payload.get("lecture_name", "Lecture"))
                ),
            )
            st.session_state["library_file_notice"] = (
                f"Deep Review for slide {slide_number} was queued as job {review_job.id[:8]}."
            )
            st.rerun()
        except JobError as exc:
            st.error(str(exc))


def _render_slide_document(
    manager: JobManager,
    document: LibraryDocument,
    bundle: _LectureSlideBundle,
) -> None:
    slide_lookup = {int(item["slide"]): item for item in bundle.slides}
    slide_numbers = sorted(slide_lookup)
    selected_number = st.selectbox(
        "Current slide",
        slide_numbers,
        format_func=lambda number: f"Slide {number}: {slide_lookup[number].get('title', '')}",
        key=f"current_library_slide_{document.id}",
    )
    slide = slide_lookup[selected_number]
    st.progress(
        (slide_numbers.index(selected_number) + 1) / len(slide_numbers),
        text=f"Slide {selected_number} of {len(slide_numbers)}",
    )
    slide_column, summary_column = st.columns([1.15, 1], gap="large")
    with slide_column:
        st.markdown(f"#### Slide {selected_number}")
        _render_current_slide(document, slide)
    with summary_column:
        st.markdown("#### Slide summary")
        st.write(_exact_slide_summary(bundle, slide))
        _render_deep_review_control(manager, document, bundle, selected_number)

    with st.expander("Open full document", expanded=False):
        if document.stored_path.suffix.lower() == ".pdf":
            _render_pdf(document)
        else:
            _render_powerpoint_preview(document)


def _render_document_content(document: LibraryDocument) -> None:
    suffix = document.stored_path.suffix.lower()
    try:
        if suffix == ".pdf":
            _render_pdf(document)
        elif suffix in AUDIO_SUFFIXES:
            st.audio(str(document.stored_path), format=document.media_type)
        elif suffix in VIDEO_SUFFIXES:
            st.video(str(document.stored_path), format=document.media_type)
        elif suffix in IMAGE_SUFFIXES:
            st.image(str(document.stored_path), use_container_width=True)
        elif suffix == ".json":
            text, truncated = _read_text_preview(document.stored_path)
            try:
                st.json(json.loads(text), expanded=1)
            except json.JSONDecodeError:
                st.code(text, language="json")
            if truncated:
                st.caption("Preview limited to the first 750 KB.")
        elif suffix in TEXT_PREVIEW_SUFFIXES:
            text, truncated = _read_text_preview(document.stored_path)
            language = suffix.lstrip(".")
            if language in {"md", "markdown"}:
                language = "markdown"
            st.code(text, language=language or None)
            if truncated:
                st.caption("Preview limited to the first 750 KB.")
        elif suffix == ".pptx":
            _render_powerpoint_preview(document)
        elif suffix == ".ppt":
            st.info("Legacy PPT files cannot be previewed directly. Download or convert this file to PPTX/PDF.")
        else:
            st.info("This file type has no inline preview. You can still download or organize the file.")
    except OSError as exc:
        st.error(f"Could not open this file: {exc}")


def _render_file_preview(
    library: LibraryStore,
    manager: JobManager,
    document: LibraryDocument,
    selected_key: str,
    slide_bundle: _LectureSlideBundle | None = None,
) -> None:
    st.markdown(f"### {file_icon(document.original_name)} {document.original_name}")
    st.caption(
        f"📁 {document.folder_name or 'Subject root'} · {format_size(document.size_bytes)} · "
        f"{document.status.replace('_', ' ').title()}"
    )
    if document.extraction_error:
        st.warning(document.extraction_error)

    close_col, rename_col, delete_col = st.columns(3)
    if close_col.button("Close", key=f"close_preview_{document.id}", use_container_width=True):
        st.session_state.pop(selected_key, None)
        st.rerun()
    with rename_col.popover("Rename", use_container_width=True):
        with st.form(f"rename_file_{document.id}"):
            new_name = st.text_input("File name", value=document.original_name)
            rename = st.form_submit_button("Save name", use_container_width=True)
        if rename:
            try:
                updated = library.rename_document(document.id, new_name)
                st.session_state["library_file_notice"] = f"Renamed file to {updated.original_name}."
                st.rerun()
            except LibraryError as exc:
                st.error(str(exc))
    if delete_col.button("Delete", key=f"delete_preview_{document.id}", use_container_width=True):
        st.session_state["library_document_delete_confirmation"] = document.id

    if st.session_state.get("library_document_delete_confirmation") == document.id:
        st.warning(f"Permanently delete '{document.original_name}'?")
        confirm, cancel = st.columns(2)
        if confirm.button("Yes, delete", type="primary", key=f"confirm_delete_{document.id}"):
            try:
                library.delete_document(document.id)
                st.session_state.pop("library_document_delete_confirmation", None)
                st.session_state.pop(selected_key, None)
                st.session_state["library_file_notice"] = f"Deleted {document.original_name}."
                st.rerun()
            except LibraryError as exc:
                st.error(str(exc))
        if cancel.button("Keep file", key=f"cancel_delete_{document.id}"):
            st.session_state.pop("library_document_delete_confirmation", None)
            st.rerun()

    if document.size_bytes <= 100 * 1024 * 1024:
        try:
            st.download_button(
                "Download",
                data=document.stored_path.read_bytes(),
                file_name=document.original_name,
                mime=document.media_type,
                key=f"download_file_{document.id}",
                use_container_width=True,
            )
        except OSError as exc:
            st.error(f"Could not prepare this file for download: {exc}")
    else:
        st.caption("Large file download is disabled in the preview to avoid loading it into memory.")

    st.divider()
    if slide_bundle is not None:
        _render_slide_document(manager, document, slide_bundle)
    else:
        _render_document_content(document)


def _render_folder_creator(library: LibraryStore, subject_id: str, browse_key: str) -> None:
    with st.popover("➕ New folder"):
        with st.form(f"library_create_folder_{subject_id}", clear_on_submit=True):
            name = st.text_input("Folder name", placeholder="e.g. Lecture 03 — Search")
            submitted = st.form_submit_button("Create folder", type="primary", use_container_width=True)
        if submitted:
            try:
                folder = library.create_folder(subject_id, name)
                st.session_state[browse_key] = folder.id
                st.session_state["library_file_notice"] = f"Created {folder.name}."
                st.rerun()
            except LibraryError as exc:
                st.error(str(exc))


def _render_folder_delete(
    library: LibraryStore,
    folder: LibraryFolder,
    browse_key: str,
    selected_key: str,
) -> None:
    confirmation_key = "library_folder_delete_confirmation"
    if st.button("Delete folder", key=f"delete_folder_{folder.id}"):
        st.session_state[confirmation_key] = folder.id

    if st.session_state.get(confirmation_key) != folder.id:
        return
    if folder.document_count:
        st.warning(
            f"Delete '{folder.name}' and all {folder.document_count} file(s) inside it? "
            "This cannot be undone."
        )
    else:
        st.warning(f"Delete the empty folder '{folder.name}'?")
    confirm, cancel, _ = st.columns([1, 1, 3])
    if confirm.button("Yes, delete", type="primary", key=f"confirm_folder_delete_{folder.id}"):
        try:
            library.delete_folder(folder.id, delete_documents=bool(folder.document_count))
            st.session_state.pop(confirmation_key, None)
            st.session_state[browse_key] = ""
            st.session_state.pop(selected_key, None)
            st.session_state["library_file_notice"] = f"Deleted {folder.name}."
            st.rerun()
        except LibraryError as exc:
            st.error(str(exc))
    if cancel.button("Keep folder", key=f"cancel_folder_delete_{folder.id}"):
        st.session_state.pop(confirmation_key, None)
        st.rerun()


def _handle_file_manager_event(
    library: LibraryStore,
    subject_id: str,
    folders: list[LibraryFolder],
    documents: list[LibraryDocument],
    browse_key: str,
    selected_key: str,
    event: dict[str, object] | None,
) -> None:
    if not event:
        return
    event_id = str(event.get("event_id", ""))
    handled_key = f"library_handled_explorer_event_{subject_id}"
    if not event_id or st.session_state.get(handled_key) == event_id:
        return
    st.session_state[handled_key] = event_id

    action = str(event.get("action", ""))
    folder_id = str(event.get("folder_id", ""))
    document_id = str(event.get("document_id", ""))
    folder_lookup = {folder.id: folder for folder in folders}
    document_lookup = {document.id: document for document in documents}

    if action == "browse":
        if folder_id and folder_id not in folder_lookup:
            st.error("That folder no longer exists. Refresh the library and try again.")
            return
        st.session_state[browse_key] = folder_id
        st.session_state.pop(selected_key, None)
        st.rerun()

    if action == "open":
        if document_id not in document_lookup:
            st.error("That file no longer exists. Refresh the library and try again.")
            return
        st.session_state[selected_key] = document_id
        st.rerun()

    if action != "move":
        return
    if document_id not in document_lookup or (folder_id and folder_id not in folder_lookup):
        st.error("That drag-and-drop move is no longer valid. Refresh the library and try again.")
        return

    target_name = folder_lookup[folder_id].name if folder_id else "Subject root"
    try:
        moved = library.move_document_to_folder(document_id, folder_id or None)
        st.session_state.pop(selected_key, None)
        st.session_state["library_file_notice"] = f"Moved {moved.original_name} to {target_name}."
        st.rerun()
    except LibraryError as exc:
        st.error(str(exc))


def render_library(library: LibraryStore, manager: JobManager) -> None:
    st.title("📚 Subject Library")
    st.caption(
        "Browse folders like a desktop file manager, drag files to move them, "
        "and click a file to preview its contents."
    )
    stats = library.stats()
    first, second, third, fourth = st.columns(4)
    first.metric("Subjects", stats["subjects"])
    second.metric("Folders", stats["folders"])
    third.metric("Files", stats["documents"])
    fourth.metric("Searchable", stats["indexed_documents"])

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

    documents_tab, upload_tab, search_tab = st.tabs(["File explorer", "Add files", "Search"])
    with documents_tab:
        folders = library.list_folders(subject.id)
        documents = library.list_documents(subject.id)
        folder_lookup = {folder.id: folder for folder in folders}
        document_lookup = {document.id: document for document in documents}
        browse_key = f"library_browse_folder_{subject.id}"
        selected_key = f"library_selected_file_{subject.id}"
        if st.session_state.get(browse_key, "") not in {"", *folder_lookup}:
            st.session_state[browse_key] = ""
        if st.session_state.get(selected_key) not in document_lookup:
            st.session_state.pop(selected_key, None)
        current_folder_id = st.session_state.get(browse_key, "")
        selected_document_id = st.session_state.get(selected_key)

        notice = st.session_state.pop("library_file_notice", "")
        if notice:
            st.success(notice)

        control_left, control_right, control_space = st.columns([1, 1, 4])
        with control_left:
            _render_folder_creator(library, subject.id, browse_key)
        if current_folder_id and current_folder_id in folder_lookup:
            with control_right:
                _render_folder_delete(
                    library,
                    folder_lookup[current_folder_id],
                    browse_key,
                    selected_key,
                )
        control_space.caption("Folders stay visible as drop targets. Click a file once to open its preview.")

        selected_document = document_lookup.get(selected_document_id) if selected_document_id else None
        slide_bundle = (
            _lecture_slide_bundle(library, manager, selected_document)
            if selected_document is not None
            else None
        )
        explorer_column, preview_column = st.columns([1, 3] if slide_bundle else [3, 2], gap="large")
        with explorer_column:
            manager_event = render_file_manager(
                folders,
                documents,
                current_folder_id=current_folder_id or None,
                selected_document_id=selected_document_id,
                key=f"library_explorer_{subject.id}",
            )
        with preview_column:
            if selected_document_id and selected_document_id in document_lookup:
                _render_file_preview(
                    library,
                    manager,
                    document_lookup[selected_document_id],
                    selected_key,
                    slide_bundle,
                )
            else:
                st.info("Click a file icon to preview its contents here.")

        _handle_file_manager_event(
            library,
            subject.id,
            folders,
            documents,
            browse_key,
            selected_key,
            manager_event,
        )
    with upload_tab:
        render_upload_panel(library, [subject], "library")
    with search_tab:
        query = st.text_input("Search this subject", placeholder="Enter a file name, topic, definition, or phrase")
        if query.strip():
            render_search_results(library.search_documents(query, subject_id=subject.id, limit=12))
