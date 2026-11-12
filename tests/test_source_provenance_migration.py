"""Migrating legacy provenance never silently changes note text."""

import json
import tempfile
import unittest
from pathlib import Path

from scripts.migrate_source_provenance import migrate


class LegacySourceMigrationTests(unittest.TestCase):
    def test_dry_run_and_idempotent_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            derived = root / "library" / "subjects" / "subject" / "derived" / "lecture"
            derived.mkdir(parents=True)
            (derived / "slides.json").write_text(json.dumps({
                "slides": [{"slide": 1, "title": "Algorithms", "content": "Queues"}]
            }))
            (derived / "alignment.json").write_text(json.dumps({
                "slides": [{"slide": 1, "paragraphs": [{"text": "Teacher explanation"}]}]
            }))
            summary = derived / "slide_summaries.json"
            summary.write_text(json.dumps({
                "slides": [{"slide": 1, "summary": "Original immutable summary"}]
            }))
            before = summary.read_bytes()
            self.assertEqual(len(migrate(root)), 1)
            self.assertEqual(before, summary.read_bytes())
            self.assertEqual(len(migrate(root, apply=True)), 1)
            converted = json.loads(summary.read_text())["slides"][0]
            self.assertEqual(converted["summary"], "Original immutable summary")
            self.assertEqual(converted["sources"], {"slide": True, "teacher_audio": True})
            self.assertTrue(converted["provenance_reconstructed"])
            self.assertEqual(migrate(root, apply=True), [])
            backup = root / "runs" / "source_provenance_backups" / "subject" / "derived" / "lecture" / "slide_summaries.json"
            self.assertEqual(backup.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
