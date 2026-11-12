"""Plan or regenerate historical lecture notes using local Whisper and Ollama.

Run from the repository root after `git lfs pull`, model installation, and
`python3 scripts/validate_library.py --hashes`.

    python3 scripts/rebuild_legacy_lectures.py                # dry run
    python3 scripts/rebuild_legacy_lectures.py --folder ID --execute
    python3 scripts/rebuild_legacy_lectures.py --all --execute

By default nothing changes. Explicit execution regenerates transcription,
slide alignment, per-slide explanations, and Notes/Summary PDFs, preserving
existing document IDs and private backups in ignored runs/.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.library import LibraryStore  # noqa: E402
from src.lecture_artifacts import save_portable_artifacts  # noqa: E402


RECORDINGS = (".m4a", ".mp3", ".wav", ".flac", ".ogg", ".opus", ".aac", ".mp4", ".mov", ".webm", ".m4v", ".mkv")
SLIDES = (".pdf", ".pptx", ".ppt")


@dataclass(frozen=True)
class LectureCandidate:
    subject_id: str
    folder_id: str
    title: str
    deck_id: str
    recording_id: str
    reasons: tuple[str, ...]


def candidates(library: LibraryStore) -> list[LectureCandidate]:
    result: list[LectureCandidate] = []
    for subject in library.list_subjects():
        documents = library.list_documents(subject.id)
        for folder in library.list_folders(subject.id):
            grouped = [doc for doc in documents if doc.folder_id == folder.id]
            recordings = [doc for doc in grouped if doc.stored_path.suffix.lower() in RECORDINGS]
            decks = [
                doc for doc in grouped
                if doc.stored_path.suffix.lower() in SLIDES
                and "notes" not in doc.original_name.casefold()
                and "summary" not in doc.original_name.casefold()
            ]
            if len(recordings) != 1 or len(decks) != 1:
                continue
            derived = library.subjects_root / subject.id / "derived" / folder.id
            reasons: list[str] = []
            alignment_path = derived / "alignment.json"
            summaries_path = derived / "slide_summaries.json"
            try:
                alignment = json.loads(alignment_path.read_text(encoding="utf-8"))
                if int(alignment.get("metadata", {}).get("alignment_version", 0)) < 2:
                    reasons.append("legacy non-sequential alignment")
            except (OSError, ValueError, TypeError):
                reasons.append("missing or invalid alignment")
            try:
                summaries = json.loads(summaries_path.read_text(encoding="utf-8")).get("slides", [])
                if any(not isinstance(x.get("sources"), dict) for x in summaries):
                    reasons.append("missing source coverage")
            except (OSError, ValueError, TypeError):
                reasons.append("missing or invalid slide explanations")
            if reasons:
                result.append(LectureCandidate(
                    subject.id, folder.id, f"{subject.name} / {folder.name}",
                    decks[0].id, recordings[0].id, tuple(reasons),
                ))
    return result


def rebuild(root: Path, library: LibraryStore, lecture: LectureCandidate) -> tuple[Path, Path]:
    """Process originals before touching any previously published material."""
    from src.config import PipelineConfig
    from src.pipeline import LecturePipeline

    slides = library.get_document(lecture.deck_id)
    recording = library.get_document(lecture.recording_id)
    if recording.stored_path.stat().st_size != recording.size_bytes:
        raise RuntimeError("Recording size differs from catalog. Run git lfs pull first.")
    with recording.stored_path.open("rb") as stream:
        if stream.read(43).startswith(b"version https://git-lfs.github.com"):
            raise RuntimeError("Recording is a Git LFS pointer. Run git lfs pull first.")
    result = LecturePipeline(PipelineConfig(), project_root=root).run(
        recording.stored_path, slides.stored_path, lecture.title,
        on_stage=lambda stage, message: print(f"[{stage}] {message}", flush=True),
    )
    derived = library.subjects_root / lecture.subject_id / "derived" / lecture.folder_id
    backup = root / "runs" / "rebuild_backups" / lecture.subject_id / lecture.folder_id / result.run_id
    backup.mkdir(parents=True, exist_ok=False)
    if derived.is_dir():
        shutil.copytree(derived, backup / "derived")
    documents = [
        doc for doc in library.list_documents(lecture.subject_id)
        if doc.folder_id == lecture.folder_id
    ]
    existing = {
        doc.original_name.casefold(): doc
        for doc in documents if doc.original_name.casefold() in {"notes.pdf", "summary.pdf"}
    }
    old_pdfs: dict[str, Path] = {}
    for name, doc in existing.items():
        original = backup / f"{doc.id}.pdf"
        shutil.copy2(doc.stored_path, original)
        old_pdfs[name] = original

    modified: list[str] = []
    created: list[str] = []
    try:
        save_portable_artifacts(library, lecture.subject_id, lecture.folder_id, result.run_dir)
        for name, output in (
            ("notes.pdf", result.pdf_path),
            ("summary.pdf", result.lecture_summary_pdf_path),
        ):
            if name in existing:
                library.replace_document_contents(existing[name].id, output)
                modified.append(name)
            else:
                doc = library.add_document(
                    lecture.subject_id, output, filename="Notes.pdf" if name == "notes.pdf" else "Summary.pdf",
                    folder_id=lecture.folder_id,
                )
                created.append(doc.id)
    except Exception:
        # Restore the original catalog and published artifacts, including the
        # learner-facing PDFs, if any publishing step fails.
        for doc_id in reversed(created):
            library.delete_document(doc_id)
        for name in reversed(modified):
            library.replace_document_contents(existing[name].id, old_pdfs[name])
        if derived.is_dir():
            shutil.rmtree(derived)
        if (backup / "derived").is_dir():
            shutil.copytree(backup / "derived", derived)
        raise
    return result.run_dir, backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--folder", help="Stable lecture folder ID to regenerate")
    choice.add_argument("--all", action="store_true", help="Select all stale recorded lectures")
    parser.add_argument("--execute", action="store_true", help="Actually run local models and replace published notes")
    args = parser.parse_args()
    root = args.root.resolve()
    library = LibraryStore(root / "library")
    planned = candidates(library)
    if args.folder:
        planned = [item for item in planned if item.folder_id == args.folder]
    for item in planned:
        print(f"{item.folder_id} | {item.title} | {', '.join(item.reasons)}")
    print(f"{len(planned)} lecture(s) need regeneration.")
    if not args.execute:
        print("Dry run. Add --folder ID --execute (recommended) or --all --execute after reviewing sources.")
        return 0
    if not args.folder and not args.all:
        parser.error("Choose --folder or --all when using --execute.")
    for item in planned:
        run_dir, backup = rebuild(root, library, item)
        print(f"REBUILT {item.title} | run={run_dir} | backup={backup}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
