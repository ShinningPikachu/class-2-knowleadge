"""Offline, complete text-to-speech narration for library content."""

from __future__ import annotations

from functools import lru_cache
from collections.abc import Callable
import io
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import wave


class VoiceNarrationError(RuntimeError):
    """Raised when a local voice engine cannot create playable narration."""


SYSTEM_DEFAULT_VOICE = "System default"
NARRATION_CHUNK_CHARS = 320
MINIMUM_RECOVERY_CHARS = 48
MAXIMUM_RECOVERY_DEPTH = 4


def prepare_narration(text: str, maximum_chars: int | None = None) -> tuple[str, bool]:
    """Normalize narration, clipping only when a caller supplies an explicit limit."""
    normalized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+", " ", text)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        raise VoiceNarrationError("There is no extractable text available to narrate.")
    if maximum_chars is None or len(normalized) <= maximum_chars:
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


def _narration_chunks(text: str, limit: int = NARRATION_CHUNK_CHARS) -> list[str]:
    """Keep every character while splitting at sentence or word boundaries."""
    chunks = []
    while len(text) > limit:
        boundary = max(text.rfind(mark, 0, limit) + 1 for mark in ".!?。！？")
        if boundary < limit // 2:
            boundary = text.rfind(" ", 0, limit)
        if boundary <= 0:
            boundary = limit
        chunks.append(text[:boundary].strip())
        text = text[boundary:].strip()
    if text:
        chunks.append(text)
    return chunks


def wav_duration_seconds(audio: bytes) -> float:
    """Return the playable duration of a validated in-memory WAV."""
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            if wav.getframerate() <= 0:
                raise VoiceNarrationError("The voice engine returned audio with an invalid sample rate.")
            return wav.getnframes() / wav.getframerate()
    except (wave.Error, EOFError) as exc:
        raise VoiceNarrationError("The local voice engine returned an invalid WAV audio file.") from exc


def _minimum_duration_seconds(text: str) -> float:
    """Estimate a conservative floor that still detects a two-word truncation."""
    cjk_characters = len(re.findall(r"[\u3400-\u9fff\uf900-\ufaff]", text))
    without_cjk = re.sub(r"[\u3400-\u9fff\uf900-\ufaff]", " ", text)
    words = len(re.findall(r"\b[\w']+\b", without_cjk, flags=re.UNICODE))
    # The configured engine rate is 175 words/minute. Allow substantial room
    # for abbreviations, punctuation, URLs, and unusually fast voices.
    expected = words * 60 / 175 + cjk_characters / 4.5
    return max(0.18, expected * 0.42)


def _validate_segment_completion(text: str, audio: bytes) -> None:
    duration = wav_duration_seconds(audio)
    minimum = _minimum_duration_seconds(text)
    if duration < minimum:
        raise VoiceNarrationError(
            f"The voice engine stopped early ({duration:.1f}s generated; "
            f"at least {minimum:.1f}s expected)."
        )


def _split_for_recovery(text: str) -> list[str]:
    """Split a failed segment more aggressively without dropping any words."""
    target = max(MINIMUM_RECOVERY_CHARS, len(text) // 2)
    pieces = _narration_chunks(text, target)
    if len(pieces) > 1:
        return pieces
    midpoint = len(text) // 2
    right_boundary = text.find(" ", midpoint)
    left_boundary = text.rfind(" ", 0, midpoint)
    boundary = right_boundary if 0 < right_boundary < len(text) else left_boundary
    if boundary <= 0:
        boundary = midpoint
    return [part.strip() for part in (text[:boundary], text[boundary:]) if part.strip()]


def _synthesize_complete_segment(
    text: str,
    engine: Callable[[str, str], bytes],
    voice: str,
    *,
    depth: int = 0,
) -> list[bytes]:
    """Synthesize one segment, recursively shrinking it if an engine truncates."""
    last_error: VoiceNarrationError | None = None
    for _attempt in range(2):
        try:
            audio = engine(text, voice)
            _validate_segment_completion(text, audio)
            return [audio]
        except VoiceNarrationError as exc:
            last_error = exc

    if depth < MAXIMUM_RECOVERY_DEPTH and len(text) > MINIMUM_RECOVERY_CHARS:
        pieces = _split_for_recovery(text)
        if len(pieces) > 1:
            recovered: list[bytes] = []
            for piece in pieces:
                recovered.extend(
                    _synthesize_complete_segment(piece, engine, voice, depth=depth + 1)
                )
            return recovered

    detail = str(last_error) if last_error else "unknown voice-engine failure"
    raise VoiceNarrationError(f"The narration segment could not be completed: {detail}")


def _join_wav_segments(segments: list[bytes]) -> bytes:
    """Combine decoded PCM frames so browsers receive one valid WAV container."""
    if not segments:
        raise VoiceNarrationError("The voice engine did not return any narration segments.")
    output = io.BytesIO()
    with wave.open(output, "wb") as joined:
        parameters: tuple[int, int, int] | None = None
        for audio in segments:
            try:
                with wave.open(io.BytesIO(audio), "rb") as wav:
                    current = (wav.getnchannels(), wav.getsampwidth(), wav.getframerate())
                    if parameters is None:
                        parameters = current
                        joined.setnchannels(current[0])
                        joined.setsampwidth(current[1])
                        joined.setframerate(current[2])
                        joined.setcomptype("NONE", "not compressed")
                    elif current != parameters:
                        raise VoiceNarrationError(
                            "The voice engine changed audio format between narration segments."
                        )
                    joined.writeframes(wav.readframes(wav.getnframes()))
            except (wave.Error, EOFError) as exc:
                raise VoiceNarrationError(
                    "The local voice engine returned an invalid WAV audio segment."
                ) from exc
    return output.getvalue()


def synthesize_speech(text: str, voice: str = SYSTEM_DEFAULT_VOICE) -> bytes:
    """Synthesize all text in bounded chunks and return one complete PCM WAV."""
    narration, _ = prepare_narration(text)
    macos = shutil.which("say")
    espeak = shutil.which("espeak-ng") or shutil.which("espeak")
    if not macos and not espeak:
        raise VoiceNarrationError(
            "No local voice engine was found. On macOS, enable the built-in 'say' command; "
            "on Linux, install espeak-ng."
        )
    engine = _synthesize_with_macos_say if macos else (
        lambda segment, selected_voice: _synthesize_with_espeak(
            str(espeak), segment, selected_voice
        )
    )
    chunks = _narration_chunks(narration)
    segments: list[bytes] = []
    for index, chunk in enumerate(chunks, start=1):
        try:
            segments.extend(_synthesize_complete_segment(chunk, engine, voice))
        except VoiceNarrationError as exc:
            raise VoiceNarrationError(
                f"Narration part {index} of {len(chunks)} failed: {exc}"
            ) from exc
    completed = _join_wav_segments(segments)
    _validate_segment_completion(narration, completed)
    return completed


def _synthesize_with_macos_say(text: str, voice: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="class-knowledge-voice-") as directory:
        root = Path(directory)
        text_path = root / "narration.txt"
        output_path = root / "narration.wav"
        # ``say`` reserves double brackets for embedded commands (for example,
        # ``[[slnc 200]]``). Lecture code such as nested Python lists must be
        # spoken as content and must never control the speech engine.
        safe_text = text.replace("[[", " open bracket open bracket ").replace(
            "]]", " close bracket close bracket "
        )
        text_path.write_text(safe_text, encoding="utf-8")
        command = ["say", "-r", "175"]
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
        command = [executable, "-s", "175", "-w", str(output_path)]
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
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            if wav.getcomptype() != "NONE" or wav.getsampwidth() != 2:
                raise VoiceNarrationError("The voice engine returned unsupported audio; select another voice.")
            frames = wav.readframes(wav.getnframes())
            expected = wav.getnframes() * wav.getnchannels() * wav.getsampwidth()
            if not expected or len(frames) != expected:
                raise VoiceNarrationError(
                    "The voice engine produced empty or incomplete audio. Try another installed voice. "
                    "If this persists, run the app from Terminal with access to macOS speech services."
                )
            if not any(frames):
                raise VoiceNarrationError("The voice engine produced silent audio. Try another installed voice.")
            # Rewrite a standard PCM WAV, removing platform-specific chunks.
            output = io.BytesIO()
            with wave.open(output, "wb") as clean:
                clean.setparams(wav.getparams())
                clean.writeframes(frames)
            return output.getvalue()
    except (wave.Error, EOFError) as exc:
        raise VoiceNarrationError("The local voice engine returned an invalid WAV audio file.") from exc
