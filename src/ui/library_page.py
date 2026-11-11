"""Subject-library page."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import hashlib
import html
import json
from pathlib import Path
import re
import tempfile
from typing import Any

import streamlit as st
import streamlit.components.v1 as components
from streamlit import runtime

from ..jobs import PRIORITIES, JobError, JobManager, JobRecord
from ..library import LibraryDocument, LibraryError, LibraryFolder, LibraryStore
from ..voice import (
    VoiceNarrationError,
    install_default_voice,
    local_voice_options,
    neural_voice_setup_message,
    prepare_narration,
    synthesize_speech,
    wav_duration_seconds,
)
from .audio_transcript_component import render_audio_transcript
from .common import format_size, render_preview_image, render_search_results, subject_lookup
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
# The explorer reports its 430px CSS minimum plus a 4px iframe allowance.
# Keep the adjacent preview on that same baseline.
INLINE_PREVIEW_HEIGHT = 434
INLINE_PREVIEW_CONTENT_HEIGHT = 390
INLINE_PDF_RENDER_SCALE = 1.75
DIRECT_DROP_MAX_FILE_BYTES = 64 * 1024 * 1024
DIRECT_DROP_MAX_TOTAL_BYTES = 96 * 1024 * 1024


@dataclass(frozen=True)
class _LectureSlideBundle:
    slides: list[dict[str, Any]]
    summaries: dict[int, str]
    alignment: dict[int, list[dict[str, Any]]]
    source_job: JobRecord | None
    saved_translations: dict[int, dict[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class _LectureAudioBundle:
    paragraphs: list[dict[str, Any]]
    source_job: JobRecord | None


_INTERNAL_LECTURE_ARTIFACTS = (
    "_transcript_raw",
    "_transcript_cleaned",
    "_slides_extracted",
    "_slide_summaries",
    "_alignment",
    "_quality_report",
    "_manifest",
)


def _is_internal_lecture_artifact_name(filename: str) -> bool:
    """Recognize generated evidence that belongs behind a lecture preview."""
    path = Path(filename)
    stem = path.stem.casefold()
    normalized = re.sub(r"[^a-z0-9]+", "_", path.stem.casefold()).strip("_")
    translated_output = path.suffix.lower() in {".md", ".markdown", ".pdf"} and (
        stem.startswith("notes.") or re.match(r"^slide_[0-9]+\.", stem) is not None
    )
    return (
        "transcript" in normalized and path.suffix.lower() in {".json", ".txt"}
    ) or any(normalized.endswith(token.strip("_")) for token in _INTERNAL_LECTURE_ARTIFACTS) or (
        normalized.endswith("_notes") and path.suffix.lower() in {".md", ".markdown"}
    ) or translated_output or (
        "_deep_review_" in f"_{normalized}_" and path.suffix.lower() in {".md", ".markdown"}
    )


def _is_internal_lecture_artifact(document: LibraryDocument) -> bool:
    """Hide processing evidence from the learner-facing file explorer."""
    return _is_internal_lecture_artifact_name(document.original_name)


def _visible_documents(documents: list[LibraryDocument]) -> list[LibraryDocument]:
    return [document for document in documents if not _is_internal_lecture_artifact(document)]


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


def _job_input_files(manager: JobManager, job: JobRecord) -> list[Path]:
    """Return the retained source files for a job without trusting display metadata."""
    roots: list[Path] = []
    run_dir = str(job.result.get("run_dir", "")).strip()
    if run_dir:
        roots.append(Path(run_dir) / "input")
    # Inputs are retained with every queued job.  A completed run may be moved
    # or pruned, so this is the durable source of truth for this association.
    roots.append(manager.root / job.id / "input")

    files: list[Path] = []
    visited: set[Path] = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in visited or not resolved.is_dir():
            continue
        visited.add(resolved)
        try:
            files.extend(path for path in resolved.iterdir() if path.is_file())
        except OSError:
            continue
    return files


def _matching_lecture_job(manager: JobManager, document: LibraryDocument) -> JobRecord | None:
    """Resolve a library file to its lecture without crossing subject boundaries.

    Folder and lecture titles are user-facing labels, not stable identifiers:
    many subjects legitimately have a folder called ``Lecture 01``.  The source
    file checksum is therefore the required association.  When provenance
    cannot be verified, this returns no job so an uncertain match never shows
    the explanation from a different lecture.
    """
    candidates = [
        job
        for job in manager.list_jobs(limit=500)
        if job.kind == "lecture"
        and job.status == "completed"
        and str(job.payload.get("subject_id") or "") == document.subject_id
    ]
    if not candidates:
        return None

    # A stored job input has the same immutable checksum as the library file.
    # Check it before labels or folder identity, including when a user renamed
    # the visible file after import.
    content_matches = [
        job
        for job in candidates
        if any(_matches_document_content(path, document) for path in _job_input_files(manager, job))
    ]
    if len(content_matches) == 1:
        return content_matches[0]
    if len(content_matches) > 1:
        # Reprocessing the exact same source can produce several valid jobs;
        # use the latest one, all of which describe the same source document.
        return max(content_matches, key=lambda job: job.created_at)
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


def _translated_slide_sections(markdown: str) -> dict[int, str]:
    """Read explicit slide numbers, including the existing Chinese note headings."""
    headings = list(re.finditer(r"^##[ \t]+(.+)$", markdown, re.MULTILINE))
    sections: dict[int, str] = {}
    for index, heading in enumerate(headings):
        match = re.match(
            r"(?:Slide\s+|幻灯片\s*|幻燈片\s*|投影片\s*|第\s*)([0-9]+)(?:\s*[：:页頁張张]|\b)",
            heading.group(1), re.IGNORECASE,
        )
        if not match:
            continue
        number = int(match.group(1))
        end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
        section = markdown[heading.start():end].strip()
        if number in sections:
            return {}  # Ambiguous numbering must never attach the wrong content.
        if markdown[heading.end():end].strip():
            sections[number] = section
    return sections


def _saved_lecture_translations(
    related: list[LibraryDocument], source_job: JobRecord | None,
) -> dict[int, dict[str, str]]:
    candidates: list[tuple[Path, str]] = []
    if source_job:
        source_path = _result_path(source_job, "markdown_path")
        if source_path and source_path.is_file():
            for metadata_path in sorted((source_path.parent / "translations").glob("*.md.metadata.json")):
                metadata = _load_json_file(metadata_path)
                path = metadata_path.with_name(metadata_path.name.removesuffix(".metadata.json"))
                language = str(metadata.get("target_language", "")).strip()
                if language and path.is_file():
                    candidates.append((path, language))
    # Library copies remain usable when a run folder has been moved or removed.
    for document in related:
        match = re.search(r"Notes\.([\w-]+)\.md$", document.original_name, re.IGNORECASE)
        if match:
            slug = match.group(1).lower()
            language = {"chinese_simplified": "Chinese (Simplified)",
                        "chinese_traditional": "Chinese (Traditional)"}.get(slug, slug.replace("_", " ").title())
            candidates.append((document.stored_path, language))
    translations: dict[int, dict[str, str]] = {}
    for path, language in candidates:
        try:
            sections = _translated_slide_sections(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
        for number, text in sections.items():
            translations.setdefault(number, {}).setdefault(language, text)
    return translations


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
    if not slides or not _is_slide_deck(document, slides, source_job):
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
    return _LectureSlideBundle(slides, summaries, alignment, source_job, _saved_lecture_translations(related, source_job))


def _lecture_audio_bundle(
    library: LibraryStore,
    manager: JobManager,
    document: LibraryDocument,
) -> _LectureAudioBundle | None:
    if document.stored_path.suffix.lower() not in AUDIO_SUFFIXES:
        return None
    related = _related_documents(library, document)
    source_job = _matching_lecture_job(manager, document)
    transcript_path = next(
        (
            item.stored_path
            for item in related
            if "transcript_cleaned" in re.sub(
                r"[^a-z0-9]+", "_", item.original_name.casefold()
            ).strip("_")
            and item.stored_path.suffix.lower() == ".json"
        ),
        None,
    ) or _result_path(source_job, "transcript_path")
    payload = _load_json_file(transcript_path)
    metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}
    transcript_kind = str(metadata.get("transcript_kind", "")).casefold()
    cleanup_enabled = True
    if source_job is not None and isinstance(source_job.payload.get("config"), dict):
        cleanup_enabled = bool(source_job.payload["config"].get("enable_transcript_cleanup", True))
    if transcript_kind and transcript_kind != "cleaned":
        return _LectureAudioBundle([], source_job)
    if source_job is not None and not cleanup_enabled:
        return _LectureAudioBundle([], source_job)
    paragraphs = [
        item
        for item in payload.get("paragraphs", [])
        if isinstance(item, dict) and str(item.get("text", "")).strip()
    ]
    return _LectureAudioBundle(paragraphs, source_job)


@st.cache_data(show_spinner=False)
def _pdf_page_count(path: str, modified_ns: int) -> int:
    """Return a PDF's page count, invalidating the cache when it changes."""
    del modified_ns
    import fitz

    with fitz.open(path) as pdf:
        return len(pdf)


@st.cache_data(show_spinner=False)
def _pdf_page_image(path: str, modified_ns: int, page_number: int) -> bytes:
    """Rasterize one page for a stable, immediately-sized inline preview."""
    del modified_ns
    import fitz

    with fitz.open(path) as pdf:
        page = pdf[page_number - 1]
        return page.get_pixmap(
            matrix=fitz.Matrix(INLINE_PDF_RENDER_SCALE, INLINE_PDF_RENDER_SCALE),
            alpha=False,
        ).tobytes("png")


def _locator_number(locator: str, kind: str) -> int | None:
    """Return the page or slide number encoded in an indexed chunk locator."""
    match = re.search(rf"\b{re.escape(kind)}\s+(\d+)\b", locator, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _render_stable_pdf_pages(document: LibraryDocument) -> None:
    """Render PDF pages without the viewer iframe's provisional dimensions."""
    modified_ns = document.stored_path.stat().st_mtime_ns
    page_count = _pdf_page_count(str(document.stored_path), modified_ns)
    if page_count < 1:
        st.warning("This PDF does not contain any pages.")
        return

    page_key = f"library_pdf_page_{document.id}"
    try:
        page_number = int(st.session_state.get(page_key, 1))
    except (TypeError, ValueError):
        page_number = 1
    page_number = min(max(page_number, 1), page_count)
    st.session_state[page_key] = page_number

    if page_count > 1:
        navigation = st.container(key="library_pdf_navigation")
        previous, label, next_page = navigation.columns([1, 5, 1], vertical_alignment="center")
        if previous.button(
            "◀",
            disabled=page_number == 1,
            help="Previous page",
            key=f"previous_library_pdf_page_{document.id}",
            width="stretch",
        ):
            st.session_state[page_key] = page_number - 1
            st.rerun()
        label.caption(f"Page {page_number} of {page_count}")
        if next_page.button(
            "▶",
            disabled=page_number == page_count,
            help="Next page",
            key=f"next_library_pdf_page_{document.id}",
            width="stretch",
        ):
            st.session_state[page_key] = page_number + 1
            st.rerun()

    # Safari can keep an image toolbar's wrapper at its intrinsic width.
    # Scope the width override to PDF pages so the rendered page always fills
    # either the inline panel or the full-page view.
    st.html(
        """<style>
        .st-key-library_pdf_page_image [data-testid="stFullScreenFrame"] > div:has(> [data-testid="stImage"]) {
          width: 100%;
        }
        .st-key-library_pdf_page_image [data-testid="stImage"],
        .st-key-library_pdf_page_image [data-testid="stImageContainer"] {
          width: 100%;
        }
        .st-key-library_pdf_page_image [data-testid="stImageContainer"] > img {
          width: 100% !important; height: auto;
        }
        </style>""",
    )
    with st.container(key="library_pdf_page_image"):
        render_preview_image(
            _pdf_page_image(str(document.stored_path), modified_ns, page_number),
            width="stretch",
        )


def _render_scrollable_pdf_pages(
    document: LibraryDocument,
    *,
    focus_page: int | None = None,
) -> None:
    """Render a full PDF as centered pages that follow the browser scroll."""
    modified_ns = document.stored_path.stat().st_mtime_ns
    page_count = _pdf_page_count(str(document.stored_path), modified_ns)
    if page_count < 1:
        st.warning("This PDF does not contain any pages.")
        return

    if focus_page is not None:
        focus_page = min(max(focus_page, 1), page_count)
        st.markdown(f"#### Search match · Page {focus_page}")
        with st.container(border=True):
            render_preview_image(
                _pdf_page_image(str(document.stored_path), modified_ns, focus_page),
                width="stretch",
            )
        st.caption("The complete document follows below.")

    # The generated lecture-notes PDF is intended to be read like a document,
    # rather than one slide at a time. A middle column keeps every portrait page
    # centered, and page images have their dimensions before the browser paints
    # them, so this avoids the PDF viewer's delayed resize.
    st.html(
        """<style>
        .st-key-library_pdf_scroll_pages [data-testid="stFullScreenFrame"] > div:has(> [data-testid="stImage"]) {
          width: 100%;
        }
        .st-key-library_pdf_scroll_pages [data-testid="stImage"],
        .st-key-library_pdf_scroll_pages [data-testid="stImageContainer"] {
          width: 100%;
        }
        .st-key-library_pdf_scroll_pages [data-testid="stImageContainer"] > img {
          width: 100% !important; height: auto; display: block;
        }
        </style>""",
    )
    _, pages, _ = st.columns([1, 2.25, 1])
    with pages:
        with st.container(key="library_pdf_scroll_pages"):
            for page_number in range(1, page_count + 1):
                render_preview_image(
                    _pdf_page_image(str(document.stored_path), modified_ns, page_number),
                    width="stretch",
                )


def _render_pdf(
    document: LibraryDocument,
    *,
    full_page: bool = False,
    focus_locator: str = "",
) -> None:
    """Render a PDF without the viewer iframe's delayed page resize."""
    try:
        if full_page:
            _render_scrollable_pdf_pages(
                document,
                focus_page=_locator_number(focus_locator, "page"),
            )
        else:
            _render_stable_pdf_pages(document)
    except (OSError, ValueError, RuntimeError) as exc:
        st.error(f"Could not render this PDF preview: {exc}")


@st.cache_data(show_spinner=False)
def _pdf_page_text(path: str, modified_ns: int, page_number: int) -> str:
    """Extract a PDF page's readable content for local narration."""
    del modified_ns
    import fitz

    with fitz.open(path) as pdf:
        return str(pdf[page_number - 1].get_text("text"))


@st.cache_data(show_spinner=False)
def _pdf_full_text(path: str, modified_ns: int) -> str:
    """Extract the complete text of a generated lecture-summary PDF."""
    del modified_ns
    import fitz

    with fitz.open(path) as pdf:
        return "\n\n".join(str(page.get_text("text")) for page in pdf)


def _presentation_image_url(image: bytes, *, key: str, page_number: int) -> str:
    """Register a rendered page with Streamlit's media server for iframe use."""
    if runtime.exists():
        try:
            return str(
                runtime.get_instance().media_file_mgr.add(
                    image,
                    "image/png",
                    f"elastic-presentation-{key}-{page_number}",
                )
            )
        except Exception:
            pass
    encoded = base64.b64encode(image).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _render_elastic_presentation(
    pages: list[dict[str, Any]],
    *,
    page_label: str,
    deck_name: str,
    initial_page: int,
    key: str,
) -> None:
    """Render a scroll-driven page reader with synchronized explanatory text."""
    if not pages:
        return

    slides = [
        {
            "title": str(page.get("title", "")).strip() or f"{page_label} {number}",
            "content": str(page.get("content", "")).strip(),
            "image": _presentation_image_url(
                page["image"], key=key, page_number=number
            ) if page.get("image") else "",
        }
        for number, page in enumerate(pages, start=1)
    ]
    slides_json = json.dumps(slides, ensure_ascii=False).replace("</", "<\\/")
    start_index = min(max(initial_page - 1, 0), len(slides) - 1)
    template = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root { color-scheme: light dark; --ink:#20242c; --muted:#727b8a; --line:#d9dee8;
      --panel:#fff; --soft:#f3f5f9; --accent:#d84f45; --accent-soft:#fbe9e7;
      --stage:#161a22; --stage-line:#2b313d; --shadow:0 20px 55px rgba(18,25,38,.13); }
    * { box-sizing:border-box; }
    html, body { width:100%; height:100%; margin:0; overflow:hidden; background:transparent;
      color:var(--ink); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    button { font:inherit; }
    .viewer { height:calc(100vh - 14px); min-height:610px; max-height:890px; display:flex;
      flex-direction:column; overflow:hidden; border:1px solid var(--line); border-radius:18px;
      background:var(--panel); box-shadow:var(--shadow); outline:none; user-select:none; }
    .topbar { min-height:57px; padding:0 22px; display:flex; align-items:center; gap:14px;
      border-bottom:1px solid var(--line); }
    .brand-mark { width:28px; height:28px; display:grid; place-items:center; flex:none;
      border-radius:9px; color:#fff; background:var(--accent); font-size:12px; font-weight:800; }
    .brand-copy { min-width:0; }
    .eyebrow { display:block; color:var(--muted); font-size:10px; line-height:1.25; font-weight:800;
      letter-spacing:.11em; text-transform:uppercase; }
    .deck-name { overflow:hidden; color:var(--ink); font-size:13px; font-weight:650;
      text-overflow:ellipsis; white-space:nowrap; }
    .top-meta { margin-left:auto; color:var(--muted); font-size:12px; font-weight:650;
      font-variant-numeric:tabular-nums; white-space:nowrap; }
    .progress-track { height:3px; flex:none; overflow:hidden; background:var(--soft); }
    .progress-fill { height:100%; width:0; background:var(--accent); transition:width .42s cubic-bezier(.2,.8,.2,1); }
    .reader { flex:1; min-height:0; display:grid; grid-template-columns:minmax(0,1.52fr) minmax(290px,.88fr);
      gap:18px; padding:18px; }
    .visual-column { min-width:0; min-height:0; display:flex; flex-direction:column; gap:12px; }
    .stage { position:relative; flex:1; min-height:0; display:grid; place-items:center; overflow:hidden;
      padding:clamp(16px,3vw,32px); border:1px solid var(--stage-line); border-radius:15px;
      background:radial-gradient(ellipse at 50% 35%,#252b37 0%,#191e27 58%,#12161d 100%);
      perspective:1000px; touch-action:none; }
    .stage-visual { width:100%; height:100%; display:grid; place-items:center; transform-origin:center;
      transform:translate3d(0,var(--pull-y,0px),0) scale(var(--pull-x,1),var(--pull-y-scale,1));
      transition:transform .12s cubic-bezier(.2,.8,.2,1), opacity .36s cubic-bezier(.2,.8,.2,1);
      will-change:transform,opacity; }
    .stage img { display:block; max-width:100%; max-height:100%; object-fit:contain; border-radius:5px;
      box-shadow:0 16px 44px rgba(0,0,0,.3); }
    .slide-fallback { width:min(100%,740px); height:min(100%,430px); overflow:hidden; padding:30px;
      border:1px solid #454d5a; border-radius:10px; background:#fff; color:#20242c; white-space:pre-wrap;
      box-shadow:0 16px 44px rgba(0,0,0,.25); }
    .slide-fallback strong { display:block; margin-bottom:12px; font-size:24px; }
    .controls { height:44px; flex:none; display:flex; align-items:center; justify-content:space-between; }
    .control-group { display:flex; align-items:center; gap:8px; }
    .nav-button { width:38px; height:38px; display:grid; place-items:center; border:1px solid var(--line);
      border-radius:11px; background:var(--panel); color:var(--ink); cursor:pointer;
      transition:background .15s ease, border-color .15s ease, transform .15s ease; }
    .nav-button:hover:not(:disabled) { transform:translateY(-1px); border-color:var(--accent); background:var(--accent-soft); }
    .nav-button:disabled { opacity:.38; cursor:default; }
    .nav-button svg { width:16px; height:16px; }
    .step-label { min-width:72px; text-align:center; color:var(--muted); font-size:11px;
      font-weight:700; font-variant-numeric:tabular-nums; }
    .copy-panel { min-width:0; min-height:0; display:flex; flex-direction:column; justify-content:space-between;
      padding:25px 24px 20px; overflow:hidden; border:1px solid var(--line); border-radius:15px;
      background:linear-gradient(150deg,var(--panel),var(--soft)); }
    .copy-block { min-height:0; flex:1; display:flex; flex-direction:column; overflow:hidden;
      transition:transform .36s cubic-bezier(.2,.8,.2,1), opacity .28s ease; }
    .copy-kicker { display:flex; align-items:center; justify-content:space-between; gap:12px; }
    .copy-number { color:var(--accent); font-size:11px; font-weight:800; font-variant-numeric:tabular-nums; }
    h1 { margin:18px 0 14px; color:var(--ink); font-size:clamp(22px,2.6vw,34px); line-height:1.12;
      letter-spacing:-.035em; font-weight:720; }
    .explanation { min-height:0; flex:1; overflow:auto; padding-right:5px; color:var(--muted); font-size:14px;
      line-height:1.72; white-space:pre-wrap; overscroll-behavior:contain; user-select:text; }
    .copy-footer { flex:none; display:flex; align-items:center; gap:8px; margin-top:20px; padding-top:15px;
      border-top:1px solid var(--line); color:var(--muted); font-size:11px; }
    .scroll-icon { width:16px; height:23px; display:grid; place-items:center; border:1px solid var(--muted);
      border-radius:9px; font-size:11px; line-height:1; }
    .hint { margin-left:auto; color:var(--muted); font-size:10px; }
    .is-leaving-next .stage-visual { opacity:0; transform:translate3d(0,-54px,0) scale(.975); }
    .is-leaving-prev .stage-visual { opacity:0; transform:translate3d(0,54px,0) scale(.975); }
    .is-leaving-next .copy-block { opacity:0; transform:translate3d(0,-13px,0); }
    .is-leaving-prev .copy-block { opacity:0; transform:translate3d(0,13px,0); }
    .is-entering-next .stage-visual { opacity:0; transform:translate3d(0,54px,0) scale(.975); }
    .is-entering-prev .stage-visual { opacity:0; transform:translate3d(0,-54px,0) scale(.975); }
    .is-entering-next .copy-block { opacity:0; transform:translate3d(0,13px,0); }
    .is-entering-prev .copy-block { opacity:0; transform:translate3d(0,-13px,0); }
    .no-motion .stage-visual, .no-motion .copy-block { transition:none !important; }
    @media (max-width:760px) {
      .viewer { min-height:650px; border-radius:13px; }
      .topbar { min-height:50px; padding:0 14px; }
      .reader { grid-template-columns:minmax(0,1fr); grid-template-rows:minmax(260px,1.25fr) minmax(180px,.8fr);
        gap:10px; padding:11px; }
      .stage { padding:14px; border-radius:12px; }
      .copy-panel { padding:16px 17px 12px; border-radius:12px; }
      .copy-block { overflow:hidden; }
      h1 { margin:10px 0 8px; font-size:21px; }
      .explanation { font-size:12px; line-height:1.5; }
      .copy-footer { margin-top:10px; padding-top:9px; }
      .hint { display:none; }
    }
    @media (prefers-reduced-motion:reduce) {
      *, *::before, *::after { scroll-behavior:auto !important; transition-duration:.12s !important; }
    }
    @media (prefers-color-scheme:dark) {
      :root { --ink:#f2f4f8; --muted:#a1a9b7; --line:#343a46; --panel:#191c23;
        --soft:#222630; --accent:#ff8177; --accent-soft:#362526; --shadow:0 20px 55px rgba(0,0,0,.25); }
      .stage { background:radial-gradient(ellipse at 50% 35%,#2a303c 0%,#1e222b 58%,#15181f 100%); }
    }
  </style>
</head>
<body>
  <main id="viewer" class="viewer" tabindex="0" aria-label="Scroll driven presentation viewer">
    <header class="topbar">
      <div class="brand-mark" aria-hidden="true">↟</div>
      <div class="brand-copy"><span class="eyebrow">Presentation reader</span><div class="deck-name">__DECK__</div></div>
      <div id="topMeta" class="top-meta"></div>
    </header>
    <div class="progress-track" aria-hidden="true"><div id="progress" class="progress-fill"></div></div>
    <section class="reader">
      <div class="visual-column">
        <div id="stage" class="stage" aria-label="Presentation page">
          <div class="stage-visual">
            <img id="pageImage" alt="" />
            <div id="fallback" class="slide-fallback" hidden><strong></strong><span></span></div>
          </div>
        </div>
        <div class="controls">
          <div class="step-label" id="stepLabel"></div>
          <div class="control-group">
            <button class="nav-button" id="previous" type="button" aria-label="Previous __SINGULAR__">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg>
            </button>
            <button class="nav-button" id="next" type="button" aria-label="Next __SINGULAR__">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>
            </button>
          </div>
        </div>
      </div>
      <aside class="copy-panel" aria-label="__SINGULAR__ explanation">
        <div id="copyBlock" class="copy-block" aria-live="polite">
          <div class="copy-kicker"><span class="eyebrow">What to notice</span><span id="copyNumber" class="copy-number"></span></div>
          <h1 id="pageTitle"></h1>
          <div id="explanation" class="explanation"></div>
        </div>
        <div class="copy-footer"><span class="scroll-icon" aria-hidden="true">↕</span><span>Scroll to move through the deck</span><span class="hint">← → keys</span></div>
      </aside>
    </section>
  </main>
  <script>
    const pages = __PAGES__;
    const label = __LABEL_JSON__;
    const viewer = document.getElementById("viewer");
    const stage = document.getElementById("stage");
    const image = document.getElementById("pageImage");
    const fallback = document.getElementById("fallback");
    const titleNode = document.getElementById("pageTitle");
    const explanation = document.getElementById("explanation");
    const copyBlock = document.getElementById("copyBlock");
    const progress = document.getElementById("progress");
    const topMeta = document.getElementById("topMeta");
    const stepLabel = document.getElementById("stepLabel");
    const copyNumber = document.getElementById("copyNumber");
    const previousButton = document.getElementById("previous");
    const nextButton = document.getElementById("next");
    const THRESHOLD = 118;
    const TRANSITION_MS = 420;
    const GESTURE_QUIET_MS = 230;
    let activeIndex = __START__;
    let pullDistance = 0;
    let transitioning = false;
    let consumedGesture = false;
    let lastInputAt = 0;
    let springTimer = 0;
    let gestureTimer = 0;
    let pointerStart = null;

    function setPull(distance) {
      pullDistance = Math.max(-THRESHOLD * 1.25, Math.min(THRESHOLD * 1.25, distance));
      const atEdge = (activeIndex === 0 && pullDistance < 0) ||
        (activeIndex === pages.length - 1 && pullDistance > 0);
      const resistance = atEdge ? 0.2 : 1;
      const amount = Math.max(-1, Math.min(1, pullDistance / THRESHOLD));
      const eased = Math.sign(amount) * (1 - Math.exp(-Math.abs(amount) * 1.1));
      stage.style.setProperty("--pull-y", `${-eased * 24 * resistance}px`);
      stage.style.setProperty("--pull-x", `${1 - Math.abs(eased) * .012}`);
      stage.style.setProperty("--pull-y-scale", `${1 + Math.abs(eased) * .018}`);
    }

    function springBack() {
      window.clearTimeout(springTimer);
      stage.style.setProperty("--pull-y", "0px");
      stage.style.setProperty("--pull-x", "1");
      stage.style.setProperty("--pull-y-scale", "1");
      pullDistance = 0;
    }

    function updatePage() {
      const page = pages[activeIndex];
      const number = String(activeIndex + 1).padStart(2, "0");
      const total = String(pages.length).padStart(2, "0");
      titleNode.textContent = page.title;
      explanation.textContent = page.content || `No selectable text is available on this ${label.toLowerCase()}.`;
      copyNumber.textContent = `${label.toUpperCase()} ${number}`;
      topMeta.textContent = `${label} ${number} / ${total}`;
      stepLabel.textContent = `${number} / ${total}`;
      progress.style.width = `${((activeIndex + 1) / pages.length) * 100}%`;
      previousButton.disabled = activeIndex === 0;
      nextButton.disabled = activeIndex === pages.length - 1;
      image.alt = `${label} ${activeIndex + 1}: ${page.title}`;
      if (page.image) {
        image.hidden = false;
        fallback.hidden = true;
        if (image.src !== page.image) image.src = page.image;
      } else {
        image.removeAttribute("src");
        image.hidden = true;
        fallback.hidden = false;
        fallback.querySelector("strong").textContent = page.title;
        fallback.querySelector("span").textContent = page.content || "No preview image is available.";
      }
      explanation.scrollTop = 0;
    }

    function clearMotionClasses() {
      viewer.classList.remove("is-leaving-next", "is-leaving-prev", "is-entering-next", "is-entering-prev", "no-motion");
    }

    function transition(direction) {
      if (transitioning) return;
      const nextIndex = activeIndex + direction;
      if (nextIndex < 0 || nextIndex >= pages.length) {
        springBack();
        return;
      }
      transitioning = true;
      consumedGesture = true;
      viewer.classList.add(direction > 0 ? "is-leaving-next" : "is-leaving-prev");
      window.setTimeout(() => {
        viewer.classList.add("no-motion");
        viewer.classList.remove("is-leaving-next", "is-leaving-prev");
        viewer.classList.add(direction > 0 ? "is-entering-next" : "is-entering-prev");
        activeIndex = nextIndex;
        updatePage();
        void viewer.offsetWidth;
        viewer.classList.remove("no-motion");
        window.requestAnimationFrame(() => {
          viewer.classList.remove("is-entering-next", "is-entering-prev");
        });
      }, 175);
      window.setTimeout(() => {
        clearMotionClasses();
        transitioning = false;
        springBack();
        releaseGestureAfterQuiet();
      }, TRANSITION_MS);
    }

    function releaseGestureAfterQuiet() {
      window.clearTimeout(gestureTimer);
      const elapsed = performance.now() - lastInputAt;
      const remaining = Math.max(0, GESTURE_QUIET_MS - elapsed);
      gestureTimer = window.setTimeout(() => {
        if (!transitioning && performance.now() - lastInputAt >= GESTURE_QUIET_MS) {
          consumedGesture = false;
        } else if (!transitioning) {
          releaseGestureAfterQuiet();
        }
      }, remaining + 5);
    }

    function settleAfterIdle() {
      window.clearTimeout(springTimer);
      springTimer = window.setTimeout(() => {
        if (transitioning) return;
        if (Math.abs(pullDistance) >= THRESHOLD) transition(Math.sign(pullDistance));
        else springBack();
      }, 230);
    }

    function scrollExplanationCanContinue(target, direction) {
      const scroller = target.closest(".explanation");
      if (!scroller || scroller.scrollHeight <= scroller.clientHeight + 2) return false;
      return direction > 0
        ? scroller.scrollTop + scroller.clientHeight < scroller.scrollHeight - 2
        : scroller.scrollTop > 2;
    }

    viewer.addEventListener("wheel", (event) => {
      const multiplier = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 380 : 1;
      const delta = event.deltaY * multiplier;
      if (scrollExplanationCanContinue(event.target, delta)) return;
      event.preventDefault();
      lastInputAt = performance.now();
      if (transitioning || consumedGesture) {
        if (consumedGesture) releaseGestureAfterQuiet();
        return;
      }
      pullDistance += delta;
      setPull(pullDistance);
      settleAfterIdle();
    }, { passive:false });

    stage.addEventListener("pointerdown", (event) => {
      if (event.pointerType === "mouse" || transitioning) return;
      pointerStart = { id:event.pointerId, y:event.clientY };
      stage.setPointerCapture(event.pointerId);
    });
    stage.addEventListener("pointermove", (event) => {
      if (!pointerStart || pointerStart.id !== event.pointerId || transitioning) return;
      event.preventDefault();
      const delta = pointerStart.y - event.clientY;
      pullDistance = delta;
      setPull(pullDistance);
    });
    stage.addEventListener("pointerup", (event) => {
      if (!pointerStart || pointerStart.id !== event.pointerId) return;
      pointerStart = null;
      lastInputAt = performance.now();
      if (Math.abs(pullDistance) >= THRESHOLD) transition(Math.sign(pullDistance));
      else { springBack(); settleAfterIdle(); }
    });
    stage.addEventListener("pointercancel", () => { pointerStart = null; springBack(); });

    previousButton.addEventListener("click", () => transition(-1));
    nextButton.addEventListener("click", () => transition(1));
    window.addEventListener("keydown", (event) => {
      if (event.target.closest?.("button, input, textarea, select") || window.getSelection()?.toString()) return;
      if (["ArrowDown", "PageDown", " ", "ArrowRight"].includes(event.key)) {
        event.preventDefault(); transition(1);
      } else if (["ArrowUp", "PageUp", "ArrowLeft"].includes(event.key)) {
        event.preventDefault(); transition(-1);
      }
    });
    updatePage();
  </script>
</body>
</html>'''
    rendered = (
        template.replace("__PAGES__", slides_json)
        .replace("__START__", str(start_index))
        .replace("__DECK__", html.escape(deck_name))
        .replace("__SINGULAR__", html.escape(page_label.lower()))
        .replace("__LABEL_JSON__", json.dumps(page_label.lower()))
    )
    components.html(rendered, height=850, scrolling=False)


def _render_fullscreen_pdf_reader(document: LibraryDocument, *, focus_locator: str = "") -> None:
    """Show a scroll-driven PDF page beside its selectable explanation."""
    modified_ns = document.stored_path.stat().st_mtime_ns
    page_count = _pdf_page_count(str(document.stored_path), modified_ns)
    if page_count < 1:
        st.warning("This PDF does not contain any pages.")
        return

    page_key = f"fullscreen_library_pdf_page_{document.id}"
    focus_page = _locator_number(focus_locator, "page")
    focus_key = f"fullscreen_library_pdf_focus_{document.id}_{focus_page}"
    if focus_page and not st.session_state.get(focus_key, False):
        st.session_state[page_key] = focus_page
        st.session_state[focus_key] = True
    try:
        page_number = int(st.session_state.get(page_key, 1))
    except (TypeError, ValueError):
        page_number = 1
    page_number = min(max(page_number, 1), page_count)
    st.session_state[page_key] = page_number

    pages = []
    for number in range(1, page_count + 1):
        text = _pdf_page_text(str(document.stored_path), modified_ns, number).strip()
        pages.append(
            {
                "title": next(
                    (line.strip() for line in text.splitlines() if line.strip()),
                    f"Page {number}",
                ),
                "content": text,
                "image": _pdf_page_image(str(document.stored_path), modified_ns, number),
            }
        )
    _render_elastic_presentation(
        pages,
        page_label="Page",
        deck_name=document.original_name,
        initial_page=page_number,
        key=f"pdf-{document.id}",
    )


@st.cache_data(show_spinner=False)
def _powerpoint_slide_text_records(path: str, modified_ns: int) -> list[dict[str, str]]:
    """Extract readable text for PPT/PPTX slides; convert legacy PPT locally."""
    del modified_ns
    source = Path(path)
    if source.suffix.lower() == ".pptx":
        from pptx import Presentation

        deck = Presentation(source)
        records: list[dict[str, str]] = []
        for number, slide in enumerate(deck.slides, start=1):
            parts: list[str] = []
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False) and str(shape.text).strip():
                    parts.append(str(shape.text).strip())
                elif getattr(shape, "has_table", False):
                    table_text = " | ".join(
                        cell.text.strip()
                        for row in shape.table.rows
                        for cell in row.cells
                        if cell.text.strip()
                    )
                    if table_text:
                        parts.append(table_text)
            title = parts[0].splitlines()[0] if parts else f"Slide {number}"
            records.append({"title": title, "content": "\n\n".join(parts)})
        return records

    from ..pdf_processor import PDFProcessor

    with tempfile.TemporaryDirectory(prefix="library-ppt-text-") as directory:
        converted = PDFProcessor._convert_legacy_ppt(source, Path(directory) / "converted")
        import fitz

        with fitz.open(converted) as pdf:
            records = []
            for number, page in enumerate(pdf, start=1):
                content = str(page.get_text("text")).strip()
                title = next((line.strip() for line in content.splitlines() if line.strip()), f"Slide {number}")
                records.append({"title": title, "content": content})
            return records


@st.cache_data(show_spinner=False)
def _legacy_ppt_fullscreen_data(
    path: str, modified_ns: int
) -> tuple[list[dict[str, str]], dict[int, bytes]]:
    """Convert a legacy PPT once to provide both slide text and slide images."""
    del modified_ns
    from ..pdf_processor import PDFProcessor
    import fitz

    source = Path(path)
    with tempfile.TemporaryDirectory(prefix="library-ppt-fullscreen-") as directory:
        converted = PDFProcessor._convert_legacy_ppt(source, Path(directory) / "converted")
        records: list[dict[str, str]] = []
        images: dict[int, bytes] = {}
        with fitz.open(converted) as pdf:
            for number, page in enumerate(pdf, start=1):
                content = str(page.get_text("text")).strip()
                title = next((line.strip() for line in content.splitlines() if line.strip()), f"Slide {number}")
                records.append({"title": title, "content": content})
                images[number] = page.get_pixmap(
                    matrix=fitz.Matrix(1.5, 1.5), alpha=False
                ).tobytes("png")
        return records, images


def _render_fullscreen_powerpoint(document: LibraryDocument, *, focus_locator: str = "") -> None:
    """Show a scroll-driven PowerPoint slide beside its explanation."""
    modified_ns = document.stored_path.stat().st_mtime_ns
    try:
        if document.stored_path.suffix.lower() == ".ppt":
            records, images = _legacy_ppt_fullscreen_data(
                str(document.stored_path), modified_ns
            )
        else:
            records = _powerpoint_slide_text_records(str(document.stored_path), modified_ns)
            images = _powerpoint_slide_images(str(document.stored_path), modified_ns)
    except Exception as exc:
        st.error(f"Could not prepare this presentation: {exc}")
        return
    if not records:
        st.info("This presentation does not contain any readable slides.")
        return

    page_key = f"fullscreen_library_ppt_slide_{document.id}"
    focus_slide = _locator_number(focus_locator, "slide")
    focus_key = f"fullscreen_library_ppt_focus_{document.id}_{focus_slide}"
    if focus_slide and not st.session_state.get(focus_key, False):
        st.session_state[page_key] = focus_slide
        st.session_state[focus_key] = True
    try:
        slide_number = int(st.session_state.get(page_key, 1))
    except (TypeError, ValueError):
        slide_number = 1
    slide_number = min(max(slide_number, 1), len(records))
    st.session_state[page_key] = slide_number
    pages = [
        {
            **record,
            "image": images.get(number, b""),
        }
        for number, record in enumerate(records, start=1)
    ]
    _render_elastic_presentation(
        pages,
        page_label="Slide",
        deck_name=document.original_name,
        initial_page=slide_number,
        key=f"ppt-{document.id}",
    )


def _is_summary_pdf(document: LibraryDocument) -> bool:
    return document.stored_path.suffix.lower() == ".pdf" and "summary" in document.original_name.casefold()


def _voice_audio_key(key: str, narration: str, voice: str) -> str:
    digest = hashlib.sha256(f"{voice}\0{narration}".encode("utf-8")).hexdigest()[:16]
    return f"library_voice_audio_v4_{key}_{digest}"


def _render_voice_narration(
    narration: str,
    *,
    key: str,
    button_label: str = "🔊",
    button_target: Any | None = None,
    help_text: str = "Explain this content aloud with a local voice.",
) -> None:
    """Offer local neural TTS playback without storing narration files in the library."""
    target = button_target or st
    voices = local_voice_options()
    with target.popover(
        button_label,
        help=help_text,
        width="stretch",
    ):
        st.caption("Narration uses a local Piper neural voice and is not uploaded or saved to your library.")
        setup_message = neural_voice_setup_message()
        if setup_message:
            st.info(setup_message)
            if "not installed" not in setup_message:
                if st.button(
                    "Install English neural voice",
                    key=f"library_install_voice_{key}",
                    width="stretch",
                ):
                    try:
                        with st.spinner("Downloading the local neural voice…"):
                            install_default_voice()
                    except VoiceNarrationError as exc:
                        st.error(str(exc))
                    else:
                        st.rerun()
            return
        voice = st.selectbox(
            "Neural voice",
            voices,
            key=f"library_voice_choice_{key}",
        )
        try:
            prepared, _ = prepare_narration(narration)
        except VoiceNarrationError as exc:
            st.info(str(exc))
            return
        audio_key = _voice_audio_key(key, prepared, voice)
        if st.button(
            "Generate narration",
            type="primary",
            key=f"library_generate_voice_{key}",
            width="stretch",
        ):
            # Never leave an older recording visible when a fresh generation
            # attempt fails or is interrupted.
            st.session_state.pop(audio_key, None)
            try:
                with st.spinner("Generating local narration…"):
                    st.session_state[audio_key] = synthesize_speech(prepared, voice)
            except VoiceNarrationError as exc:
                st.error(str(exc))
        audio = st.session_state.get(audio_key)
        if isinstance(audio, bytes):
            st.audio(audio, format="audio/wav")
            duration = wav_duration_seconds(audio)
            st.caption(
                f"Full narration: {duration:.1f} seconds · {len(prepared):,} text characters"
            )
            st.download_button(
                "Download WAV",
                data=audio,
                file_name="class-knowledge-explanation.wav",
                mime="audio/wav",
                key=f"library_download_voice_{key}",
                width="stretch",
            )


def _render_pdf_voice_control(document: LibraryDocument, *, full_page: bool) -> None:
    """Add a page-level listener, plus whole-document playback for summaries."""
    try:
        modified_ns = document.stored_path.stat().st_mtime_ns
        page_count = _pdf_page_count(str(document.stored_path), modified_ns)
        if page_count < 1:
            return
        page_key = (
            f"fullscreen_library_pdf_page_{document.id}"
            if full_page else f"current_library_pdf_page_{document.id}"
        )
        selected_page = int(st.session_state.get(page_key, 1))
        selected_page = min(max(selected_page, 1), page_count)
        if full_page and page_count > 1:
            selected_page = st.selectbox(
                "Page to read",
                list(range(1, page_count + 1)),
                format_func=lambda page: f"Page {page}",
                key=page_key,
            )
        page_text = _pdf_page_text(str(document.stored_path), modified_ns, selected_page)
        _render_voice_narration(
            page_text,
            key=f"pdf_page_{document.id}_{selected_page}",
            button_label="🔊 Listen to page",
            help_text=f"Explain page {selected_page} aloud with a local voice.",
        )
        if _is_summary_pdf(document):
            _render_voice_narration(
                _pdf_full_text(str(document.stored_path), modified_ns),
                key=f"pdf_summary_{document.id}",
                button_label="🔊 Listen to complete summary",
                help_text="Explain the complete summary aloud with a local voice.",
            )
    except (OSError, ValueError, RuntimeError) as exc:
        st.caption(f"Voice narration is unavailable for this PDF: {exc}")


def _read_text_preview(path: Path, maximum_bytes: int = 750_000) -> tuple[str, bool]:
    with path.open("rb") as source:
        data = source.read(maximum_bytes + 1)
    truncated = len(data) > maximum_bytes
    return data[:maximum_bytes].decode("utf-8", errors="replace"), truncated


@st.cache_data(show_spinner=False)
def _powerpoint_slide_images(path: str, modified_ns: int) -> dict[int, bytes]:
    """Cache rendered slide images without retaining temporary conversion files."""
    from ..pdf_processor import PDFProcessor

    with tempfile.TemporaryDirectory(prefix="library-ppt-preview-") as directory:
        image_dir = Path(directory) / "slide_images"
        image_dir.mkdir()
        images = PDFProcessor._render_powerpoint_previews(Path(path), image_dir)
        if not images and Path(path).suffix.lower() == ".pptx":
            from pptx import Presentation

            deck = Presentation(path)
            for number, slide in enumerate(deck.slides, start=1):
                parts: list[str] = []
                for shape in slide.shapes:
                    if getattr(shape, "has_text_frame", False) and str(shape.text).strip():
                        parts.append(str(shape.text).strip())
                    elif getattr(shape, "has_table", False):
                        table_text = " | ".join(
                            cell.text.strip()
                            for row in shape.table.rows
                            for cell in row.cells
                            if cell.text.strip()
                        )
                        if table_text:
                            parts.append(table_text)
                title = parts[0].splitlines()[0] if parts else f"Slide {number}"
                preview_path = image_dir / f"slide_{number:03d}_preview.png"
                PDFProcessor._make_text_preview(preview_path, number, title, "\n".join(parts))
                images[number] = preview_path
        return {number: image.read_bytes() for number, image in images.items()}


def _render_powerpoint_preview(document: LibraryDocument, *, focus_locator: str = "") -> None:
    try:
        from pptx import Presentation

        deck = Presentation(document.stored_path)
        if not deck.slides:
            st.info("This presentation does not contain slides.")
            return
        with st.spinner("Preparing slide previews…"):
            images = _powerpoint_slide_images(
                str(document.stored_path), document.stored_path.stat().st_mtime_ns
            )
        if not images:
            st.caption("Slide images require LibreOffice. Showing extracted slide content below.")

        focus_slide = _locator_number(focus_locator, "slide")
        if focus_slide is not None and 1 <= focus_slide <= len(deck.slides):
            matched_slide = deck.slides[focus_slide - 1]
            matched_parts = [
                str(shape.text).strip()
                for shape in matched_slide.shapes
                if getattr(shape, "has_text_frame", False) and str(shape.text).strip()
            ]
            st.markdown(f"#### Search match · Slide {focus_slide}")
            with st.container(border=True):
                if focus_slide in images:
                    render_preview_image(images[focus_slide], width="stretch")
                st.markdown("##### Slide content")
                st.text("\n\n".join(matched_parts) if matched_parts else "(No extractable text on this slide.)")
            st.caption("The complete presentation follows below.")

        for slide_number, slide in enumerate(deck.slides, start=1):
            parts = [
                str(shape.text).strip()
                for shape in slide.shapes
                if getattr(shape, "has_text_frame", False) and str(shape.text).strip()
            ]
            title = parts[0].splitlines()[0] if parts else f"Slide {slide_number}"
            with st.expander(
                f"Slide {slide_number}: {title}",
                expanded=slide_number == (focus_slide or 1),
            ):
                if slide_number in images:
                    render_preview_image(images[slide_number], width="stretch")
                st.markdown("#### Slide content")
                st.text("\n\n".join(parts) if parts else "(No extractable text on this slide.)")
                if parts:
                    _render_voice_narration(
                        f"Slide {slide_number}. {title}. {' '.join(parts)}",
                        key=f"powerpoint_slide_{document.id}_{slide_number}",
                        button_label="🔊 Explain this slide",
                    )
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
    return "A combined slide-and-lecture note has not been generated for this slide."


def _render_current_slide(document: LibraryDocument, slide: dict[str, Any]) -> None:
    number = int(slide.get("slide", 1))
    suffix = document.stored_path.suffix.lower()
    if suffix == ".pdf":
        try:
            import fitz

            with fitz.open(document.stored_path) as pdf:
                page = pdf[number - 1]
                image = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False).tobytes("png")
            render_preview_image(image, width="stretch")
            return
        except Exception as exc:
            st.warning(f"Could not render slide {number}: {exc}")

    preview_path = Path(str(slide.get("preview_image", "")))
    if preview_path.is_file():
        render_preview_image(str(preview_path), width="stretch")
        return
    title = html.escape(str(slide.get("title", f"Slide {number}")))
    content = html.escape(str(slide.get("content", "")) or "(No extractable slide text.)")
    st.markdown(
        f'<div class="lecture-slide-fallback"><strong>{title}</strong><br><br>{content}</div>',
        unsafe_allow_html=True,
    )


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
    button_target: Any | None = None,
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
        st.info(f"🧠 {latest.status.title()} · {latest.progress}% — {latest.message}")
        if st.button(
            "↻",
            help="Refresh review status",
            key=f"refresh_deep_review_{document.id}_{slide_number}",
            width="stretch",
        ):
            st.rerun()

    help_text = (
        "Generate a detailed, source-grounded explanation for only this slide."
        if source_job
        else "The completed lecture job for this file could not be located."
    )
    target = button_target or st
    if target.button(
        "🧠",
        type="primary",
        disabled=source_job is None or active,
        help=help_text,
        key=f"library_deep_review_{document.id}_{slide_number}",
        width="stretch",
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


def _render_translation_control(
    manager: JobManager,
    document: LibraryDocument,
    bundle: _LectureSlideBundle,
    slide_number: int,
    button_target: Any | None = None,
) -> None:
    """Queue a lecture translation from the slide explanation controls."""
    source_job = bundle.source_job
    target = button_target or st
    help_text = (
        "Translate this slide’s combined lecture note."
        if source_job
        else "The completed lecture job for this file could not be located."
    )
    if source_job is None:
        target.button(
            "🌐",
            disabled=True,
            help=help_text,
            key=f"library_translation_unavailable_{document.id}_{slide_number}",
            width="stretch",
        )
        return
    with target.popover("🌐", help=help_text, width="stretch"):
        st.caption("Translate only this slide’s combined note.")
        with st.form(f"library_translation_form_{document.id}_{source_job.id}_{slide_number}"):
            language_option = st.selectbox(
                "Translate to",
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
                key=f"library_translation_language_{document.id}_{source_job.id}_{slide_number}",
            )
            custom_language = st.text_input(
                "Other target language",
                placeholder="e.g. Italian",
                disabled=language_option != "Other language",
                key=f"library_translation_custom_language_{document.id}_{source_job.id}_{slide_number}",
            )
            priority_label = st.selectbox(
                "Priority",
                list(PRIORITIES),
                index=list(PRIORITIES).index("Normal"),
                key=f"library_translation_priority_{document.id}_{source_job.id}_{slide_number}",
            )
            submitted = st.form_submit_button("Queue translation", type="primary", width="stretch")
        if submitted:
            target_language = custom_language.strip() if language_option == "Other language" else language_option
            try:
                translation_job = manager.enqueue_translation(
                    source_job.id,
                    target_language,
                    priority=PRIORITIES[priority_label],
                    slide_number=slide_number,
                    explanation=_exact_slide_summary(bundle, next(item for item in bundle.slides if int(item["slide"]) == slide_number)),
                )
                st.session_state[f"slide_requested_language_{document.id}_{slide_number}"] = target_language
                if translation_job.status == "completed":
                    notice = f"The {target_language} translation is already available."
                elif translation_job.status in {"queued", "waiting", "running", "deferred"}:
                    notice = (
                        f"The {target_language} translation is {translation_job.status} "
                        f"as job {translation_job.id[:8]}."
                    )
                else:
                    notice = f"Queued a {target_language} translation as job {translation_job.id[:8]}."
                st.session_state["library_file_notice"] = notice
                st.rerun()
            except JobError as exc:
                st.error(str(exc))


def _slide_content_markdown(slide: dict[str, Any]) -> str:
    title = str(slide.get("title", "")).strip()
    content = str(slide.get("content", "")).strip()
    return "\n\n".join(part for part in (f"## {title}" if title else "", content) if part)


def _slide_content_translations(
    manager: JobManager, bundle: _LectureSlideBundle, slide: dict[str, Any]
) -> dict[str, str]:
    source = _slide_content_markdown(slide)
    translations = {"English": source}
    saved = getattr(bundle, "saved_translations", {}).get(int(slide["slide"]), {})
    if bundle.source_job is None:
        translations.update(saved)
        return translations
    for job in manager.list_jobs(limit=500):
        if (job.kind == "translation" and job.status == "completed"
            and job.payload.get("source_job_id") == bundle.source_job.id
            and job.payload.get("slide_number") == int(slide["slide"])
            and job.payload.get("source_slide_content") == source):
            language = str(job.payload.get("target_language", ""))
            text = str(job.result.get("translated_slide_content", "")).strip()
            if text and language not in translations:
                translations[language] = text
    for language, text in saved.items():
        translations.setdefault(language, text)
    return translations


def _slide_explanation_translations(
    manager: JobManager, bundle: _LectureSlideBundle, slide: dict[str, Any]
) -> tuple[dict[str, str], list[JobRecord]]:
    explanations = {"English": _exact_slide_summary(bundle, slide)}
    pending = []
    for language, text in getattr(bundle, "saved_translations", {}).get(int(slide["slide"]), {}).items():
        if text.strip():
            explanations.setdefault(language, text.strip())
    if bundle.source_job is None:
        return explanations, pending
    # Newest matching translation wins; never mix lectures or slide content.
    for job in manager.list_jobs(limit=500):
        if (job.kind != "translation"
            or job.payload.get("source_job_id") != bundle.source_job.id
            or job.payload.get("slide_number") != int(slide["slide"])
            or job.payload.get("source_explanation") != explanations["English"]):
            continue
        language = str(job.payload.get("target_language", ""))
        if job.status == "completed" and language not in explanations:
            text = str(job.result.get("translated_explanation", "")).strip()
            if text:
                explanations[language] = text
        elif job.status in {"queued", "running", "waiting", "deferred", "failed"}:
            pending.append(job)
    return explanations, pending


def _render_note_language(document_id: str, slide_number: int, contents: dict[str, str]) -> str:
    preference_key = f"slide_text_language_preference_{document_id}"
    widget_key = f"slide_text_language_{document_id}_{slide_number}"
    requested_key = f"slide_requested_language_{document_id}_{slide_number}"
    requested = st.session_state.get(requested_key)
    if requested in contents:
        st.session_state[preference_key] = requested
        del st.session_state[requested_key]
    preferred = st.session_state.get(preference_key, "English")
    st.session_state[widget_key] = preferred if preferred in contents else "English"

    def remember_language() -> None:
        st.session_state[preference_key] = st.session_state[widget_key]

    language = st.selectbox(
        "Note language", list(contents), key=widget_key, on_change=remember_language,
    )
    if preferred not in contents:
        st.caption(f"{preferred} is unavailable for this slide. Showing English; your language preference is retained.")
    return language


def _render_slide_document(
    manager: JobManager,
    document: LibraryDocument,
    bundle: _LectureSlideBundle,
    *,
    full_page: bool = False,
    focus_locator: str = "",
) -> None:
    slide_lookup = {int(item["slide"]): item for item in bundle.slides}
    slide_numbers = sorted(slide_lookup)
    slide_key = f"current_library_slide_{document.id}"
    try:
        selected_number = int(st.session_state.get(slide_key, slide_numbers[0]))
    except (TypeError, ValueError):
        selected_number = slide_numbers[0]
    focus_slide = _locator_number(focus_locator, "slide")
    focus_key = f"library_search_focus_slide_{document.id}_{focus_slide}"
    if focus_slide in slide_lookup and not st.session_state.get(focus_key, False):
        selected_number = focus_slide
        st.session_state[focus_key] = True
    if selected_number not in slide_lookup:
        selected_number = slide_numbers[0]
    st.session_state[slide_key] = selected_number
    slide = slide_lookup[selected_number]
    selected_index = slide_numbers.index(selected_number)
    st.html(
        """<style>
        .lecture-slide-nav { text-align:center; line-height:1.25; padding:.15rem .3rem; }
        .lecture-slide-nav strong { display:block; font-size:clamp(1rem, 1.3vw, 1.25rem); }
        .lecture-slide-nav span { color:#687083; font-size:.78rem; }
        .lecture-slide-summary { font-size:clamp(1.08rem, 1.45vw, 1.38rem); line-height:1.58; }
        .lecture-slide-fallback { font-size:clamp(1.08rem, 1.65vw, 1.55rem); line-height:1.55;
          padding:1.25rem; border:1px solid rgba(128,128,128,.25); border-radius:.75rem; }
        /* Streamlit's fit-content image toolbar can collapse in Safari. Give
           the slide an explicit CSS width instead of relying on its measured width. */
        .st-key-library_slide_image [data-testid="stFullScreenFrame"] > div:has(> [data-testid="stImage"]) {
          width: 100%;
        }
        .st-key-library_slide_image [data-testid="stImage"],
        .st-key-library_slide_image [data-testid="stImageContainer"] {
          width: 100%;
        }
        .st-key-library_slide_image [data-testid="stImageContainer"] > img {
          width: 100% !important; height: auto;
        }
        </style>""",
    )
    if full_page:
        st.html(
            """<style>
            .lecture-slide-summary {
              font-size: clamp(1.35rem, 1.8vw, 1.8rem);
              line-height: 1.68;
            }
            </style>""",
        )
    def render_slide() -> None:
        navigation = st.container(key="library_slide_navigation")
        previous_column, title_column, next_column = navigation.columns(
            [1, 5, 1], vertical_alignment="center"
        )
        if previous_column.button(
            "◀",
            disabled=selected_index == 0,
            help="Previous slide",
            key=f"previous_library_slide_{document.id}",
            width="stretch",
        ):
            st.session_state[slide_key] = slide_numbers[selected_index - 1]
            st.rerun()
        slide_title = html.escape(str(slide.get("title", "")).strip())
        title_column.markdown(
            f'<div class="lecture-slide-nav"><strong>{slide_title or f"Slide {selected_number}"}</strong>'
            f'<span>{selected_number} / {len(slide_numbers)}</span></div>',
            unsafe_allow_html=True,
        )
        if next_column.button(
            "▶",
            disabled=selected_index == len(slide_numbers) - 1,
            help="Next slide",
            key=f"next_library_slide_{document.id}",
            width="stretch",
        ):
            st.session_state[slide_key] = slide_numbers[selected_index + 1]
            st.rerun()
        with st.container(key="library_slide_image"):
            _render_current_slide(document, slide)

    def render_summary() -> None:
        summary_title, voice_button, review_button, translation_button = st.columns(
            [4, 1, 1, 1], vertical_alignment="center"
        )
        summary_title.markdown("#### Combined lecture note")
        explanations, translation_jobs = _slide_explanation_translations(manager, bundle, slide)
        language = _render_note_language(document.id, selected_number, explanations)
        for translation_job in translation_jobs:
            st.caption(f"{translation_job.payload['target_language']} translation: {translation_job.status}")
        if translation_jobs and st.button("Refresh translations", key=f"refresh_slide_translation_{document.id}_{selected_number}"):
            st.rerun()
        explanation = explanations.get(language)
        if explanation:
            summary = html.escape(explanation)
            st.markdown(f'<div class="lecture-slide-summary">{summary}</div>', unsafe_allow_html=True)
        # The language display choice is independent of speech generation.
        narration = "\n\n".join(part for part in (
            str(slide.get("title", "")).strip(), explanations["English"]
        ) if part)
        _render_voice_narration(
            narration,
            key=f"slide_{document.id}_{selected_number}",
            button_target=voice_button,
            help_text="Read the combined slide-and-lecture note aloud.",
        )
        _render_deep_review_control(
            manager,
            document,
            bundle,
            selected_number,
            button_target=review_button,
        )
        _render_translation_control(
            manager,
            document,
            bundle,
            selected_number,
            button_target=translation_button,
        )

    if full_page:
        slide_column, summary_column = st.columns([1.65, 1], gap="large")
        with slide_column:
            render_slide()
        with summary_column:
            render_summary()
    else:
        render_slide()
        render_summary()

def _render_transcript_fallback(
    document: LibraryDocument,
    paragraphs: list[dict[str, Any]],
) -> None:
    st.audio(str(document.stored_path), format=document.media_type)
    if not paragraphs:
        st.info("A cleaned transcript is not available for this recording yet.")
        return
    timeline = st.container(height=560, border=True)
    with timeline:
        st.markdown("#### Cleaned transcript timeline")
        for paragraph in paragraphs:
            start = str(paragraph.get("start_time", "")).strip()
            end = str(paragraph.get("end_time", "")).strip()
            stamp = f"{start}–{end}".strip("–")
            if stamp:
                st.caption(stamp)
            st.write(str(paragraph.get("text", "")).strip())


def _render_audio_document(
    document: LibraryDocument,
    bundle: _LectureAudioBundle | None,
    *,
    full_page: bool = False,
    focus_text: str = "",
) -> None:
    paragraphs = bundle.paragraphs if bundle is not None else []
    if not paragraphs:
        _render_transcript_fallback(document, paragraphs)
        return
    rendered = render_audio_transcript(
        document.stored_path,
        document.media_type,
        paragraphs,
        key=document.id,
        height=790 if full_page else INLINE_PREVIEW_CONTENT_HEIGHT,
        focus_text=focus_text,
    )
    if not rendered:
        _render_transcript_fallback(document, paragraphs)


def _render_document_content(
    document: LibraryDocument,
    audio_bundle: _LectureAudioBundle | None = None,
    *,
    full_page: bool = False,
    focus_locator: str = "",
    focus_text: str = "",
) -> None:
    suffix = document.stored_path.suffix.lower()
    try:
        if suffix == ".pdf":
            if full_page:
                _render_fullscreen_pdf_reader(document, focus_locator=focus_locator)
            else:
                _render_pdf(document, full_page=False, focus_locator=focus_locator)
            _render_pdf_voice_control(document, full_page=full_page)
        elif suffix in AUDIO_SUFFIXES:
            _render_audio_document(
                document,
                audio_bundle,
                full_page=full_page,
                focus_text=focus_text,
            )
        elif suffix in VIDEO_SUFFIXES:
            st.video(str(document.stored_path), format=document.media_type)
        elif suffix in IMAGE_SUFFIXES:
            render_preview_image(str(document.stored_path), width="stretch")
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
        elif suffix in {".ppt", ".pptx"}:
            if full_page:
                _render_fullscreen_powerpoint(document, focus_locator=focus_locator)
            elif suffix == ".pptx":
                _render_powerpoint_preview(document, focus_locator=focus_locator)
            else:
                st.info("Legacy PPT files need LibreOffice for a slide preview. Use File actions (⋯) to download or organize it.")
        else:
            st.info("This file type has no inline preview. Use File actions (⋯) to download or organize it.")
    except (OSError, ValueError, RuntimeError) as exc:
        st.error(f"Could not open this file: {exc}")


def _render_file_preview(
    library: LibraryStore,
    manager: JobManager,
    document: LibraryDocument,
    selected_key: str,
    slide_bundle: _LectureSlideBundle | None = None,
    audio_bundle: _LectureAudioBundle | None = None,
) -> None:
    # Style-only HTML is placed outside the content flow by Streamlit.
    st.html(
        """<style>
        .st-key-library_preview_content {
          position: relative;
        }
        /* Remove the layout wrapper, not just its keyed child, from the flow. */
        .st-key-library_preview_content > [data-testid="stLayoutWrapper"]:has(> .st-key-library_preview_menu) {
          position: absolute; top: .4rem; right: .4rem; z-index: 100;
          width: 2.7rem; min-width: 0; height: auto; overflow: visible;
        }
        .st-key-library_preview_menu {
          width: 100%; overflow: visible;
        }
        .st-key-library_preview_content .st-key-library_slide_navigation {
          padding-right: 2.7rem;
        }
        .st-key-library_preview_content .st-key-library_pdf_navigation {
          padding-right: 2.7rem;
        }
        .st-key-library_preview_menu div[data-testid="stPopover"] > button {
          border-radius: 999px; background: color-mix(in srgb, var(--secondary-background-color) 88%, transparent);
          box-shadow: 0 2px 10px rgba(0,0,0,.22); font-size: 1.15rem;
        }
        </style>""",
    )
    with st.container(key="library_preview_menu"):
        with st.popover("⋯", help="File actions", width="stretch"):
            if st.button(
                "Close preview",
                key=f"close_preview_{document.id}",
                width="stretch",
            ):
                st.session_state.pop(selected_key, None)
                st.rerun()
            st.link_button(
                "Full screen ↗",
                f"?open_file={document.id}",
                help="Open this file in a full-page browser view.",
                width="stretch",
            )
            with st.expander("Rename"):
                with st.form(f"rename_file_{document.id}"):
                    new_name = st.text_input("File name", value=document.original_name)
                    rename = st.form_submit_button("Save", width="stretch")
                if rename:
                    try:
                        updated = library.rename_document(document.id, new_name)
                        st.session_state["library_file_notice"] = f"Renamed file to {updated.original_name}."
                        st.rerun()
                    except LibraryError as exc:
                        st.error(str(exc))
            if document.size_bytes <= 100 * 1024 * 1024:
                try:
                    st.download_button(
                        "Download",
                        data=document.stored_path.read_bytes(),
                        file_name=document.original_name,
                        mime=document.media_type,
                        key=f"download_file_{document.id}",
                        width="stretch",
                    )
                except OSError as exc:
                    st.error(f"Could not prepare this file for download: {exc}")
            else:
                st.caption("This file is too large for an in-browser download.")
            if st.button(
                "Delete",
                key=f"delete_preview_{document.id}",
                width="stretch",
            ):
                st.session_state["library_document_delete_confirmation"] = document.id

            if st.session_state.get("library_document_delete_confirmation") == document.id:
                st.warning(f"Permanently delete '{document.original_name}'?")
                confirm, cancel = st.columns(2)
                if confirm.button("Delete", type="primary", key=f"confirm_delete_{document.id}"):
                    try:
                        library.delete_document(document.id)
                        st.session_state.pop("library_document_delete_confirmation", None)
                        st.session_state.pop(selected_key, None)
                        st.session_state["library_file_notice"] = f"Deleted {document.original_name}."
                        st.rerun()
                    except LibraryError as exc:
                        st.error(str(exc))
                if cancel.button("Cancel", key=f"cancel_delete_{document.id}"):
                    st.session_state.pop("library_document_delete_confirmation", None)
                    st.rerun()

    if document.extraction_error:
        st.warning(document.extraction_error)
    if slide_bundle is not None:
        _render_slide_document(manager, document, slide_bundle)
    else:
        _render_document_content(document, audio_bundle)


def render_full_library_document(
    library: LibraryStore,
    manager: JobManager,
    document_id: str,
    *,
    focus_locator: str = "",
    focus_text: str = "",
) -> None:
    """Render one library file without the workspace chrome for a new tab."""
    try:
        document = library.get_document(document_id)
    except LibraryError as exc:
        st.error(str(exc))
        return

    st.html(
        """<style>
        [data-testid="stAppViewContainer"] .main .block-container {
          max-width: none !important; width: 100% !important;
          padding: 1rem 2rem 3rem;
        }
        [data-testid="stHeader"], footer { display: none; }
        .fullscreen-page-label { text-align: center; font-weight: 650; padding: .25rem; }
        .st-key-fullscreen_document_text [data-testid="stMarkdownContainer"] {
          font-size: clamp(1.3rem, 1.65vw, 1.75rem); line-height: 1.65;
        }
        .st-key-fullscreen_document_text h3 { font-size: clamp(1.5rem, 1.9vw, 2rem); }
        </style>""",
    )
    with st.container(key="full_library_document"):
        back, heading, download = st.columns([1, 6, 1], vertical_alignment="center")
        if back.button("← Library", key=f"full_library_back_{document.id}"):
            st.query_params.clear()
            st.rerun()
        heading.markdown(f"### {file_icon(document.original_name)} {document.original_name}")
        if document.size_bytes <= 100 * 1024 * 1024:
            try:
                download.download_button(
                    "Download",
                    data=document.stored_path.read_bytes(),
                    file_name=document.original_name,
                    mime=document.media_type,
                    key=f"fullscreen_download_{document.id}",
                    width="stretch",
                )
            except OSError as exc:
                download.caption(f"Download unavailable: {exc}")
        else:
            download.caption("File is too large for an in-browser download.")
    st.caption(
        f"{document.subject_name} / {document.folder_name or 'Subject root'} · "
        f"{format_size(document.size_bytes)}"
    )
    if focus_text:
        with st.container(border=True):
            st.caption(f"Search match{f' · {focus_locator}' if focus_locator else ''}")
            st.write(focus_text)
    st.divider()
    slide_bundle = _lecture_slide_bundle(library, manager, document)
    if slide_bundle is not None:
        _render_slide_document(
            manager,
            document,
            slide_bundle,
            full_page=True,
            focus_locator=focus_locator,
        )
        return
    audio_bundle = _lecture_audio_bundle(library, manager, document)
    _render_document_content(
        document,
        audio_bundle,
        full_page=True,
        focus_locator=focus_locator,
        focus_text=focus_text,
    )


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

    if action == "create_folder":
        try:
            folder = library.create_folder(subject_id, str(event.get("name", "")))
            st.session_state[browse_key] = folder.id
            st.session_state["library_file_notice"] = f"Created {folder.name}."
            st.rerun()
        except LibraryError as exc:
            st.error(str(exc))
        return

    if action == "rename_folder":
        if folder_id not in folder_lookup:
            st.error("That folder no longer exists. Refresh the library and try again.")
            return
        try:
            renamed = library.rename_folder(folder_id, str(event.get("new_name", "")))
            st.session_state["library_file_notice"] = f"Renamed folder to {renamed.name}."
            st.rerun()
        except LibraryError as exc:
            st.error(str(exc))
        return

    if action == "delete_folder":
        if folder_id not in folder_lookup:
            st.error("That folder no longer exists. Refresh the library and try again.")
            return
        folder = folder_lookup[folder_id]
        try:
            library.delete_folder(folder.id, delete_documents=bool(folder.document_count))
            st.session_state[browse_key] = ""
            st.session_state.pop(selected_key, None)
            st.session_state["library_file_notice"] = f"Deleted {folder.name}."
            st.rerun()
        except LibraryError as exc:
            st.error(str(exc))
        return

    if action == "upload":
        if folder_id and folder_id not in folder_lookup:
            st.error("That folder no longer exists. Refresh the library and try again.")
            return
        raw_files = event.get("files", [])
        if not isinstance(raw_files, list) or not raw_files:
            st.error("No files were received. Try dropping the files again.")
            return

        uploaded = 0
        errors: list[str] = []
        skipped_too_large = event.get("skipped_too_large", [])
        if isinstance(skipped_too_large, list):
            errors.extend(
                f"{str(filename)} is too large to upload by direct drop."
                for filename in skipped_too_large
                if isinstance(filename, str) and filename.strip()
            )
        total_bytes = 0
        for item in raw_files:
            if not isinstance(item, dict):
                errors.append("One dropped item could not be read.")
                continue
            filename = str(item.get("name", "")).strip()
            encoded_data = item.get("data", "")
            if not filename or not isinstance(encoded_data, str):
                errors.append("One dropped file is missing its name or data.")
                continue
            try:
                data = base64.b64decode(encoded_data, validate=True)
            except (binascii.Error, ValueError):
                errors.append(f"Could not read {filename}.")
                continue
            if len(data) > DIRECT_DROP_MAX_FILE_BYTES:
                errors.append(f"{filename} is too large to upload by direct drop.")
                continue
            if total_bytes + len(data) > DIRECT_DROP_MAX_TOTAL_BYTES:
                errors.append("The dropped files are too large to upload together. Drop fewer files at once.")
                continue
            total_bytes += len(data)
            try:
                library.add_document_bytes(subject_id, filename, data, folder_id=folder_id or None)
                uploaded += 1
            except LibraryError as exc:
                errors.append(f"Could not add {filename}: {exc}")

        destination = folder_lookup[folder_id].name if folder_id else "Subject root"
        if uploaded:
            st.session_state["library_file_notice"] = (
                f"Added {uploaded} file{'s' if uploaded != 1 else ''} to {destination}."
            )
        if errors:
            st.session_state["library_file_upload_errors"] = errors
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


def _render_subject_selector(library: LibraryStore) -> str | None:
    creating_key = "library_creating_subject"
    name_key = "library_new_subject_name"
    error_key = "library_new_subject_error"
    notice_key = "library_new_subject_notice"
    deleting_key = "library_subject_delete_confirmation"
    pending_selection_key = "library_pending_open_subject"

    if st.session_state.get(creating_key, False):
        def create_subject() -> None:
            name = str(st.session_state.get(name_key, "")).strip()
            if not name:
                st.session_state[error_key] = "Subject name cannot be empty."
                return
            try:
                subject = library.create_subject(name)
            except LibraryError as exc:
                st.session_state[error_key] = str(exc)
                return
            st.session_state["library_open_subject"] = subject.id
            st.session_state[creating_key] = False
            st.session_state.pop(error_key, None)
            st.session_state[notice_key] = f"Created {subject.name}."

        st.text_input(
            "Subject name",
            key=name_key,
            max_chars=120,
            placeholder="Type a subject name and press Enter",
            icon=":material/add:",
            on_change=create_subject,
        )
        error = st.session_state.get(error_key)
        if error:
            st.error(str(error))
        return None

    notice = st.session_state.pop(notice_key, None)
    if notice:
        st.success(str(notice))

    subjects = library.list_subjects()
    lookup = subject_lookup(subjects)
    pending_selection = st.session_state.pop(pending_selection_key, None)
    if pending_selection is not None:
        if pending_selection in lookup:
            st.session_state["library_open_subject"] = pending_selection
        else:
            st.session_state.pop("library_open_subject", None)
    elif st.session_state.get("library_open_subject") not in lookup:
        if subjects:
            st.session_state["library_open_subject"] = subjects[0].id
        else:
            st.session_state.pop("library_open_subject", None)

    deletion_target_id = str(st.session_state.get(deleting_key, ""))
    if deletion_target_id and deletion_target_id not in lookup:
        st.session_state.pop(deleting_key, None)
        deletion_target_id = ""

    selector, add, remove = st.columns([10, 1, 1], vertical_alignment="bottom")
    with selector:
        selected_id = st.selectbox(
            "Open subject",
            options=[subject.id for subject in subjects],
            format_func=lambda value: lookup[value].name,
            key="library_open_subject",
            placeholder="No subjects yet",
            disabled=not subjects or bool(deletion_target_id),
        )
    with add:
        if st.button(
            "＋",
            key="library_add_subject",
            help="Add a subject",
            width="stretch",
            disabled=bool(deletion_target_id),
        ):
            st.session_state[creating_key] = True
            st.session_state.pop(name_key, None)
            st.session_state.pop(error_key, None)
            st.rerun()
    with remove:
        if st.button(
            "🗑️",
            key="library_delete_subject",
            help="Remove this subject",
            width="stretch",
            disabled=not subjects or bool(deletion_target_id),
        ):
            st.session_state[deleting_key] = str(selected_id)
            st.rerun()
    if not subjects:
        st.info("Select + to add your first subject.")
        return None

    if deletion_target_id:
        target = lookup[deletion_target_id]
        document_count = len(library.list_documents(target.id))
        folder_count = len(library.list_folders(target.id))
        st.warning(
            f"Permanently remove '{target.name}'? This deletes {document_count} "
            f"file{'s' if document_count != 1 else ''}, {folder_count} "
            f"folder{'s' if folder_count != 1 else ''}, and their search data."
        )
        confirm, cancel = st.columns(2)
        if confirm.button("Delete subject", type="primary", key="library_confirm_delete_subject"):
            try:
                deleted = library.delete_subject(target.id)
            except LibraryError as exc:
                st.error(str(exc))
                return str(selected_id)
            remaining_subjects = library.list_subjects()
            st.session_state.pop(deleting_key, None)
            st.session_state[pending_selection_key] = (
                remaining_subjects[0].id if remaining_subjects else ""
            )
            st.session_state[notice_key] = f"Deleted {deleted.name}."
            st.rerun()
        if cancel.button("Cancel", key="library_cancel_delete_subject"):
            st.session_state.pop(deleting_key, None)
            st.rerun()
    return str(selected_id)


def render_library(library: LibraryStore, manager: JobManager) -> None:
    st.title("📚 Subject Library")
    st.caption(
        "Browse folders like a desktop file manager, drag files to move them, "
        "and click a file to preview its contents."
    )

    selected_id = _render_subject_selector(library)
    if selected_id is None:
        return
    subjects = library.list_subjects()
    lookup = subject_lookup(subjects)
    subject = lookup[selected_id]
    st.subheader(subject.name)
    if subject.description:
        st.write(subject.description)

    folders = library.list_folders(subject.id)
    documents = _visible_documents(library.list_documents(subject.id))
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
    query = st.text_input(
        "Search this subject",
        placeholder="Search this subject",
        icon=":material/search:",
        label_visibility="collapsed",
        key=f"library_search_{subject.id}",
    )

    notice = st.session_state.pop("library_file_notice", "")
    if notice:
        st.success(notice)
    upload_errors = st.session_state.pop("library_file_upload_errors", [])
    for error in upload_errors:
        st.error(error)

    if query.strip():
        results = [
            result
            for result in library.search_documents(query, subject_id=subject.id, limit=30)
            if not _is_internal_lecture_artifact_name(result.document_name)
        ][:12]
        render_search_results(results)

    selected_document = document_lookup.get(selected_document_id) if selected_document_id else None
    slide_bundle = (
        _lecture_slide_bundle(library, manager, selected_document)
        if selected_document is not None
        else None
    )
    audio_bundle = (
        _lecture_audio_bundle(library, manager, selected_document)
        if selected_document is not None
        else None
    )
    explorer_column, preview_column = st.columns([3, 2], gap="small")
    with explorer_column:
        manager_event = render_file_manager(
            folders,
            documents,
            current_folder_id=current_folder_id or None,
            selected_document_id=selected_document_id,
            key=f"library_explorer_{subject.id}",
        )
    with preview_column:
        with st.container(
            height=INLINE_PREVIEW_HEIGHT,
            border=True,
            key="library_preview_content",
        ):
            if selected_document_id and selected_document_id in document_lookup:
                _render_file_preview(
                    library,
                    manager,
                    document_lookup[selected_document_id],
                    selected_key,
                    slide_bundle,
                    audio_bundle,
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
