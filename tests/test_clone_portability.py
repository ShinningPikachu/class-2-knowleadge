"""A fresh checkout can use stored subject files without the author's job DB."""

import shutil
import tempfile
import unittest
from pathlib import Path

from src.library import LibraryStore


class FreshClonePortabilityTests(unittest.TestCase):
    def test_library_paths_and_portable_artifacts_survive_directory_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "author" / "library"
            dest = Path(temp) / "reader" / "library"
            store = LibraryStore(source)
            subject = store.create_subject("Example Course")
            folder = store.create_folder(subject.id, "Lecture 1")
            doc = store.add_document_bytes(
                subject.id, "Slides.pdf", b"%PDF-1.4\ninvalid dummy fixture\n", folder.id
            )
            derived = source / "subjects" / subject.id / "derived" / folder.id
            derived.mkdir(parents=True)
            (derived / "slide_summaries.json").write_text(
                '{"slides":[{"slide":1,"summary":"One note."}]}', encoding="utf-8"
            )
            # All connections are closed after library operations. Checkpoint
            # before taking a portable snapshot; do not copy active WAL state.
            with store._connect() as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            shutil.copytree(source, dest)
            reader = LibraryStore(dest)
            loaded = reader.get_document(doc.id)
            self.assertEqual(loaded.folder_id, folder.id)
            self.assertEqual(loaded.stored_path.read_bytes(), doc.stored_path.read_bytes())
            self.assertTrue(str(loaded.stored_path).startswith(str(dest)))
            from src.lecture_artifacts import artifact_dir
            self.assertTrue((artifact_dir(reader, loaded) / "slide_summaries.json").is_file())


if __name__ == "__main__":
    unittest.main()
