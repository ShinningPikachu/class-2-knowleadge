"""Small dependency-free helpers used across pipeline modules."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def seconds_to_timestamp(seconds: float) -> str:
    """Format seconds as a readable, zero-padded HH:MM:SS timestamp."""
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def clean_text(text: str) -> str:
    """Remove repeated whitespace without changing the words themselves."""
    return re.sub(r"\s+", " ", text or "").strip()


def safe_filename(name: str, fallback: str = "upload") -> str:
    """Return a filename safe to place inside a run directory."""
    candidate = Path(name or fallback).name
    candidate = re.sub(r"[^\w.\-]+", "_", candidate, flags=re.UNICODE).strip(" .")
    return candidate if candidate and candidate not in {".", ".."} else fallback


def dump_json(path: Path, payload: Any) -> None:
    """Write UTF-8 JSON consistently throughout the project."""
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
