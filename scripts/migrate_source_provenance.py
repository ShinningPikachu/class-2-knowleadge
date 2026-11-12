"""Reconstruct legacy source-coverage metadata without changing any note text.

The metadata means *evidence available*, not proof that the model used it.
Existing semantic alignment errors still require full local reprocessing.
Dry-run by default. To write corrected metadata:
    python3 scripts/migrate_source_provenance.py --apply
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from src.quality import section_source_coverage


def migrate(root: Path, *, apply: bool = False) -> list[tuple[str, int]]:
    root = root.resolve()
    updated: list[tuple[str, int]] = []
    derived_root = root / "library" / "subjects"
    for summaries_path in sorted(derived_root.glob("*/derived/*/slide_summaries.json")):
        folder = summaries_path.parent
        slides_path = folder / "slides.json"
        alignment_path = folder / "alignment.json"
        if not slides_path.is_file() or not alignment_path.is_file():
            continue  # Slide-only manual summaries have no alignment evidence.
        slides = json.loads(slides_path.read_text(encoding="utf-8")).get("slides", [])
        alignments = json.loads(alignment_path.read_text(encoding="utf-8")).get("slides", [])
        notes = json.loads(summaries_path.read_text(encoding="utf-8"))
        if not isinstance(notes.get("slides"), list):
            continue
        slide_by_number = {int(s["slide"]): s for s in slides if isinstance(s, dict)}
        audio_by_number = {int(a["slide"]): a for a in alignments if isinstance(a, dict)}
        changed = 0
        for note in notes["slides"]:
            if not isinstance(note, dict) or str(note.get("slide", "")).strip() == "":
                continue
            number = int(note["slide"])
            slide = slide_by_number.get(number)
            if slide is None:
                continue
            coverage = section_source_coverage(slide, audio_by_number.get(number))
            if not isinstance(note.get("sources"), dict) or any(
                bool(note["sources"].get(k)) != v for k, v in coverage.items()
            ):
                note["sources"] = coverage
                note["provenance_reconstructed"] = True
                changed += 1
        if changed:
            relative = summaries_path.relative_to(root).as_posix()
            updated.append((relative, changed))
            if apply:
                backup = root / "runs" / "source_provenance_backups" / summaries_path.relative_to(derived_root)
                backup.parent.mkdir(parents=True, exist_ok=True)
                if not backup.exists():
                    shutil.copy2(summaries_path, backup)
                tmp = summaries_path.with_name(summaries_path.name + ".part")
                tmp.write_text(json.dumps(notes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                tmp.replace(summaries_path)
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--apply", action="store_true", help="Save repaired metadata and keep private backups")
    options = parser.parse_args()
    changed = migrate(options.root, apply=options.apply)
    for path, count in changed:
        print(f"{count} legacy source flags: {path}")
    print(f"{len(changed)} lecture artifact(s) {'updated' if options.apply else 'need a source-metadata migration'}")
    if not options.apply:
        print("Dry run. Use --apply only after reviewing the existing library.")
    print("Source-flag migration does not correct chronological slide alignment or regenerate explanations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
