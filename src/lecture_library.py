"""Store completed lecture sources and outputs in the persistent library."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .library import DuplicateDocumentError, LibraryError, LibraryStore
from .lecture_naming import infer_lecture_identity
from .utils import clean_text


def save_lecture_result(
    library: LibraryStore,
    subject_id: str,
    result: Any,
    lecture_title: str | None,
) -> list[str]:
    """Store source files, the transcript, and both note formats in a subject."""
    subject = library.get_subject(subject_id)
    messages: list[str] = []
    source_files = sorted((result.run_dir / "input").iterdir())
    identity = infer_lecture_identity(lecture_title or getattr(result, "lecture_title", None))
    base_name = str(getattr(result, "lecture_name", "") or identity.base_name)
    folder_name = clean_text(str(getattr(result, "lecture_title", "") or identity.display_title))
    folder = next(
        (item for item in library.list_folders(subject_id) if item.name.casefold() == folder_name.casefold()),
        None,
    )
    if folder is None:
        folder = library.create_folder(subject_id, folder_name)
    candidates: list[tuple[Path, str | None]] = []
    for path in source_files:
        role = "Slides" if path.suffix.lower() in {".pdf", ".ppt", ".pptx"} else "Recording"
        candidates.append((path, f"{base_name}_{role}{path.suffix.lower()}"))
    artifact_specs = [
        ("raw_transcript_path", f"{base_name}_Transcript_Raw.json"),
        ("transcript_path", f"{base_name}_Transcript_Cleaned.json"),
        ("transcript_text_path", f"{base_name}_Transcript_Cleaned.txt"),
        ("slides_path", f"{base_name}_Slides_Extracted.json"),
        ("slide_summaries_path", f"{base_name}_Slide_Summaries.json"),
        ("alignment_path", f"{base_name}_Alignment.json"),
        ("quality_report_path", f"{base_name}_Quality_Report.json"),
        ("manifest_path", f"{base_name}_Manifest.json"),
        ("markdown_path", f"{base_name}_Notes.md"),
        ("pdf_path", f"{base_name}_Notes.pdf"),
    ]
    candidates.extend(
        (Path(path), filename)
        for attribute, filename in artifact_specs
        if (path := getattr(result, attribute, None))
    )
    for path, filename in candidates:
        if not path.is_file():
            continue
        try:
            document = library.add_document(
                subject_id,
                path,
                filename=filename,
                folder_id=folder.id,
            )
            messages.append(f"Saved {document.original_name} to {subject.name} / {folder.name}.")
        except DuplicateDocumentError as exc:
            messages.append(str(exc))
        except LibraryError as exc:
            messages.append(f"Could not add {filename or path.name} to {subject.name}: {exc}")
    return messages
