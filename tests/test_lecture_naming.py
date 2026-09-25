"""Tests for meaningful, automatic lecture artifact names."""

from __future__ import annotations

import unittest

from src.lecture_naming import infer_lecture_identity


class LectureNamingTest(unittest.TestCase):
    def test_number_and_topic_are_inferred_from_a_deck(self) -> None:
        identity = infer_lecture_identity(presentation_path="week 7 - Neural Networks slides.pptx")

        self.assertEqual(identity.base_name, "Lecture_07_Neural_Networks")
        self.assertEqual(identity.display_title, "Lecture 07 — Neural Networks")

    def test_plain_title_receives_the_requested_default_sequence(self) -> None:
        identity = infer_lecture_identity("Introduction", default_number=3)

        self.assertEqual(identity.base_name, "Lecture_03_Introduction")


if __name__ == "__main__":
    unittest.main()
