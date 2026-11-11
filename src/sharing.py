"""GitHub-ready inventory and catalog for shareable course materials."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import sqlite3
import shutil
import subprocess
from typing import Any
from urllib.parse import quote

from .library import LibraryDocument, LibraryStore


GITHUB_WARNING_BYTES = 50 * 1024 * 1024
GITHUB_BLOCK_BYTES = 100 * 1024 * 1024
SHAREABLE_ASSET_ROOTS = ("audio", "pdf", "reviewed")
SHARE_INDEX_FILENAME = "SHARE_INDEX.md"
_IGNORED_ASSET_NAMES = {".DS_Store", ".gitkeep"}


@dataclass(frozen=True)
class SharingStatus:
    """A read-only summary of materials and GitHub large-file readiness."""

    repository_root: str
    index_path: str
    subject_count: int
    document_count: int
    library_bytes: int
    loose_file_count: int
    loose_bytes: int
    total_file_count: int
    total_bytes: int
    large_file_count: int
    blocked_file_count: int
    large_files: list[dict[str, Any]]
    blocked_files: list[dict[str, Any]]
    git_available: bool
    git_lfs_installed: bool
    pending_file_count: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _document_path(library: LibraryStore, document: LibraryDocument) -> Path | None:
    candidate = (library.root / document.stored_path).resolve()
    if not _is_within(candidate, library.root.resolve()):
        return None
    return candidate


def _shareable_loose_files(repository_root: Path) -> list[Path]:
    files: list[Path] = []
    for directory in SHAREABLE_ASSET_ROOTS:
        folder = repository_root / directory
        if not folder.is_dir():
            continue
        files.extend(
            path
            for path in folder.rglob("*")
            if path.is_file()
            and path.name not in _IGNORED_ASSET_NAMES
            and not path.name.endswith(".part")
        )
    return sorted(files, key=lambda path: path.relative_to(repository_root).as_posix().casefold())


def _git_attributes(repository_root: Path, paths: list[Path]) -> tuple[bool, dict[str, str]]:
    if not paths:
        return _is_git_repository(repository_root), {}
    relative = [path.relative_to(repository_root).as_posix() for path in paths]
    try:
        result = subprocess.run(
            ["git", "-C", str(repository_root), "check-attr", "-z", "filter", "--", *relative],
            capture_output=True,
            check=False,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, {}
    if result.returncode != 0:
        return False, {}
    fields = result.stdout.split("\0")
    attributes = {
        fields[index]: fields[index + 2]
        for index in range(0, len(fields) - 2, 3)
        if fields[index + 1] == "filter"
    }
    return True, attributes


def _is_git_repository(repository_root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository_root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _git_lfs_installed(repository_root: Path) -> bool:
    if not shutil.which("git"):
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(repository_root), "lfs", "version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def collect_sharing_status(library: LibraryStore, repository_root: str | Path) -> SharingStatus:
    """Inventory indexed library documents and loose user-owned course files."""
    root = Path(repository_root).resolve()
    subjects = library.list_subjects()
    documents = library.list_documents()
    indexed_paths: list[Path] = []
    library_bytes = 0
    for document in documents:
        path = _document_path(library, document)
        if path is not None and path.is_file():
            indexed_paths.append(path)
            library_bytes += path.stat().st_size

    catalog_path = library.database_path.resolve()
    if catalog_path.is_file():
        indexed_paths.append(catalog_path)
        library_bytes += catalog_path.stat().st_size

    loose_paths = _shareable_loose_files(root)
    index_path = root / SHARE_INDEX_FILENAME
    generated_paths = [index_path] if index_path.is_file() else []
    all_paths = sorted(
        set(indexed_paths + loose_paths + generated_paths),
        key=lambda path: path.relative_to(root).as_posix().casefold(),
    )
    git_available, attributes = _git_attributes(root, all_paths)
    large_paths = [path for path in all_paths if path.stat().st_size > GITHUB_WARNING_BYTES]
    blocked_paths = [
        path for path in all_paths
        if path.stat().st_size > GITHUB_BLOCK_BYTES
        and attributes.get(path.relative_to(root).as_posix()) != "lfs"
    ]

    def file_info(path: Path) -> dict[str, Any]:
        relative_path = path.relative_to(root).as_posix()
        return {
            "path": relative_path,
            "size_bytes": path.stat().st_size,
            "filter": attributes.get(relative_path, "unspecified"),
        }

    try:
        pending_result = subprocess.run(
            [
                "git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "--",
                "library/library.sqlite3", "library/subjects", SHARE_INDEX_FILENAME, *SHAREABLE_ASSET_ROOTS,
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=20,
        )
        pending_count = len([line for line in pending_result.stdout.splitlines() if line.strip()]) if pending_result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        pending_count = None

    return SharingStatus(
        repository_root=str(root),
        index_path=str(root / SHARE_INDEX_FILENAME),
        subject_count=len(subjects),
        document_count=len(documents),
        library_bytes=library_bytes,
        loose_file_count=len(loose_paths),
        loose_bytes=sum(path.stat().st_size for path in loose_paths),
        total_file_count=len(all_paths),
        total_bytes=sum(path.stat().st_size for path in all_paths),
        large_file_count=len(large_paths),
        blocked_file_count=len(blocked_paths),
        large_files=[file_info(path) for path in sorted(large_paths, key=lambda item: item.stat().st_size, reverse=True)[:20]],
        blocked_files=[file_info(path) for path in sorted(blocked_paths, key=lambda item: item.stat().st_size, reverse=True)],
        git_available=git_available,
        git_lfs_installed=_git_lfs_installed(root),
        pending_file_count=pending_count,
    )


def _markdown_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _display_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


def write_share_index(library: LibraryStore, repository_root: str | Path) -> Path:
    """Write a navigable GitHub Markdown index without copying source files."""
    root = Path(repository_root).resolve()
    subjects = library.list_subjects()
    documents = library.list_documents()
    lines = [
        "# Shared course library",
        "",
        "This catalog links to the source materials stored in this repository. Clone the repository to use the full local Class Knowledge app.",
        "",
        f"Subjects: **{len(subjects)}** · Library documents: **{len(documents)}**",
        "",
        "## Subject library",
        "",
    ]
    for subject in subjects:
        subject_documents = [item for item in documents if item.subject_id == subject.id]
        lines.extend([f"### {_markdown_label(subject.name)}", ""])
        if subject.description:
            lines.extend([subject.description, ""])
        if not subject_documents:
            lines.extend(["No files yet.", ""])
            continue
        folders = library.list_folders(subject.id)
        grouped: dict[str | None, list[LibraryDocument]] = {}
        for document in subject_documents:
            grouped.setdefault(document.folder_id, []).append(document)
        folder_names = {folder.id: folder.name for folder in folders}
        order: list[str | None] = [None] + [folder.id for folder in folders]
        for folder_id in order:
            folder_documents = grouped.get(folder_id, [])
            if not folder_documents:
                continue
            if folder_id is not None:
                lines.extend([f"**{_markdown_label(folder_names[folder_id])}**", ""])
            for document in sorted(folder_documents, key=lambda item: item.original_name.casefold()):
                path = _document_path(library, document)
                if path is None or not path.is_file():
                    continue
                relative_path = path.relative_to(root).as_posix()
                target = quote(relative_path, safe="/-_.~")
                label = _markdown_label(document.original_name)
                lines.append(f"- [{label}]({target}) · {_display_size(path.stat().st_size)}")
            lines.append("")

    loose_files = _shareable_loose_files(root)
    if loose_files:
        lines.extend(["## Other course files", ""])
        for path in loose_files:
            relative_path = path.relative_to(root).as_posix()
            target = quote(relative_path, safe="/-_.~")
            label = _markdown_label(relative_path)
            lines.append(f"- [{label}]({target}) · {_display_size(path.stat().st_size)}")
        lines.append("")

    destination = root / SHARE_INDEX_FILENAME
    temporary = destination.with_suffix(".md.part")
    temporary.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination


def prepare_share_snapshot(library: LibraryStore, repository_root: str | Path) -> SharingStatus:
    """Checkpoint the shared catalog, refresh its index, and report readiness."""
    try:
        with sqlite3.connect(library.database_path, timeout=20) as connection:
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError(f"Could not prepare the library database for sharing: {exc}") from exc
    if checkpoint and checkpoint[0]:
        raise RuntimeError("The library database is busy; close active library work and refresh the share index again.")
    write_share_index(library, repository_root)
    return collect_sharing_status(library, repository_root)
