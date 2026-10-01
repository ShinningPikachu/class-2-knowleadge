"""Tests for the read-only Codex-backed library agent."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from src.codex_agent import CodexLibraryAgent
from src.config import PipelineConfig
from src.library import LibraryDocument, SearchResult, Subject
from src.library_agent import LibraryAgentError


class CodexLibraryAgentTest(unittest.TestCase):
    def _runner(self, response: str, calls: list[tuple[list[str], dict[str, object]]]):
        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((command, kwargs))
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(response, encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        return run

    def test_answer_uses_ephemeral_read_only_codex_with_only_retrieved_evidence(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []
        response = "A queue is used. [S1]"
        with patch("src.codex_agent.shutil.which", return_value="/usr/local/bin/codex"):
            agent = CodexLibraryAgent(
                PipelineConfig(agent_provider="codex"),
                runner=self._runner(response, calls),
            )
            answer = agent.answer(
                "What structure is used?",
                [SearchResult("doc", "graphs.txt", "subject", "Algorithms", "Text", "BFS uses a queue.", 4.0)],
            )

        self.assertEqual(answer, response)
        command, kwargs = calls[0]
        self.assertEqual(command[:2], ["/usr/local/bin/codex", "exec"])
        self.assertIn("read-only", command)
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn('model_reasoning_effort="medium"', command)
        self.assertEqual(command[-1], "-")
        prompt = str(kwargs["input"])
        self.assertIn("[S1]", prompt)
        self.assertIn("BFS uses a queue.", prompt)
        self.assertNotIn("graphs.txt", " ".join(command))

    def test_selected_codex_model_and_reasoning_effort_are_sent_to_the_cli(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []
        with patch("src.codex_agent.shutil.which", return_value="/usr/local/bin/codex"):
            CodexLibraryAgent(
                PipelineConfig(
                    agent_provider="codex",
                    codex_model="gpt-5.3-codex",
                    codex_reasoning_effort="xhigh",
                ),
                runner=self._runner("Grounded answer [S1].", calls),
            ).answer(
                "Question?",
                [SearchResult("doc", "notes.txt", "subject", "AI", "Text", "Evidence", 1.0)],
            )

        command, _kwargs = calls[0]
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.3-codex")
        self.assertEqual(
            command[command.index("--config") + 1],
            'model_reasoning_effort="xhigh"',
        )

    def test_invalid_codex_reasoning_effort_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "codex_reasoning_effort"):
            PipelineConfig(codex_reasoning_effort="deep").validate()

    def test_plan_uses_codex_json_schema_and_keeps_write_confirmation(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []
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
        response = json.dumps(
            {
                "action": "rename_document",
                "message": "Prepared for confirmation.",
                "query": "",
                "document_id": document.id,
                "new_name": "week-1.txt",
                "target_subject_id": "",
                "subject_name": "",
            }
        )
        with patch("src.codex_agent.shutil.which", return_value="/usr/local/bin/codex"):
            plan = CodexLibraryAgent(
                PipelineConfig(agent_provider="codex"),
                runner=self._runner(response, calls),
            ).plan("Rename notes.txt to week-1.txt", [subject], [document])

        self.assertEqual(plan.action, "rename_document")
        self.assertEqual(plan.document_id, document.id)
        self.assertIn("--output-schema", calls[0][0])
        self.assertIn("Prepared for confirmation.", plan.message)

    def test_missing_codex_executable_has_a_clear_error(self) -> None:
        with patch("src.codex_agent.shutil.which", return_value=None), patch.object(
            CodexLibraryAgent, "_known_executable_paths", return_value=[]
        ):
            with self.assertRaisesRegex(LibraryAgentError, "Codex was not found"):
                CodexLibraryAgent(PipelineConfig(agent_provider="codex")).answer(
                    "Question?",
                    [SearchResult("doc", "notes.txt", "subject", "AI", "Text", "Evidence", 1.0)],
                )

    def test_finds_codex_in_a_known_gui_install_location(self) -> None:
        with patch("src.codex_agent.shutil.which", return_value=None), patch.object(
            CodexLibraryAgent,
            "_known_executable_paths",
            return_value=[Path("/Applications/Codex.app/Contents/Resources/codex")],
        ), patch.object(CodexLibraryAgent, "_is_executable", return_value=True):
            resolved = CodexLibraryAgent._resolve_executable()

        self.assertEqual(resolved, "/Applications/Codex.app/Contents/Resources/codex")


if __name__ == "__main__":
    unittest.main()
