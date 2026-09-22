"""Store completed lecture sources and outputs in the persistent library."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .library import DuplicateDocumentError, LibraryError, LibraryStore
from .utils import safe_filename


def save_lecture_result(
    library: LibraryStore,
    subject_id: str,
    result: Any,
    lecture_title: str | None,
) -> list[str]:
    """Store source files plus both final note formats in the chosen subject."""
    subject = library.get_subject(subject_id)
    messages: list[str] = []
    source_files = sorted((result.run_dir / "input").iterdir())
    base_name = Path(safe_filename(lecture_title or result.run_id, "lecture")).stem
    candidates: list[tuple[Path, str | None]] = [(path, None) for path in source_files]
    candidates.extend(
        [
            (result.markdown_path, f"{base_name}_notes.md"),
            (result.pdf_path, f"{base_name}_notes.pdf"),
        ]
    )
    for path, filename in candidates:
        if not path.is_file():
            continue
        try:
            document = library.add_document(subject_id, path, filename=filename)
            messages.append(f"Saved {document.original_name} to {subject.name}.")
        except DuplicateDocumentError as exc:
            messages.append(str(exc))
        except LibraryError as exc:
            messages.append(f"Could not add {filename or path.name} to {subject.name}: {exc}")
    return messages
