"""Tests for explicit, resumable translation of completed English notes."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.config import PipelineConfig
from src.task_control import TaskControlSignal
from src.translator import MarkdownTranslator, TranslationError


class StopForLater(TaskControlSignal):
    pass


class EchoTranslationClient:
    def __init__(self, stop_on_call: int | None = None) -> None:
        self.calls = 0
        self.stop_on_call = stop_on_call
        self.prompts: list[str] = []

    def chat(self, **kwargs: object) -> dict[str, dict[str, str]]:
        self.calls += 1
        if self.stop_on_call == self.calls:
            raise StopForLater("pause translation")
        messages = kwargs["messages"]  # type: ignore[index]
        prompt = messages[-1]["content"]  # type: ignore[index]
        self.prompts.append(prompt)
        source = prompt.split("ENGLISH MARKDOWN:\n", 1)[1]
        return {"message": {"content": source}}


class MarkdownTranslatorTest(unittest.TestCase):
    @staticmethod
    def _translator(client: object) -> MarkdownTranslator:
        translator = MarkdownTranslator.__new__(MarkdownTranslator)
        translator.config = PipelineConfig(ollama_num_ctx=4096)
        translator._chat_guard = None
        translator._client = client
        return translator

    def test_translation_is_created_only_when_translate_is_called(self) -> None:
        source = "# Lecture Notes\n\n## Summary\n\n- First concept\n- Second concept"
        client = EchoTranslationClient()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "notes.chinese_simplified.md"
            self.assertFalse(output.exists())

            translated = self._translator(client).translate(source, "Chinese (Simplified)", output)

            self.assertTrue(output.is_file())
            self.assertEqual(
                MarkdownTranslator._markdown_signature(source),
                MarkdownTranslator._markdown_signature(translated),
            )
            self.assertIn("Chinese (Simplified)", client.prompts[0])
            metadata = json.loads(output.with_suffix(".md.metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["source_language"], "English")
            self.assertEqual(metadata["target_language"], "Chinese (Simplified)")

    def test_interrupted_translation_resumes_from_batch_checkpoint(self) -> None:
        source = "# First\n\n" + ("First English paragraph. " * 150) + "\n\n# Second\n\n" + (
            "Second English paragraph. " * 150
        )
        first_client = EchoTranslationClient(stop_on_call=2)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "notes.french.md"
            with self.assertRaisesRegex(StopForLater, "pause translation"):
                self._translator(first_client).translate(source, "French", output)

            self.assertTrue(output.with_suffix(".partial.md").is_file())
            resumed_client = EchoTranslationClient()
            translated = self._translator(resumed_client).translate(source, "French", output)

            self.assertEqual(first_client.calls, 2)
            self.assertEqual(resumed_client.calls, 1)
            self.assertIn("# First", translated)
            self.assertIn("# Second", translated)

    def test_english_is_not_offered_as_a_translation_target(self) -> None:
        with self.assertRaisesRegex(TranslationError, "already in English"):
            MarkdownTranslator.validate_target_language("English")

    def test_translation_rejects_changed_markdown_structure(self) -> None:
        with self.assertRaisesRegex(TranslationError, "Markdown"):
            MarkdownTranslator._validate_translation("# Heading\n\n- Item", "Heading\n\nItem")


if __name__ == "__main__":
    unittest.main()
