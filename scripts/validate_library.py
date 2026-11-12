"""Check that a published library can be opened after a fresh Git clone.

No Ollama or Whisper dependency. Run after `git lfs pull`:
    python3 scripts/validate_library.py --hashes
Use --strict to treat provenance warnings as failures.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


PORTABLE_FILES = ("slides.json", "slide_summaries.json", "alignment.json", "transcript.json", "transcript.txt")
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"


def validate(root: Path, *, hashes: bool = False) -> list[tuple[str, str]]:
    library = (root / "library").resolve()
    catalog = library / "library.sqlite3"
    results: list[tuple[str, str]] = []
    if not catalog.is_file():
        return [("error", f"Missing library catalog: {catalog}")]
    try:
        connection = sqlite3.connect(catalog)
        connection.row_factory = sqlite3.Row
        with connection:
            documents = connection.execute(
                "SELECT subject_id, folder_id, stored_path, original_name, size_bytes, sha256 FROM documents"
            ).fetchall()
        connection.close()
    except sqlite3.Error as exc:
        return [("error", f"Cannot read catalog: {exc}")]
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in documents:
        stored = (library / row["stored_path"]).resolve()
        if not stored.is_relative_to(library):
            results.append(("error", f"Catalog path escapes library: {row['stored_path']}"))
            continue
        if not stored.is_file():
            results.append(("error", f"Missing stored document: {row['stored_path']}"))
            continue
        with stored.open("rb") as stream:
            head = stream.read(len(LFS_POINTER))
            if head == LFS_POINTER:
                results.append(("error", f"Git LFS payload not downloaded: {row['stored_path']}"))
                continue
        if stored.stat().st_size != row["size_bytes"]:
            results.append(("error", f"Size mismatch: {row['stored_path']}"))
        if hashes:
            digest = hashlib.sha256()
            with stored.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != row["sha256"]:
                results.append(("error", f"SHA-256 mismatch: {row['stored_path']}"))
        if row["folder_id"]:
            key = (row["subject_id"], row["folder_id"])
            grouped.setdefault(key, []).append(row)

    for (subject, folder), records in grouped.items():
        derived = library / "subjects" / subject / "derived" / folder
        source_names = [str(r["original_name"]).lower() for r in records]
        has_recording = any(n.endswith((".m4a", ".mp3", ".wav", ".flac", ".ogg", ".opus", ".aac", ".mp4", ".mov", ".webm", ".m4v", ".mkv")) for n in source_names)
        if not derived.exists():
            if has_recording:
                results.append(("error", f"Recording folder missing derived artifacts: {subject}/{folder}"))
            continue
        required = PORTABLE_FILES if has_recording else ("slide_summaries.json",)
        for name in required:
            if not (derived / name).is_file():
                results.append(("error", f"Missing {name}: {subject}/{folder}"))
        try:
            slides = json.loads((derived / "slides.json").read_text(encoding="utf-8")).get("slides", []) if (derived / "slides.json").is_file() else []
            summaries = json.loads((derived / "slide_summaries.json").read_text(encoding="utf-8")).get("slides", []) if (derived / "slide_summaries.json").is_file() else []
            alignment = json.loads((derived / "alignment.json").read_text(encoding="utf-8")) if (derived / "alignment.json").is_file() else {}
            transcript = json.loads((derived / "transcript.json").read_text(encoding="utf-8")) if (derived / "transcript.json").is_file() else {}
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            results.append(("error", f"Invalid artifact JSON: {subject}/{folder}: {exc}"))
            continue
        slide_numbers = [int(s["slide"]) for s in slides]
        summary_numbers = [int(s["slide"]) for s in summaries]
        if slides and (len(summary_numbers) != len(set(summary_numbers)) or set(summary_numbers) != set(slide_numbers)):
            results.append(("error", f"Slide explanations do not cover exactly the deck: {subject}/{folder}"))
        if any(not str(s.get("summary", "")).strip() for s in summaries):
            results.append(("error", f"Empty slide explanation: {subject}/{folder}"))
        for note in summaries:
            if not isinstance(note.get("sources"), dict):
                results.append(("warning", f"Missing source provenance on slide {note['slide']}: {subject}/{folder}"))
                break
        paragraph_ids = {p.get("id") for p in transcript.get("paragraphs", [])}
        previous = None
        for item in alignment.get("paragraph_alignment", []):
            if slide_numbers and item.get("slide") not in slide_numbers:
                results.append(("error", f"Alignment references unknown slide: {subject}/{folder}"))
            if item.get("paragraph_id") not in paragraph_ids:
                results.append(("error", f"Alignment references missing transcript paragraph: {subject}/{folder}"))
            if previous is not None and item["slide"] < previous and item.get("method") != "spoken_reference":
                results.append(("warning", f"Unanchored backwards alignment: {subject}/{folder}"))
                break
            previous = item["slide"]
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--hashes", action="store_true", help="Verify full document SHA-256 hashes, including LFS")
    parser.add_argument("--strict", action="store_true", help="Fail on provenance/alignment warnings")
    args = parser.parse_args()
    issues = validate(args.root, hashes=args.hashes)
    for level, message in issues:
        print(f"{level.upper()}: {message}")
    errors = sum(level == "error" for level, _ in issues)
    warnings = sum(level == "warning" for level, _ in issues)
    print(f"Library validation: {errors} error(s), {warnings} warning(s)")
    return 1 if errors or (args.strict and warnings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
