"""Portable lecture artifacts keyed by the library's stable lecture folder ID."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

from .library import LibraryStore


ARTIFACT_NAMES = ("slides.json", "slide_summaries.json", "alignment.json", "transcript.json", "transcript.txt", "transcript.raw.json")


def artifact_dir(library: LibraryStore, document: object) -> Path:
    return library.subjects_root / document.subject_id / "derived" / (document.folder_id or document.id)


def save_portable_artifacts(library: LibraryStore, subject_id: str, folder_id: str, run_dir: Path) -> list[str]:
    """Copy a finished run's slide relation and transcript into the shareable library."""
    destination = library.subjects_root / subject_id / "derived" / folder_id
    destination.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for name in ARTIFACT_NAMES:
        source = run_dir / name
        if not source.is_file():
            continue
        target = destination / name
        if name == "slides.json":
            payload = json.loads(source.read_text(encoding="utf-8"))
            for slide in payload.get("slides", []):
                if isinstance(slide, dict):
                    # Run-local preview images cannot resolve on another computer.
                    slide["preview_image"] = ""
                    slide["images"] = []
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            shutil.copyfile(source, target)
        saved.append(name)
    return saved
