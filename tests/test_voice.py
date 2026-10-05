"""Tests for the offline voice-narration backend."""

from __future__ import annotations

import io
import wave
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from src.voice import (
    SYSTEM_DEFAULT_VOICE,
    VoiceNarrationError,
    _narration_chunks,
    _read_wav,
    prepare_narration,
    synthesize_speech,
    wav_duration_seconds,
)


class VoiceNarrationTest(unittest.TestCase):
    def test_prepare_narration_normalizes_and_clips_at_a_word_boundary(self) -> None:
        normalized, truncated = prepare_narration("  First line.\nSecond line.  ")
        self.assertEqual(normalized, "First line. Second line.")
        self.assertFalse(truncated)

        clipped, truncated = prepare_narration("alpha beta gamma delta", maximum_chars=12)
        self.assertEqual(clipped, "alpha beta…")
        self.assertTrue(truncated)

    def test_macos_voice_backend_produces_wav_audio(self) -> None:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            output.writeframes(b"\x01\x00" * 22050)
        wav = buffer.getvalue()
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

    def test_macos_nested_list_text_cannot_become_a_speech_command(self) -> None:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            output.writeframes(b"\x01\x00" * (22050 * 4))
        written_text: list[str] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            input_path = Path(command[command.index("-f") + 1])
            written_text.append(input_path.read_text(encoding="utf-8"))
            Path(command[command.index("-o") + 1]).write_bytes(buffer.getvalue())
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch(
            "src.voice.shutil.which",
            side_effect=lambda name: "/usr/bin/say" if name == "say" else None,
        ), patch("src.voice.subprocess.run", side_effect=fake_run):
            synthesize_speech("Nested list: [['John', 34], ['Mary', 19]]. Continue after it.")

        self.assertNotIn("[[", written_text[0])
        self.assertNotIn("]]", written_text[0])
        self.assertIn("open bracket open bracket", written_text[0])
        self.assertIn("Continue after it", written_text[0])

    def test_empty_silent_and_truncated_audio_are_rejected(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.wav"
            for frames, error in ((b"", "empty"), (b"\0" * 100, "silent")):
                with wave.open(str(path), "wb") as output:
                    output.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
                    output.writeframes(frames)
                with self.assertRaisesRegex(VoiceNarrationError, error):
                    _read_wav(path)
            with wave.open(str(path), "wb") as output:
                output.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
                output.writeframes(b"\x01\x00" * 100)
            path.write_bytes(path.read_bytes()[:-10])
            with self.assertRaisesRegex(VoiceNarrationError, "incomplete"):
                _read_wav(path)

    def test_full_text_is_not_clipped_and_all_segments_are_joined(self) -> None:
        text = ("A complete slide sentence with additional details. " * 250) + "Final slide words."
        prepared, clipped = prepare_narration(text)
        self.assertFalse(clipped)
        self.assertTrue(prepared.endswith("Final slide words."))
        chunks = _narration_chunks(prepared)
        self.assertEqual(" ".join(chunks), prepared)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            output.writeframes(b"\x01\x00" * (22050 * 20))
        with patch("src.voice.shutil.which", return_value="say"), patch(
            "src.voice._synthesize_with_macos_say", return_value=buffer.getvalue()
        ) as backend:
            audio = synthesize_speech(text)
        self.assertEqual([call.args[0] for call in backend.call_args_list], chunks)
        with wave.open(io.BytesIO(audio), "rb") as output:
            self.assertEqual(output.getnframes(), 22050 * 20 * len(chunks))

    def test_short_fragment_is_retried_and_never_returned_as_complete(self) -> None:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            output.writeframes(b"\x01\x00" * 2205)
        with patch("src.voice.shutil.which", return_value="say"), patch(
            "src.voice._synthesize_with_macos_say", return_value=buffer.getvalue()
        ) as backend:
            with self.assertRaisesRegex(VoiceNarrationError, "stopped early"):
                synthesize_speech("This narration must finish every spoken word.")
        self.assertEqual(backend.call_count, 2)

    def test_truncated_long_segment_recovers_with_smaller_complete_segments(self) -> None:
        calls: list[str] = []

        def wav_with_duration(seconds: float) -> bytes:
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as output:
                output.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
                output.writeframes(b"\x01\x00" * int(22050 * seconds))
            return buffer.getvalue()

        def flaky_backend(text: str, _voice: str) -> bytes:
            calls.append(text)
            if len(text) > 48:
                return wav_with_duration(0.1)
            return wav_with_duration(max(1.0, len(text.split()) * 0.25))

        narration = "Every word in this slide must survive even when the voice engine truncates a longer request."
        with patch("src.voice.shutil.which", return_value="say"), patch(
            "src.voice._synthesize_with_macos_say", side_effect=flaky_backend
        ):
            audio = synthesize_speech(narration)

        self.assertGreater(len(calls), 2)
        self.assertGreater(wav_duration_seconds(audio), 2.0)

    def test_missing_voice_engine_is_explained(self) -> None:
        with patch("src.voice.shutil.which", return_value=None):
            with self.assertRaisesRegex(VoiceNarrationError, "No local voice engine"):
                synthesize_speech("Explain this slide.", SYSTEM_DEFAULT_VOICE)


if __name__ == "__main__":
    unittest.main()
