"""Local drag-and-drop file manager component for the subject library."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit.components.v1 as components

from ..library import LibraryDocument, LibraryFolder


_COMPONENT_PATH = Path(__file__).parent / "components" / "file_manager"
_file_manager = components.declare_component(
    "class_knowledge_file_manager",
    path=str(_COMPONENT_PATH),
)


def file_icon(filename: str) -> str:
    """Return a simple, recognizable icon for a stored file name."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return "📕"
    if suffix in {".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus"}:
        return "🎧"
    if suffix in {".md", ".markdown"}:
        return "📝"
    if suffix in {".ppt", ".pptx"}:
        return "📊"
    if suffix in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
        return "🎬"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
        return "🖼️"
    if suffix in {".csv", ".tsv", ".xlsx", ".xls"}:
        return "📈"
    if suffix == ".json":
        return "🧾"
    if suffix in {".txt", ".rtf", ".doc", ".docx"}:
        return "📄"
    if suffix in {".zip", ".tar", ".gz", ".7z"}:
        return "🗜️"
    return "📎"


def render_file_manager(
    folders: list[LibraryFolder],
    documents: list[LibraryDocument],
    *,
    current_folder_id: str | None,
    selected_document_id: str | None,
    key: str,
) -> dict[str, Any] | None:
    """Render a desktop-style explorer and return its latest interaction."""
    folder_items = [
        {
            "id": "",
            "name": "Subject root",
            "icon": "🏠",
            "document_count": sum(document.folder_id is None for document in documents),
        }
    ]
    folder_items.extend(
        {
            "id": folder.id,
            "name": folder.name,
            "icon": "📁",
            "document_count": folder.document_count,
        }
        for folder in folders
    )
    document_items = [
        {
            "id": document.id,
            "name": document.original_name,
            "icon": file_icon(document.original_name),
            "extension": Path(document.original_name).suffix.lstrip(".").upper() or "FILE",
            "folder_id": document.folder_id or "",
            "folder_name": document.folder_name or "Subject root",
        }
        for document in documents
    ]
    value = _file_manager(
        folders=folder_items,
        documents=document_items,
        current_folder_id=current_folder_id or "",
        selected_document_id=selected_document_id or "",
        key=key,
        default=None,
    )
    return value if isinstance(value, dict) else None
