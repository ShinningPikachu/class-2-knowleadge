"""Offline text-to-speech support for short library explanations."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


class VoiceNarrationError(RuntimeError):
    """Raised when a local voice engine cannot create playable narration."""


SYSTEM_DEFAULT_VOICE = "System default"
# Keep browser-memory audio comfortably bounded. Longer PDFs remain available
# one page at a time, and the UI makes that continuation explicit.
MAX_NARRATION_CHARS = 9_000


def prepare_narration(text: str, maximum_chars: int = MAX_NARRATION_CHARS) -> tuple[str, bool]:
    """Normalize narration text and clip it only at a natural word boundary."""
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        raise VoiceNarrationError("There is no extractable text available to narrate.")
    if len(normalized) <= maximum_chars:
        return normalized, False
    boundary = normalized.rfind(" ", 0, maximum_chars)
    if boundary < maximum_chars // 2:
        boundary = maximum_chars
    return normalized[:boundary].rstrip(" ,;:") + "…", True


@lru_cache(maxsize=1)
def local_voice_options() -> tuple[str, ...]:
    """Return locally installed voices, beginning with the system default."""
    if shutil.which("say"):
        try:
            result = subprocess.run(
                ["say", "-v", "?"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return (SYSTEM_DEFAULT_VOICE,)
        names = [
            match.group(1).strip()
            for line in result.stdout.splitlines()
            if (match := re.match(r"^(.+?)\s{2,}\S+\s+#", line))
        ]
        # The local voice catalog can be very long. Prefer the common English
        # voices first while retaining every discovered voice for other lessons.
        preferred = [name for name in ("Samantha", "Ava", "Alex") if name in names]
        remaining = [name for name in names if name not in preferred]
        return tuple(dict.fromkeys([SYSTEM_DEFAULT_VOICE, *preferred, *remaining]))
    if shutil.which("espeak-ng") or shutil.which("espeak"):
        return (SYSTEM_DEFAULT_VOICE,)
    return ()


def synthesize_speech(text: str, voice: str = SYSTEM_DEFAULT_VOICE) -> bytes:
    """Create a WAV narration with a locally installed speech engine."""
    narration, _ = prepare_narration(text)
    if shutil.which("say"):
        return _synthesize_with_macos_say(narration, voice)
    espeak = shutil.which("espeak-ng") or shutil.which("espeak")
    if espeak:
        return _synthesize_with_espeak(espeak, narration, voice)
    raise VoiceNarrationError(
        "No local voice engine was found. On macOS, enable the built-in 'say' command; "
        "on Linux, install espeak-ng."
    )


def _synthesize_with_macos_say(text: str, voice: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="class-knowledge-voice-") as directory:
        root = Path(directory)
        text_path = root / "narration.txt"
        output_path = root / "narration.wav"
        text_path.write_text(text, encoding="utf-8")
        command = ["say"]
        if voice and voice != SYSTEM_DEFAULT_VOICE:
            command.extend(["-v", voice])
        command.extend(
            [
                "-f",
                str(text_path),
                "-o",
                str(output_path),
                "--file-format=WAVE",
                "--data-format=LEI16@22050",
            ]
        )
        _run_voice_command(command)
        return _read_wav(output_path)


def _synthesize_with_espeak(executable: str, text: str, voice: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="class-knowledge-voice-") as directory:
        output_path = Path(directory) / "narration.wav"
        command = [executable, "-w", str(output_path)]
        if voice and voice != SYSTEM_DEFAULT_VOICE:
            command.extend(["-v", voice])
        command.append(text)
        _run_voice_command(command)
        return _read_wav(output_path)


def _run_voice_command(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise VoiceNarrationError("The local voice engine took too long to create this narration.") from exc
    except OSError as exc:
        raise VoiceNarrationError(f"Could not start the local voice engine: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f" Details: {detail}" if detail else ""
        raise VoiceNarrationError(f"The local voice engine could not create narration.{suffix}") from exc


def _read_wav(path: Path) -> bytes:
    try:
        audio = path.read_bytes()
    except OSError as exc:
        raise VoiceNarrationError(f"The local voice engine did not produce an audio file: {exc}") from exc
    if len(audio) < 44 or not audio.startswith(b"RIFF") or audio[8:12] != b"WAVE":
        raise VoiceNarrationError("The local voice engine returned an invalid WAV audio file.")
    return audio
