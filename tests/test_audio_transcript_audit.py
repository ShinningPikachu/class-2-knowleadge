"""Raw versus cleaned transcript can be audited alongside local playback."""

import unittest
from pathlib import Path
from unittest.mock import patch

from src.ui.audio_transcript_component import render_audio_transcript


class AudioTranscriptAuditTests(unittest.TestCase):
    def test_audio_player_contains_linked_raw_text_and_review_controls(self) -> None:
        cleaned = [{"id": 8, "start": 12., "end": 15., "start_time": "00:00:12",
                    "end_time": "00:00:15", "text": "A neural network."}]
        raw = [{"id": 8, "text": "A neural net work."}]
        with patch("src.ui.audio_transcript_component._media_url", return_value="/media/recording"), \
             patch("src.ui.audio_transcript_component.components.html") as html:
            result = render_audio_transcript(
                Path("recording.m4a"), "audio/mp4", cleaned,
                key="lecture", raw_paragraphs=raw,
            )
        self.assertTrue(result)
        content = html.call_args.args[0]
        self.assertIn("A neural network.", content)
        self.assertIn("A neural net work.", content)
        self.assertIn("Compare original ASR", content)
        self.assertIn("Export flagged passages", content)
        self.assertIn("transcript_audit.json", content)

    def test_no_raw_transcript_hides_review_controls(self) -> None:
        with patch("src.ui.audio_transcript_component._media_url", return_value="/media/test"), \
             patch("src.ui.audio_transcript_component.components.html") as html:
            render_audio_transcript(
                Path("recording.m4a"), "audio/mp4",
                [{"id": 1, "start": 0., "end": 1., "text": "Some speech."}],
                key="lecture",
            )
        content = html.call_args.args[0]
        self.assertIn('if (cues.some((cue) => cue.raw)) auditTools.hidden = false;', content)


if __name__ == "__main__":
    unittest.main()
