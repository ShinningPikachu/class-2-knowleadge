"""Dependency-free tests for deterministic lecture-pipeline helpers."""

from __future__ import annotations

from contextlib import contextmanager
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.alignment import SlideAligner  # noqa: E402
from src.agent import LectureAgent  # noqa: E402
from src.audio_processor import AudioProcessingError, AudioProcessor  # noqa: E402
from src.config import PipelineConfig  # noqa: E402
from src.embeddings import OllamaEmbedder, build_source_documents, chunk_text  # noqa: E402
from src.pipeline import LecturePipeline  # noqa: E402
from src.utils import safe_filename, seconds_to_timestamp  # noqa: E402
from src.quality import QualityGateError, validate_evidence_quality, validate_final_notes  # noqa: E402


class CoreHelpersTest(unittest.TestCase):
    def test_audio_validation_rejects_non_finite_decoder_output(self) -> None:
        import numpy as np

        with self.assertRaisesRegex(AudioProcessingError, "non-finite"):
            AudioProcessor._validate_decoded_audio(np.array([0.0, np.nan], dtype=np.float32))

        validated = AudioProcessor._validate_decoded_audio(np.array([0.25, -0.5], dtype=np.float64))
        self.assertEqual(validated.dtype, np.float32)
        self.assertTrue(validated.flags.c_contiguous)

    def test_apple_whisper_filter_is_narrowly_scoped(self) -> None:
        with warnings.catch_warnings():
            with patch("src.audio_processor.platform.system", return_value="Darwin"), patch(
                "src.audio_processor.platform.machine", return_value="arm64"
            ):
                AudioProcessor._configure_whisper_warning_filter()
            matching_filters = [
                item
                for item in warnings.filters
                if item[0] == "ignore"
                and item[2] is RuntimeWarning
                and item[1] is not None
                and item[1].match("overflow encountered in matmul")
                and item[3] is not None
                and item[3].match("faster_whisper.feature_extractor")
            ]
            self.assertEqual(len(matching_filters), 1)

    def test_overlapping_transcription_chunks_do_not_duplicate_boundary_speech(self) -> None:
        class FakeAudio:
            def __len__(self) -> int:
                return 3_600 * 16_000

            def __getitem__(self, _key: object) -> "FakeAudio":
                return self

        class FakeModel:
            calls = 0

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def transcribe(self, *_args: object, **_kwargs: object) -> tuple[list[object], object]:
                FakeModel.calls += 1
                if FakeModel.calls == 1:
                    segments = [SimpleNamespace(start=1_790.0, end=1_805.0, text="boundary sentence")]
                else:
                    segments = [
                        SimpleNamespace(start=0.0, end=10.0, text="duplicate overlap"),
                        SimpleNamespace(start=20.0, end=30.0, text="new core speech"),
                    ]
                info = SimpleNamespace(language="en", language_probability=0.99)
                return segments, info

        config = PipelineConfig(media_chunk_seconds=1_800, media_overlap_seconds=15)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "lecture.wav"
            source.touch()
            output = Path(directory) / "transcript.json"
            with patch("faster_whisper.WhisperModel", FakeModel), patch(
                "faster_whisper.audio.decode_audio", return_value=FakeAudio()
            ):
                payload = AudioProcessor(config).transcribe(source, output)
            self.assertEqual([item["text"] for item in payload["segments"]], ["boundary sentence", "new core speech"])
            self.assertEqual(payload["metadata"]["chunk_count"], 2)
            self.assertEqual(len(list((Path(directory) / "transcript_chunks").glob("*.json"))), 2)

    def test_two_hour_recording_chunk_plan_has_overlap_without_core_gaps(self) -> None:
        plans = AudioProcessor._build_chunk_plan(7_200, 1_800, 15)
        self.assertEqual(len(plans), 4)
        self.assertEqual((plans[0].core_start, plans[0].core_end), (0.0, 1_800.0))
        self.assertEqual((plans[1].extract_start, plans[1].extract_end), (1_785.0, 3_615.0))
        self.assertEqual((plans[-1].core_start, plans[-1].core_end), (5_400.0, 7_200))
        self.assertTrue(all(left.core_end == right.core_start for left, right in zip(plans, plans[1:])))

    def test_transcription_reuses_a_completed_chunk_checkpoint(self) -> None:
        class FakeAudio:
            def __len__(self) -> int:
                return 3_600 * 16_000

            def __getitem__(self, _key: object) -> "FakeAudio":
                return self

        class FakeModel:
            calls = 0

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def transcribe(self, *_args: object, **_kwargs: object) -> tuple[list[object], object]:
                FakeModel.calls += 1
                return [SimpleNamespace(start=20.0, end=30.0, text="new core speech")], SimpleNamespace(
                    language="en", language_probability=0.99
                )

        config = PipelineConfig(media_chunk_seconds=1_800, media_overlap_seconds=15, language="en")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "lecture.wav"
            source.touch()
            output = Path(directory) / "transcript.json"
            checkpoint_dir = Path(directory) / "transcript_chunks"
            checkpoint_dir.mkdir()
            (checkpoint_dir / "chunk_0001.json").write_text(
                '{"chunk": 1, "core_start": 0.0, "core_end": 1800.0, "extract_start": 0.0, '
                '"extract_end": 1815.0, "segment_count": 1, "segments": [{"id": 1, "start": 1.0, '
                '"end": 2.0, "start_time": "00:00:01", "end_time": "00:00:02", "text": "saved", "media_chunk": 1}]}',
                encoding="utf-8",
            )
            events: list[dict[str, object]] = []
            with patch("faster_whisper.WhisperModel", FakeModel), patch(
                "faster_whisper.audio.decode_audio", return_value=FakeAudio()
            ):
                payload = AudioProcessor(config).transcribe(source, output, progress=events.append)
            self.assertEqual(FakeModel.calls, 1)
            self.assertEqual(payload["metadata"]["resumed_chunks"], 1)
            self.assertEqual([item["text"] for item in payload["segments"]], ["saved", "new core speech"])
            self.assertEqual([event["event"] for event in events], ["started", "reused", "completed"])

    def test_current_ollama_batch_embedding_response(self) -> None:
        class CurrentClient:
            def embed(self, **_kwargs: object) -> dict[str, list[list[float]]]:
                return {"embeddings": [[1.0, 2.0], [3.0, 4.0]]}

        embedder = OllamaEmbedder.__new__(OllamaEmbedder)
        embedder.config = PipelineConfig()
        embedder._client = CurrentClient()
        self.assertEqual(embedder.embed_documents(["a", "b"]), [[1.0, 2.0], [3.0, 4.0]])

    def test_older_ollama_embedding_fallback(self) -> None:
        class OlderClient:
            def embed(self, **_kwargs: object) -> None:
                raise AttributeError("old client")

            def embeddings(self, **_kwargs: object) -> dict[str, list[float]]:
                return {"embedding": [0.25, 0.75]}

        embedder = OllamaEmbedder.__new__(OllamaEmbedder)
        embedder.config = PipelineConfig()
        embedder._client = OlderClient()
        self.assertEqual(embedder.embed_documents(["a", "b"]), [[0.25, 0.75], [0.25, 0.75]])

    def test_chunking_respects_size_and_overlap(self) -> None:
        chunks = chunk_text("word " * 500, size=300, overlap=50)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(chunk) <= 300 for chunk in chunks))

    def test_nearby_segments_become_one_paragraph(self) -> None:
        segments = [
            {"id": 1, "start": 0.0, "end": 1.0, "start_time": "00:00:00", "end_time": "00:00:01", "text": "Hello"},
            {"id": 2, "start": 1.2, "end": 2.0, "start_time": "00:00:01", "end_time": "00:00:02", "text": "world."},
        ]
        paragraphs = AudioProcessor._make_paragraphs(segments)
        self.assertEqual(len(paragraphs), 1)
        self.assertEqual(paragraphs[0]["text"], "Hello world.")

    def test_spoken_slide_reference(self) -> None:
        aligner = SlideAligner(PipelineConfig(), embedder=object())  # type: ignore[arg-type]
        self.assertEqual(aligner._spoken_slide_reference("Turn to slide number 8.", {1, 8}), 8)
        self.assertIsNone(aligner._spoken_slide_reference("Turn to slide 9.", {1, 8}))

    def test_alignment_metadata_is_added_to_transcript_chunks(self) -> None:
        slides = [{"slide": 1, "title": "Queues", "content": "Ready queue", "notes": "", "visual_text": []}]
        transcript = {
            "paragraphs": [{"id": 4, "start": 10.0, "end": 12.0, "text": "The scheduler selects a process."}]
        }
        alignment = {
            "paragraph_alignment": [
                {"paragraph_id": 4, "slide": 1, "method": "semantic", "confidence": 0.82}
            ]
        }
        documents = build_source_documents(slides, transcript, PipelineConfig(), alignment)
        transcript_document = next(item for item in documents if item.metadata["source"] == "transcript")
        self.assertEqual(transcript_document.metadata["aligned_slide"], 1)
        self.assertEqual(transcript_document.metadata["alignment_method"], "semantic")

    def test_timestamp_format(self) -> None:
        self.assertEqual(seconds_to_timestamp(3750), "01:02:30")

    def test_safe_filename_preserves_unicode_names(self) -> None:
        self.assertEqual(safe_filename("算法 lecture 1.pdf"), "算法_lecture_1.pdf")

    def test_hierarchical_batches_never_split_slide_blocks(self) -> None:
        blocks = ["a" * 60, "b" * 60, "c" * 60]
        self.assertEqual(LectureAgent._pack_blocks(blocks, 125), [blocks[0] + "\n\n" + blocks[1], blocks[2]])

    def test_lecture_agent_holds_qwen_guard_for_each_chat_call(self) -> None:
        class FakeClient:
            def chat(self, **_kwargs: object) -> dict[str, dict[str, str]]:
                events.append("chat")
                return {"message": {"content": "Grounded notes"}}

        @contextmanager
        def guard():
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        events: list[str] = []
        agent = LectureAgent.__new__(LectureAgent)
        agent.config = PipelineConfig()
        agent._client = FakeClient()
        agent._chat_guard = guard
        self.assertEqual(agent._chat("Write notes"), "Grounded notes")
        self.assertEqual(events, ["enter", "chat", "exit"])

    def test_quality_gate_rejects_mostly_temporal_alignment(self) -> None:
        slides = [{"slide": 1, "content": "Topic", "notes": "", "visual_text": []}]
        transcript = {"paragraphs": [{"id": index} for index in range(10)]}
        alignment = {
            "paragraph_alignment": [
                {"paragraph_id": index, "slide": 1, "method": "temporal_fallback" if index < 8 else "semantic"}
                for index in range(10)
            ],
            "slides": [{"slide": 1, "paragraph_ids": list(range(10))}],
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(QualityGateError):
                validate_evidence_quality(slides, transcript, alignment, Path(directory) / "quality.json")

    def test_slide_only_evidence_is_valid_without_a_transcript(self) -> None:
        slides = [{"slide": 1, "content": "Artificial intelligence", "notes": "", "visual_text": []}]
        transcript = {"paragraphs": []}
        alignment = {"paragraph_alignment": [], "slides": [{"slide": 1, "paragraph_ids": []}]}
        with tempfile.TemporaryDirectory() as directory:
            report = validate_evidence_quality(slides, transcript, alignment, Path(directory) / "quality.json")
        self.assertNotEqual(report["status"], "failed")

    def test_recording_only_sections_preserve_timestamped_paragraphs(self) -> None:
        transcript = {
            "paragraphs": [
                {"id": 1, "start": 10.0, "end": 20.0, "start_time": "00:00:10", "end_time": "00:00:20", "text": "First topic."},
                {"id": 2, "start": 320.0, "end": 340.0, "start_time": "00:05:20", "end_time": "00:05:40", "text": "Second topic."},
            ]
        }
        sections = LecturePipeline._recording_sections(transcript, section_seconds=300)
        self.assertEqual([section["section_kind"] for section in sections], ["recording", "recording"])
        with tempfile.TemporaryDirectory() as directory:
            alignment = LecturePipeline._recording_alignment(sections, transcript, Path(directory) / "alignment.json")
        self.assertEqual([item["slide"] for item in alignment["paragraph_alignment"]], [1, 2])
        self.assertTrue(all(item["method"] == "recording_section" for item in alignment["paragraph_alignment"]))

    def test_final_note_gate_requires_every_slide(self) -> None:
        incomplete = """# Lecture
## Overall Summary
# Slide 1: A
# Complete Lecture Summary
# Key Definitions
# Important Formulas
# Possible Exam Questions
"""
        with self.assertRaises(QualityGateError):
            validate_final_notes(incomplete, slide_count=2)

    def test_cloud_endpoints_and_models_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            PipelineConfig(ollama_host="https://ollama.com").validate()
        with self.assertRaises(ValueError):
            PipelineConfig(llm_model="gpt-oss:120b-cloud").validate()


if __name__ == "__main__":
    unittest.main()
