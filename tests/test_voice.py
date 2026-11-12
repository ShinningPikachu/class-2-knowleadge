"""Tests for the offline neural voice-narration backend."""

from __future__ import annotations

import io
from types import SimpleNamespace
import tempfile
import wave
import unittest
from pathlib import Path
from unittest.mock import patch

from src.voice import (
    DEFAULT_PIPER_VOICE,
    VoiceNarrationError,
    _narration_chunks,
    _piper_voice_path,
    _read_wav,
    _synthesize_with_piper,
    local_voice_options,
    prepare_narration,
    synthesize_speech,
    wav_duration_seconds,
)


def _wav(seconds: float = 1.0) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setparams((1, 2, 22_050, 0, "NONE", "not compressed"))
        output.writeframes(b"\x01\x00" * int(22_050 * seconds))
    return buffer.getvalue()


class VoiceNarrationTest(unittest.TestCase):
    def test_prepare_narration_normalizes_and_clips_at_a_word_boundary(self) -> None:
        normalized, truncated = prepare_narration("  First line.\nSecond line.  ")
        self.assertEqual(normalized, "First line. Second line.")
        self.assertFalse(truncated)

        clipped, truncated = prepare_narration("alpha beta gamma delta", maximum_chars=12)
        self.assertEqual(clipped, "alpha beta…")
        self.assertTrue(truncated)

    def test_piper_backend_writes_pcm_wav_and_disables_raw_phonemes(self) -> None:
        received: list[tuple[str, bool]] = []

        class FakePiperModel:
            config = SimpleNamespace(sample_rate=22_050)

            def synthesize_wav(self, text: str, wav: wave.Wave_write, *, set_wav_format: bool) -> None:
                received.append((text, set_wav_format))
                wav.writeframes(b"\x01\x00" * 22_050)

        with patch("src.voice._piper_voice_path", return_value=Path("voice.onnx")), patch(
            "src.voice._load_piper_voice", return_value=FakePiperModel()
        ):
            audio = _synthesize_with_piper("Nested list: [['John', 34]].", DEFAULT_PIPER_VOICE)

        self.assertEqual(received, [("Nested list: open bracket open bracket'John', 34close bracket close bracket.", False)])
        with wave.open(io.BytesIO(audio), "rb") as output:
            self.assertEqual((output.getnchannels(), output.getsampwidth(), output.getframerate()), (1, 2, 22_050))
            self.assertEqual(output.getnframes(), 22_050)

    def test_empty_silent_and_truncated_audio_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.wav"
            for frames, error in ((b"", "empty"), (b"\0" * 100, "silent")):
                with wave.open(str(path), "wb") as output:
                    output.setparams((1, 2, 22_050, 0, "NONE", "not compressed"))
                    output.writeframes(frames)
                with self.assertRaisesRegex(VoiceNarrationError, error):
                    _read_wav(path)
            with wave.open(str(path), "wb") as output:
                output.setparams((1, 2, 22_050, 0, "NONE", "not compressed"))
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
        with patch("src.voice._piper_voice_path", return_value=Path("voice.onnx")), patch(
            "src.voice._synthesize_with_piper", return_value=_wav(20)
        ) as backend:
            audio = synthesize_speech(text)
        self.assertEqual([call.args[0] for call in backend.call_args_list], chunks)
        with wave.open(io.BytesIO(audio), "rb") as output:
            self.assertEqual(output.getnframes(), 22_050 * 20 * len(chunks))

    def test_short_fragment_is_retried_and_never_returned_as_complete(self) -> None:
        with patch("src.voice._piper_voice_path", return_value=Path("voice.onnx")), patch(
            "src.voice._synthesize_with_piper", return_value=_wav(0.1)
        ) as backend:
            with self.assertRaisesRegex(VoiceNarrationError, "stopped early"):
                synthesize_speech("This narration must finish every spoken word.")
        self.assertEqual(backend.call_count, 2)

    def test_truncated_long_segment_recovers_with_smaller_complete_segments(self) -> None:
        calls: list[str] = []

        def flaky_backend(text: str, _voice: str) -> bytes:
            calls.append(text)
            if len(text) > 48:
                return _wav(0.1)
            return _wav(max(1.0, len(text.split()) * 0.25))

        narration = "Every word in this slide must survive even when the voice model truncates a longer request."
        with patch("src.voice._piper_voice_path", return_value=Path("voice.onnx")), patch(
            "src.voice._synthesize_with_piper", side_effect=flaky_backend
        ):
            audio = synthesize_speech(narration)

        self.assertGreater(len(calls), 2)
        self.assertGreater(wav_duration_seconds(audio), 2.0)

    def test_native_voice_setup_returns_downloaded_voice_options(self) -> None:
        from src.native_backend import NativeBackend

        backend = NativeBackend.__new__(NativeBackend)
        with patch("src.voice.install_default_voice") as download, patch(
            "src.voice.local_voice_options", return_value=(DEFAULT_PIPER_VOICE,)
        ), patch("src.voice.neural_voice_setup_message", return_value=None):
            options = backend.dispatch("voice.install_default", {})

        download.assert_called_once_with()
        self.assertEqual(options, {
            "voices": [DEFAULT_PIPER_VOICE],
            "setup_message": "",
        })

    def test_native_synthesis_reads_entire_text_unless_limit_is_explicit(self) -> None:
        from src.native_backend import NativeBackend

        backend = NativeBackend.__new__(NativeBackend)
        narration = ("This is an explanation for a lecture slide. " * 220).strip()
        with patch("src.voice.synthesize_speech", return_value=_wav(5)) as synth:
            response = backend.dispatch("voice.synthesize", {
                "text": narration,
                "voice": DEFAULT_PIPER_VOICE,
            })
        self.assertFalse(response["truncated"])
        self.assertEqual(response["characters"], len(narration))
        synth.assert_called_once_with(narration, DEFAULT_PIPER_VOICE)

        with patch("src.voice.synthesize_speech", return_value=_wav(5)) as synth:
            response = backend.dispatch("voice.synthesize", {
                "text": narration,
                "maximum_chars": 100,
                "voice": DEFAULT_PIPER_VOICE,
            })
        self.assertTrue(response["truncated"])
        self.assertLessEqual(response["characters"], 101)
        self.assertTrue(synth.called)

        with self.assertRaisesRegex(ValueError, "positive integer"):
            backend.dispatch("voice.synthesize", {"text": narration, "maximum_chars": 0})

    def test_missing_neural_voice_model_is_explained(self) -> None:
        with patch("src.voice._installed_voice_models", return_value={}):
            with self.assertRaisesRegex(VoiceNarrationError, "not installed"):
                _piper_voice_path(DEFAULT_PIPER_VOICE)

    def test_voice_picker_requires_model_and_matching_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "src.voice._voice_models_root", return_value=Path(directory)
        ):
            root = Path(directory)
            (root / "valid.onnx").write_bytes(b"model")
            (root / "valid.onnx.json").write_text("{}", encoding="utf-8")
            (root / "missing-config.onnx").write_bytes(b"model")
            local_voice_options.cache_clear()
            self.assertEqual(local_voice_options(), ("valid",))
            local_voice_options.cache_clear()


if __name__ == "__main__":
    unittest.main()
