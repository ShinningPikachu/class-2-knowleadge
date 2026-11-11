"""Backfill portable slide links and transcripts from completed local runs.

Run from the repository root: python3 scripts/export_lecture_artifacts.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.lecture_artifacts import ARTIFACT_NAMES, save_portable_artifacts  # noqa: E402
from src.library import LibraryStore  # noqa: E402


def main() -> None:
    library = LibraryStore(ROOT / "library")
    database = sqlite3.connect(ROOT / "library" / "library.sqlite3")
    documents = database.execute("SELECT subject_id, folder_id, sha256 FROM documents WHERE folder_id IS NOT NULL").fetchall()
    by_hash = {digest: (subject_id, folder_id) for subject_id, folder_id, digest in documents}
    candidates: dict[tuple[str, str], Path] = {}
    for manifest_path in sorted((ROOT / "runs").glob("*/lecture_manifest.json")):
        run_dir = manifest_path.parent
        if not all((run_dir / name).is_file() for name in ARTIFACT_NAMES[:4]):
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        matches = []
        for role in ("recording", "slides_source"):
            name = Path(str(manifest.get("files", {}).get(role, ""))).name
            source = run_dir / "input" / name
            if source.is_file():
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                if digest in by_hash:
                    matches.append(by_hash[digest])
        if len(matches) == 2 and matches[0] == matches[1]:
            # A later successful run supersedes an earlier run for the same lecture.
            candidates[matches[0]] = run_dir

    for (subject_id, folder_id), run_dir in sorted(candidates.items()):
        saved = save_portable_artifacts(library, subject_id, folder_id, run_dir)
        print(f"{folder_id}: {run_dir.name} ({', '.join(saved)})")
    print(f"Exported {len(candidates)} lecture folders.")


if __name__ == "__main__":
    main()
