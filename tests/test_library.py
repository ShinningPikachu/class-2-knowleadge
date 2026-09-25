"""Tests for the persistent subject library and grounded library agent."""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.config import PipelineConfig
from src.library import DuplicateDocumentError, LibraryError, LibraryStore, SearchResult
from src.library_agent import LibraryAgent


class LibraryStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.store = LibraryStore(Path(self.temporary_directory.name) / "library")

    def test_subjects_and_documents_survive_a_new_store_instance(self) -> None:
        subject = self.store.create_subject("Artificial Intelligence", "Semester one")
        document = self.store.add_document_bytes(
            subject.id,
            "search-notes.md",
            b"Breadth-first search explores the shallowest frontier before deeper nodes.",
        )

        reopened = LibraryStore(self.store.root)
        self.assertEqual([item.name for item in reopened.list_subjects()], ["Artificial Intelligence"])
        documents = reopened.list_documents(subject.id)
        self.assertEqual([item.original_name for item in documents], ["search-notes.md"])
        self.assertEqual(documents[0].status, "indexed")
        self.assertTrue(document.stored_path.is_file())
        self.assertIn(subject.id, document.stored_path.parts)

    def test_duplicate_subject_names_are_case_insensitive(self) -> None:
        self.store.create_subject("Databases")
        with self.assertRaises(LibraryError):
            self.store.create_subject("databases")

    def test_duplicate_file_is_rejected_within_one_subject(self) -> None:
        subject = self.store.create_subject("Algorithms")
        self.store.add_document_bytes(subject.id, "one.txt", b"Dijkstra shortest path")
        with self.assertRaises(DuplicateDocumentError):
            self.store.add_document_bytes(subject.id, "renamed.txt", b"Dijkstra shortest path")

    def test_same_file_can_be_kept_in_two_subjects(self) -> None:
        first = self.store.create_subject("Algorithms")
        second = self.store.create_subject("Graph Theory")
        self.store.add_document_bytes(first.id, "graph.txt", b"A graph contains vertices and edges.")
        self.store.add_document_bytes(second.id, "graph.txt", b"A graph contains vertices and edges.")
        self.assertEqual(self.store.stats()["documents"], 2)

    def test_search_is_ranked_and_can_be_scoped_to_a_subject(self) -> None:
        algorithms = self.store.create_subject("Algorithms")
        history = self.store.create_subject("History")
        self.store.add_document_bytes(
            algorithms.id,
            "graphs.txt",
            b"Breadth-first search uses a queue. Depth-first search commonly uses a stack.",
        )
        self.store.add_document_bytes(history.id, "cities.txt", b"The city queue formed outside the old gate.")

        global_results = self.store.search_documents("How does breadth-first search use a queue?")
        scoped_results = self.store.search_documents("queue", subject_id=algorithms.id)
        self.assertEqual(global_results[0].document_name, "graphs.txt")
        self.assertEqual({result.subject_id for result in scoped_results}, {algorithms.id})
        self.assertIn("queue", scoped_results[0].text.lower())

    def test_unknown_binary_file_is_stored_but_not_indexed(self) -> None:
        subject = self.store.create_subject("Media")
        document = self.store.add_document_bytes(subject.id, "recording.bin", b"\x00\x01\x02")
        self.assertEqual(document.status, "stored")
        results = self.store.search_documents("recording", subject.id)
        self.assertEqual(results[0].document_id, document.id)
        self.assertIn("does not have searchable", results[0].text)

    def test_local_path_import_is_copied_and_indexed(self) -> None:
        subject = self.store.create_subject("Networks")
        source = Path(self.temporary_directory.name) / "network.txt"
        source.write_text("Packet switching divides a message into packets.", encoding="utf-8")
        document = self.store.add_document(subject.id, source)
        source.write_text("The original later changed.", encoding="utf-8")

        self.assertEqual(document.status, "indexed")
        self.assertEqual(document.stored_path.read_text(encoding="utf-8"), "Packet switching divides a message into packets.")
        self.assertEqual(self.store.search_documents("packet switching", subject.id)[0].document_id, document.id)

    def test_rename_preserves_extension_file_and_search_index(self) -> None:
        subject = self.store.create_subject("Networks")
        document = self.store.add_document_bytes(subject.id, "week-one.txt", b"Packet switching")
        old_path = document.stored_path
        renamed = self.store.rename_document(document.id, "introduction")

        self.assertEqual(renamed.original_name, "introduction.txt")
        self.assertFalse(old_path.exists())
        self.assertTrue(renamed.stored_path.is_file())
        self.assertEqual(self.store.search_documents("packet", subject.id)[0].document_name, "introduction.txt")

    def test_rename_rejects_an_extension_change(self) -> None:
        subject = self.store.create_subject("Networks")
        document = self.store.add_document_bytes(subject.id, "week-one.txt", b"Packet switching")
        with self.assertRaises(LibraryError):
            self.store.rename_document(document.id, "week-one.pdf")

    def test_move_updates_folder_and_search_scope(self) -> None:
        first = self.store.create_subject("Introduction")
        second = self.store.create_subject("Networks")
        document = self.store.add_document_bytes(first.id, "packets.txt", b"Packet switching")
        old_path = document.stored_path
        moved = self.store.move_document(document.id, second.id)

        self.assertEqual(moved.subject_id, second.id)
        self.assertIn(second.id, moved.stored_path.parts)
        self.assertFalse(old_path.exists())
        self.assertEqual(self.store.search_documents("packet", first.id), [])
        self.assertEqual(self.store.search_documents("packet", second.id)[0].document_id, document.id)

    def test_folder_keeps_related_files_together_and_accepts_moves(self) -> None:
        subject = self.store.create_subject("Algorithms")
        lecture = self.store.create_folder(subject.id, "Lecture 3")
        slides = self.store.add_document_bytes(
            subject.id,
            "slides.pdf",
            b"not-a-real-pdf",
            folder_id=lecture.id,
        )
        recording = self.store.add_document_bytes(subject.id, "recording.mp3", b"audio-data")

        moved = self.store.move_document_to_folder(recording.id, lecture.id)
        reopened = LibraryStore(self.store.root)
        documents = reopened.list_documents(subject.id)

        self.assertEqual({item.folder_name for item in documents}, {"Lecture 3"})
        self.assertEqual(reopened.get_folder(lecture.id).document_count, 2)
        self.assertEqual(slides.stored_path.parent, moved.stored_path.parent)

    def test_document_can_be_deleted_with_its_search_index(self) -> None:
        subject = self.store.create_subject("Networks")
        document = self.store.add_document_bytes(subject.id, "packets.txt", b"Packet switching")

        deleted = self.store.delete_document(document.id)

        self.assertEqual(deleted.id, document.id)
        self.assertFalse(document.stored_path.exists())
        self.assertEqual(self.store.list_documents(subject.id), [])
        self.assertEqual(self.store.search_documents("packet", subject.id), [])

    def test_nonempty_folder_requires_explicit_content_deletion(self) -> None:
        subject = self.store.create_subject("Networks")
        folder = self.store.create_folder(subject.id, "Lecture 1")
        document = self.store.add_document_bytes(
            subject.id,
            "packets.txt",
            b"Packet switching",
            folder_id=folder.id,
        )
        folder_path = document.stored_path.parent

        with self.assertRaises(LibraryError):
            self.store.delete_folder(folder.id)

        self.store.delete_folder(folder.id, delete_documents=True)
        self.assertFalse(folder_path.exists())
        self.assertEqual(self.store.list_folders(subject.id), [])
        self.assertEqual(self.store.list_documents(subject.id), [])

    def test_existing_flat_library_is_migrated_without_moving_its_files(self) -> None:
        legacy_root = Path(self.temporary_directory.name) / "legacy-library"
        legacy_root.mkdir()
        database_path = legacy_root / "library.sqlite3"
        with sqlite3.connect(database_path) as connection:
            connection.executescript(
                """
                CREATE TABLE subjects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
                );
                CREATE TABLE documents (
                    id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
                    original_name TEXT NOT NULL, stored_path TEXT NOT NULL UNIQUE,
                    media_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL, status TEXT NOT NULL,
                    extraction_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    UNIQUE(subject_id, sha256)
                );
                INSERT INTO subjects VALUES ('subject', 'Legacy', '', 'now');
                INSERT INTO documents VALUES (
                    'document', 'subject', 'notes.txt',
                    'subjects/subject/files/document_notes.txt', 'text/plain',
                    5, 'checksum', 'indexed', '', 'now'
                );
                """
            )

        migrated = LibraryStore(legacy_root)
        document = migrated.list_documents("subject")[0]

        self.assertIsNone(document.folder_id)
        self.assertEqual(document.folder_name, "")
        self.assertEqual(migrated.list_folders("subject"), [])


class LibraryAgentTest(unittest.TestCase):
    def test_answer_uses_source_markers_and_untrusted_document_instruction(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.request = None

            def chat(self, **kwargs: object) -> dict[str, dict[str, str]]:
                self.request = kwargs
                return {"message": {"content": "A queue is used. [S1]"}}

        client = FakeClient()
        result = SearchResult(
            document_id="doc",
            document_name="graphs.txt",
            subject_id="subject",
            subject_name="Algorithms",
            locator="Text",
            text="Breadth-first search uses a queue.",
            score=4.0,
        )
        answer = LibraryAgent(PipelineConfig(), client=client).answer("What structure is used?", [result])
        self.assertEqual(answer, "A queue is used. [S1]")
        self.assertIsNotNone(client.request)
        messages = client.request["messages"]  # type: ignore[index]
        self.assertIn("untrusted reference material", messages[0]["content"])  # type: ignore[index]
        self.assertIn("[S1]", messages[1]["content"])  # type: ignore[index]

    def test_answer_holds_the_configured_qwen_guard(self) -> None:
        class FakeClient:
            def chat(self, **_kwargs: object) -> dict[str, dict[str, str]]:
                events.append("chat")
                return {"message": {"content": "Grounded answer [S1]."}}

        @contextmanager
        def guard():
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        events: list[str] = []
        result = SearchResult("doc", "notes.txt", "subject", "AI", "Text", "Evidence", 1.0)
        answer = LibraryAgent(PipelineConfig(), client=FakeClient(), chat_guard=guard).answer("Question?", [result])
        self.assertEqual(answer, "Grounded answer [S1].")
        self.assertEqual(events, ["enter", "chat", "exit"])

    def test_planner_resolves_a_rename_to_catalog_ids(self) -> None:
        class FakeClient:
            def chat(self, **kwargs: object) -> dict[str, dict[str, str]]:
                self.request = kwargs
                return {
                    "message": {
                        "content": '{"action":"rename_document","message":"Prepared.","query":"",'
                        '"document_id":"doc-1","new_name":"week-1.txt","target_subject_id":"",'
                        '"subject_name":""}'
                    }
                }

        from src.library import LibraryDocument, Subject

        subject = Subject("subject-1", "Algorithms", "", "now")
        document = LibraryDocument(
            "doc-1",
            subject.id,
            subject.name,
            "notes.txt",
            Path("notes.txt"),
            "text/plain",
            10,
            "checksum",
            "indexed",
            "",
            "now",
        )
        agent = LibraryAgent(PipelineConfig(), client=FakeClient())
        plan = agent.plan("Rename notes.txt to week-1.txt", [subject], [document])

        self.assertEqual(plan.action, "rename_document")
        self.assertEqual(plan.document_id, document.id)
        self.assertEqual(plan.new_name, "week-1.txt")
        self.assertTrue(agent.should_plan_action("Please move notes.txt to Networks"))
        self.assertTrue(agent.should_plan_action("Change the file name from notes.txt to week-1.txt"))


if __name__ == "__main__":
    unittest.main()
