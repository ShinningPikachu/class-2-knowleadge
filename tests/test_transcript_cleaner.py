"""Tests for grounded, resumable transcript readability cleanup."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.config import PipelineConfig
from src.task_control import TaskControlSignal
from src.transcript_cleaner import TranscriptCleaner, TranscriptCleanupError


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


class FilteringCleanupClient(EchoCleanupClient):
    def chat(self, **kwargs: object) -> dict[str, dict[str, str]]:
        self.calls += 1
        messages = kwargs["messages"]  # type: ignore[index]
        prompt = messages[-1]["content"]  # type: ignore[index]
        source, _ = json.JSONDecoder().raw_decode(prompt.split("INPUT JSON:\n", 1)[1])
        cleaned = [
            {
                "id": item["id"],
                "text": "" if item["id"] == 2 else item["text"].strip().capitalize() + ".",
                "keep": item["id"] != 2,
                "reason": "personal background conversation" if item["id"] == 2 else "lecture content",
            }
            for item in source["paragraphs"]
        ]
        return {"message": {"content": json.dumps({"paragraphs": cleaned})}}


class InvalidLargeBatchCleanupClient(EchoCleanupClient):
    """Returns invalid IDs for multi-paragraph requests, then succeeds for singles."""

    def __init__(self) -> None:
        super().__init__()
        self.requested_ids: list[list[int]] = []

    def chat(self, **kwargs: object) -> dict[str, dict[str, str]]:
        self.calls += 1
        messages = kwargs["messages"]  # type: ignore[index]
        prompt = messages[-1]["content"]  # type: ignore[index]
        source, _ = json.JSONDecoder().raw_decode(prompt.split("INPUT JSON:\n", 1)[1])
        ids = [int(item["id"]) for item in source["paragraphs"]]
        self.requested_ids.append(ids)
        if len(ids) > 1:
            # This is valid JSON but violates the cleaner's exact-ID contract.
            return {"message": {"content": json.dumps({"paragraphs": []})}}
        item = source["paragraphs"][0]
        return {
            "message": {
                "content": json.dumps(
                    {"paragraphs": [{"id": item["id"], "text": item["text"].strip().capitalize() + "."}]}
                )
            }
        }


class InvalidCleanupClient(InvalidLargeBatchCleanupClient):
    """Always violates the exact-ID contract, including for one paragraph."""

    def chat(self, **kwargs: object) -> dict[str, dict[str, str]]:
        self.calls += 1
        return {"message": {"content": json.dumps({"paragraphs": []})}}


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

    @staticmethod
    def _short_raw_transcript() -> dict[str, object]:
        return {
            "metadata": {"source_file": "lecture.wav", "language": "en"},
            "segments": [],
            "paragraphs": [
                {
                    "id": index,
                    "start": float(index),
                    "end": float(index + 1),
                    "start_time": f"00:00:{index:02d}",
                    "end_time": f"00:00:{index + 1:02d}",
                    "text": f"lecture paragraph {index} " * 25,
                }
                for index in range(1, 5)
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

    def test_cleanup_removes_clearly_unrelated_speech_and_records_reason(self) -> None:
        raw = self._raw_transcript()
        with tempfile.TemporaryDirectory() as directory:
            result = self._cleaner(FilteringCleanupClient()).clean(
                raw,
                Path(directory) / "transcript.json",
                lecture_context="Lecture: Search algorithms",
            )

        self.assertEqual([item["id"] for item in result["paragraphs"]], [1])
        self.assertEqual(result["metadata"]["kept_paragraph_count"], 1)
        self.assertEqual(result["metadata"]["removed_paragraph_count"], 1)
        self.assertEqual(
            result["removed_paragraphs"],
            [{"id": 2, "reason": "personal background conversation"}],
        )

    def test_cleanup_splits_only_an_invalid_batch_into_smaller_pieces(self) -> None:
        raw = self._short_raw_transcript()
        client = InvalidLargeBatchCleanupClient()
        progress: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            result = self._cleaner(client).clean(
                raw,
                Path(directory) / "transcript.json",
                progress=lambda _index, _total, message: progress.append(message),
            )

        self.assertEqual([item["id"] for item in result["paragraphs"]], [1, 2, 3, 4])
        self.assertEqual(
            client.requested_ids,
            [[1, 2, 3], [1, 2, 3], [1], [2, 3], [2, 3], [2], [3], [4]],
        )
        self.assertTrue(any("retrying its 3 paragraphs as 1- and 2-paragraph pieces" in message for message in progress))
        self.assertTrue(any("retrying its 2 paragraphs as 1- and 1-paragraph pieces" in message for message in progress))

    def test_cleanup_still_fails_safely_when_one_paragraph_cannot_be_validated(self) -> None:
        raw = self._short_raw_transcript()
        raw["paragraphs"] = raw["paragraphs"][:1]  # type: ignore[index]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(TranscriptCleanupError, "batch 1 returned unsafe or invalid content twice"):
                self._cleaner(InvalidCleanupClient()).clean(raw, Path(directory) / "transcript.json")


if __name__ == "__main__":
    unittest.main()
