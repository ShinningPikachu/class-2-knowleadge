"""Consistent, human-readable identities for lecture runs and artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .utils import clean_text, safe_filename


_NUMBERED_LECTURE = re.compile(
    r"\b(?:lecture|lec|week|class|session|chapter)\s*[-_ ]*0*(\d{1,3})\b",
    flags=re.IGNORECASE,
)
_GENERIC_WORDS = re.compile(
    r"\b(?:recording|audio|video|slides?|deck|presentation|powerpoint|final|copy)\b",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class LectureIdentity:
    """Names shared by the queue, run directory, files, and library folder."""

    number: int
    topic: str
    base_name: str
    display_title: str


def infer_lecture_identity(
    lecture_title: str | None = None,
    audio_path: str | Path | None = None,
    presentation_path: str | Path | None = None,
    *,
    default_number: int = 1,
) -> LectureIdentity:
    """Infer a stable artifact identity while preserving an explicit task title."""
    explicit_title = clean_text(lecture_title or "")
    candidates = [
        explicit_title,
        Path(presentation_path).stem if presentation_path else "",
        Path(audio_path).stem if audio_path else "",
    ]
    source = next((item for item in candidates if item), "Lecture")
    match = _NUMBERED_LECTURE.search(source)
    number = max(1, int(match.group(1))) if match else max(1, int(default_number))

    topic = _NUMBERED_LECTURE.sub(" ", source)
    topic = _GENERIC_WORDS.sub(" ", topic)
    topic = re.sub(r"(?:^|\s)[vV]?\d+(?:\.\d+)+(?:\s|$)", " ", topic)
    topic = re.sub(r"\s*[-_–—]+\s*", " ", topic)
    topic = clean_text(topic).strip(" .-_–—()[]")
    if not topic:
        topic = "Untitled"

    topic_slug = Path(safe_filename(topic, "Untitled")).stem.strip("_-") or "Untitled"
    base_name = f"Lecture_{number:02d}_{topic_slug}"
    display_topic = re.sub(r"[_-]+", " ", topic_slug).strip()
    # A manually supplied title is the user-facing task and folder name. The
    # generated base name remains stable and filesystem-friendly for artifacts.
    display_title = explicit_title or f"Lecture {number:02d} — {display_topic}"
    return LectureIdentity(number, display_topic, base_name, display_title)
