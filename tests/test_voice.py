"""Tests for the offline voice-narration backend."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from src.voice import SYSTEM_DEFAULT_VOICE, VoiceNarrationError, prepare_narration, synthesize_speech


class VoiceNarrationTest(unittest.TestCase):
    def test_prepare_narration_normalizes_and_clips_at_a_word_boundary(self) -> None:
        normalized, truncated = prepare_narration("  First line.\nSecond line.  ")
        self.assertEqual(normalized, "First line. Second line.")
        self.assertFalse(truncated)

        clipped, truncated = prepare_narration("alpha beta gamma delta", maximum_chars=12)
        self.assertEqual(clipped, "alpha beta…")
        self.assertTrue(truncated)

    def test_macos_voice_backend_produces_wav_audio(self) -> None:
        wav = b"RIFF" + (36).to_bytes(4, "little") + b"WAVE" + b"\0" * 32
        commands: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            output = Path(command[command.index("-o") + 1])
            output.write_bytes(wav)
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("src.voice.shutil.which", side_effect=lambda name: "/usr/bin/say" if name == "say" else None), patch(
            "src.voice.subprocess.run", side_effect=fake_run
        ):
            audio = synthesize_speech("Explain this slide.", "Samantha")

        self.assertEqual(audio, wav)
        self.assertEqual(commands[0][0], "say")
        self.assertIn("Samantha", commands[0])
        self.assertIn("--file-format=WAVE", commands[0])

    def test_missing_voice_engine_is_explained(self) -> None:
        with patch("src.voice.shutil.which", return_value=None):
            with self.assertRaisesRegex(VoiceNarrationError, "No local voice engine"):
                synthesize_speech("Explain this slide.", SYSTEM_DEFAULT_VOICE)


if __name__ == "__main__":
    unittest.main()
