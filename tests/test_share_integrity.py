"""Tests for the self-contained, post-clone integrity checker."""

import json
import tempfile
import unittest
from pathlib import Path

from scripts.validate_library import validate
from src.library import LibraryStore


class SharedLibraryValidatorTests(unittest.TestCase):
    def test_detects_missing_lfs_payload_and_alignment_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = LibraryStore(root / "library")
            subject = library.create_subject("Course")
            folder = library.create_folder(subject.id, "Lecture 1")
            library.add_document_bytes(subject.id, "Slides.pdf", b"%PDF-1.4\nfixture", folder.id)
            recording = library.add_document_bytes(subject.id, "Recording.m4a", b"recorded audio", folder.id)
            derived = library.subjects_root / subject.id / "derived" / folder.id
            derived.mkdir(parents=True)
            (derived / "slides.json").write_text(json.dumps({"slides": [{"slide": 1, "content": "Lecture"}]}))
            (derived / "slide_summaries.json").write_text(json.dumps({
                "slides": [{"slide": 1, "summary": "Lecture note", "sources": {"slide": True, "teacher_audio": True}}]
            }))
            (derived / "transcript.json").write_text(json.dumps({
                "paragraphs": [{"id": 1, "text": "Recorded words"}]
            }))
            (derived / "transcript.txt").write_text("Recorded words")
            (derived / "alignment.json").write_text(json.dumps({
                "paragraph_alignment": [{"paragraph_id": 1, "slide": 1, "method": "semantic"}],
                "slides": [{"slide": 1, "paragraph_ids": [1]}]
            }))
            self.assertEqual(validate(root, hashes=True), [])
            recording.stored_path.write_bytes(
                b"version https://git-lfs.github.com/spec/v1\noid sha256:fixture\nsize 15\n"
            )
            issues = validate(root)
            self.assertTrue(any("Git LFS payload" in message for _, message in issues))

    def test_missing_catalog_is_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(validate(Path(root))[0][0], "error")


if __name__ == "__main__":
    unittest.main()
