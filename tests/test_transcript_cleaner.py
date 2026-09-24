"""Tests for grounded, resumable transcript readability cleanup."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.config import PipelineConfig
from src.task_control import TaskControlSignal
from src.transcript_cleaner import TranscriptCleaner


class StopForLater(TaskControlSignal):
    pass


class EchoCleanupClient:
    def __init__(self, stop_on_call: int | None = None) -> None:
        self.calls = 0
        self.stop_on_call = stop_on_call

    def chat(self, **kwargs: object) -> dict[str, dict[str, str]]:
        self.calls += 1
        if self.stop_on_call == self.calls:
            raise StopForLater("pause transcript cleanup")
        messages = kwargs["messages"]  # type: ignore[index]
        prompt = messages[-1]["content"]  # type: ignore[index]
        source = json.loads(prompt.split("INPUT JSON:\n", 1)[1])
        cleaned = [
            {"id": item["id"], "text": item["text"].strip().capitalize() + "."}
            for item in source["paragraphs"]
        ]
        return {"message": {"content": json.dumps({"paragraphs": cleaned})}}


class TranscriptCleanerTest(unittest.TestCase):
    @staticmethod
    def _cleaner(client: object) -> TranscriptCleaner:
        cleaner = TranscriptCleaner.__new__(TranscriptCleaner)
        cleaner.config = PipelineConfig(transcript_cleanup_batch_chars=2_000)
        cleaner._chat_guard = None
        cleaner._client = client
        return cleaner

    @staticmethod
    def _raw_transcript() -> dict[str, object]:
        first = "this is broken lecture text " * 55
        second = "another fragment from noisy audio " * 50
        return {
            "metadata": {"source_file": "lecture.wav", "language": "en"},
            "segments": [{"id": 1, "text": "raw segment remains untouched"}],
            "paragraphs": [
                {
                    "id": 1,
                    "start": 1.0,
                    "end": 20.0,
                    "start_time": "00:00:01",
                    "end_time": "00:00:20",
                    "text": first,
                },
                {
                    "id": 2,
                    "start": 21.0,
                    "end": 40.0,
                    "start_time": "00:00:21",
                    "end_time": "00:00:40",
                    "text": second,
                },
            ],
        }

    def test_cleanup_preserves_timestamps_ids_and_raw_segments(self) -> None:
        raw = self._raw_transcript()
        client = EchoCleanupClient()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "transcript.json"
            result = self._cleaner(client).clean(raw, output)

            self.assertEqual([item["id"] for item in result["paragraphs"]], [1, 2])
            self.assertEqual(result["paragraphs"][0]["start_time"], "00:00:01")
            self.assertEqual(result["segments"], [])
            self.assertEqual(raw["segments"], [{"id": 1, "text": "raw segment remains untouched"}])
            self.assertEqual(result["metadata"]["transcript_kind"], "cleaned")
            self.assertEqual(result["metadata"]["raw_transcript_file"], "transcript.raw.json")
            self.assertFalse(result["metadata"]["is_partial"])
            self.assertEqual(client.calls, 2)
            self.assertTrue((Path(directory) / "transcript.cleanup.partial.json").is_file())

    def test_interrupted_cleanup_resumes_from_batch_checkpoint(self) -> None:
        raw = self._raw_transcript()
        first_client = EchoCleanupClient(stop_on_call=2)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "transcript.json"
            cleaner = self._cleaner(first_client)
            with self.assertRaisesRegex(StopForLater, "pause transcript cleanup"):
                cleaner.clean(raw, output)

            partial = json.loads(
                (Path(directory) / "transcript.cleanup.partial.json").read_text(encoding="utf-8")
            )
            self.assertEqual(partial["metadata"]["cleanup_completed_batches"], 1)
            self.assertEqual([item["id"] for item in partial["paragraphs"]], [1])

            resumed_client = EchoCleanupClient()
            resumed = self._cleaner(resumed_client).clean(raw, output)
            self.assertEqual(resumed_client.calls, 1)
            self.assertEqual([item["id"] for item in resumed["paragraphs"]], [1, 2])

    def test_changed_source_invalidates_only_affected_cleanup_work(self) -> None:
        raw = self._raw_transcript()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "transcript.json"
            first_client = EchoCleanupClient()
            first = self._cleaner(first_client).clean(raw, output)

            changed = self._raw_transcript()
            changed["paragraphs"][0]["text"] = "changed words from repaired audio " * 55  # type: ignore[index]
            resumed_client = EchoCleanupClient()
            resumed = self._cleaner(resumed_client).clean(changed, output)

            self.assertEqual(first_client.calls, 2)
            self.assertEqual(resumed_client.calls, 1)
            self.assertNotEqual(
                first["metadata"]["cleanup_source_digest"],
                resumed["metadata"]["cleanup_source_digest"],
            )
            self.assertTrue(resumed["paragraphs"][0]["text"].startswith("Changed words"))


if __name__ == "__main__":
    unittest.main()
