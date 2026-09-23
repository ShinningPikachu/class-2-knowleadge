"""Small runtime safeguards shared by the local PDF processors."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock
from typing import Any


_MUPDF_DIAGNOSTIC_LOCK = RLock()


@contextmanager
def muted_mupdf_errors(fitz: Any) -> Iterator[None]:
    """Mute MuPDF's native stderr logger while preserving Python exceptions.

    Some valid lecture PDFs contain ``Screen`` annotations for embedded media.
    MuPDF cannot build a visual appearance for that annotation type and writes a
    native error for every occurrence, even though page text and images remain
    readable. The setting is process-global, so all project access is serialized
    while it is changed and the caller's previous setting is always restored.
    """
    display_errors = fitz.TOOLS.mupdf_display_errors
    with _MUPDF_DIAGNOSTIC_LOCK:
        previously_enabled = bool(display_errors())
        display_errors(False)
        try:
            yield
        finally:
            display_errors(previously_enabled)
