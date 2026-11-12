"""Regression tests for chronological slide alignment and spoken revisits."""

import tempfile
import unittest
from pathlib import Path

from src.alignment import SlideAligner
from src.config import PipelineConfig
from src.quality import QualityGateError, validate_evidence_quality


class SequenceAlignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.aligner = SlideAligner(PipelineConfig(), embedder=object())

    def test_semantic_repetition_cannot_jump_to_early_slide(self) -> None:
        numbers = [1, 2, 3]
        vectors = [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]]
        paragraphs = [
            {"id": i, "start": i * 10., "end": i * 10. + 5., "text": "Lecture content"}
            for i in range(4)
        ]
        utterances = [vectors[0], vectors[1], vectors[2], vectors[0]]
        indices, references = self.aligner._sequence_path(
            numbers, vectors, paragraphs, utterances, 35.,
        )
        self.assertTrue(all(next_ >= previous for previous, next_ in zip(indices, indices[1:])))
        self.assertEqual(references, [None] * 4)
        self.assertGreater(indices[-1], 0)

    def test_explicit_spoken_reference_allows_real_revisit(self) -> None:
        vectors = [[1., 0.], [0., 1.]]
        paragraphs = [
            {"start": 0., "end": 10., "text": "Slide 2"},
            {"start": 20., "end": 30., "text": "Back to slide 1"},
        ]
        indices, references = self.aligner._sequence_path(
            [1, 2], vectors, paragraphs, [vectors[1], vectors[0]], 30.,
        )
        self.assertEqual(indices, [1, 0])
        self.assertEqual(references, [2, 1])

    def test_quality_gate_catches_unanchored_backward_jump(self) -> None:
        slides = [{"slide": number, "content": "Source"} for number in (1, 2, 3)]
        transcript = {"paragraphs": [{"id": 1}, {"id": 2}]}
        alignment = {
            "paragraph_alignment": [
                {"paragraph_id": 1, "slide": 3, "method": "semantic"},
                {"paragraph_id": 2, "slide": 1, "method": "semantic"},
            ],
            "slides": [{"slide": number, "paragraph_ids": []} for number in (1, 2, 3)],
        }
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(QualityGateError, "jump backward"):
                validate_evidence_quality(slides, transcript, alignment, Path(root) / "quality.json")


if __name__ == "__main__":
    unittest.main()
