"""Offline neural text-to-speech narration for library content."""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
import io
from pathlib import Path
import re
import subprocess
import sys
import wave


class VoiceNarrationError(RuntimeError):
    """Raised when local neural text-to-speech cannot create playable narration."""


DEFAULT_PIPER_VOICE = "en_US-lessac-medium"
NARRATION_CHUNK_CHARS = 320
MINIMUM_RECOVERY_CHARS = 48
MAXIMUM_RECOVERY_DEPTH = 4


def _voice_models_root() -> Path:
    """Keep downloaded neural voices outside the tracked source tree."""
    return Path(__file__).resolve().parents[1] / "models" / "piper"


def _installed_voice_models() -> dict[str, Path]:
    root = _voice_models_root()
    if not root.is_dir():
        return {}
    models: dict[str, Path] = {}
    for candidate in root.rglob("*.onnx"):
        config_path = candidate.with_suffix(candidate.suffix + ".json")
        if config_path.is_file():
            name = candidate.relative_to(root).with_suffix("").as_posix()
            models[name] = candidate
    return dict(sorted(models.items(), key=lambda item: item[0].casefold()))


def local_voice_options() -> tuple[str, ...]:
    """Return current Piper voice-model names, including models added manually."""
    return tuple(_installed_voice_models())


def neural_voice_setup_message() -> str | None:
    """Explain the one-time local setup required before narration can run."""
    try:
        import piper  # noqa: F401
    except ImportError:
        return "The Piper neural voice runtime is not installed. Update the app dependencies, then restart it."
    if not local_voice_options():
        root = _voice_models_root()
        return (
            "No Piper voice model is installed yet. Install the bundled English voice below, "
            f"or place a Piper .onnx model and its matching .onnx.json file in {root}."
        )
    return None


def install_default_voice() -> Path:
    """Download the default open-source Piper voice after an explicit UI request."""
    try:
        import piper  # noqa: F401
    except ImportError as exc:
        raise VoiceNarrationError(
            "The Piper neural voice runtime is not installed. Update dependencies and restart the app."
        ) from exc

    root = _voice_models_root()
    root.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "piper.download_voices",
        DEFAULT_PIPER_VOICE,
        "--download-dir",
        str(root),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as exc:
        raise VoiceNarrationError("Downloading the neural voice model took too long. Try again later.") from exc
    except OSError as exc:
        raise VoiceNarrationError(f"Could not start the Piper voice download: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f" Details: {detail}" if detail else ""
        raise VoiceNarrationError(f"Could not download the Piper voice model.{suffix}") from exc

    model_path = root / f"{DEFAULT_PIPER_VOICE}.onnx"
    config_path = model_path.with_suffix(model_path.suffix + ".json")
    if not model_path.is_file() or not config_path.is_file():
        raise VoiceNarrationError("Piper finished without saving a complete voice model. Try the download again.")
    _load_piper_voice.cache_clear()
    return model_path


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
                raise VoiceNarrationError("The voice model returned audio with an invalid sample rate.")
            return wav.getnframes() / wav.getframerate()
    except (wave.Error, EOFError) as exc:
        raise VoiceNarrationError("The voice model returned an invalid WAV audio file.") from exc


def _minimum_duration_seconds(text: str) -> float:
    """Estimate a conservative floor that still detects a two-word truncation."""
    cjk_characters = len(re.findall(r"[\u3400-\u9fff\uf900-\ufaff]", text))
    without_cjk = re.sub(r"[\u3400-\u9fff\uf900-\ufaff]", " ", text)
    words = len(re.findall(r"\b[\w']+\b", without_cjk, flags=re.UNICODE))
    # Allow substantial room for abbreviations, punctuation, URLs, and fast voices.
    expected = words * 60 / 175 + cjk_characters / 4.5
    return max(0.18, expected * 0.42)


def _validate_segment_completion(text: str, audio: bytes) -> None:
    duration = wav_duration_seconds(audio)
    minimum = _minimum_duration_seconds(text)
    if duration < minimum:
        raise VoiceNarrationError(
            f"The voice model stopped early ({duration:.1f}s generated; at least {minimum:.1f}s expected)."
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
    """Synthesize one segment, recursively shrinking it if a model truncates."""
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

    detail = str(last_error) if last_error else "unknown neural voice-model failure"
    raise VoiceNarrationError(f"The narration segment could not be completed: {detail}")


def _join_wav_segments(segments: list[bytes]) -> bytes:
    """Combine decoded PCM frames so browsers receive one valid WAV container."""
    if not segments:
        raise VoiceNarrationError("The voice model did not return any narration segments.")
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
                            "The voice model changed audio format between narration segments."
                        )
                    joined.writeframes(wav.readframes(wav.getnframes()))
            except (wave.Error, EOFError) as exc:
                raise VoiceNarrationError("The voice model returned an invalid WAV audio segment.") from exc
    return output.getvalue()


def _piper_voice_path(voice: str) -> Path:
    model_path = _installed_voice_models().get(voice)
    if model_path is None:
        raise VoiceNarrationError(
            f"The Piper voice '{voice}' is not installed. Choose an available neural voice model."
        )
    return model_path


@lru_cache(maxsize=4)
def _load_piper_voice(model_path: str) -> object:
    try:
        from piper import PiperVoice
    except ImportError as exc:
        raise VoiceNarrationError(
            "The Piper neural voice runtime is not installed. Update dependencies and restart the app."
        ) from exc
    try:
        return PiperVoice.load(model_path)
    except Exception as exc:
        raise VoiceNarrationError(f"Could not load the Piper voice model: {exc}") from exc


def _piper_safe_text(text: str) -> str:
    """Keep document text from opting into Piper's raw-phoneme syntax."""
    return text.replace("[[", "open bracket open bracket").replace(
        "]]", "close bracket close bracket"
    )


def _validate_wav_bytes(audio: bytes) -> bytes:
    if len(audio) < 44 or not audio.startswith(b"RIFF") or audio[8:12] != b"WAVE":
        raise VoiceNarrationError("The voice model returned an invalid WAV audio file.")
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            if wav.getcomptype() != "NONE" or wav.getsampwidth() != 2:
                raise VoiceNarrationError("The voice model returned unsupported audio.")
            frames = wav.readframes(wav.getnframes())
            expected = wav.getnframes() * wav.getnchannels() * wav.getsampwidth()
            if not expected or len(frames) != expected:
                raise VoiceNarrationError("The voice model produced empty or incomplete audio.")
            if not any(frames):
                raise VoiceNarrationError("The voice model produced silent audio.")
            # Rewrite a standard PCM WAV, removing model/runtime-specific chunks.
            output = io.BytesIO()
            with wave.open(output, "wb") as clean:
                clean.setparams(wav.getparams())
                clean.writeframes(frames)
            return output.getvalue()
    except (wave.Error, EOFError) as exc:
        raise VoiceNarrationError("The voice model returned an invalid WAV audio file.") from exc


def _read_wav(path: Path) -> bytes:
    """Read and validate a WAV file; retained for filesystem-level validation tests."""
    try:
        return _validate_wav_bytes(path.read_bytes())
    except OSError as exc:
        raise VoiceNarrationError(f"The voice model did not produce an audio file: {exc}") from exc


def _synthesize_with_piper(text: str, voice: str) -> bytes:
    model_path = _piper_voice_path(voice)
    model = _load_piper_voice(str(model_path))
    sample_rate = int(getattr(getattr(model, "config", None), "sample_rate", 0))
    if sample_rate <= 0:
        raise VoiceNarrationError("The Piper voice model has an invalid sample rate.")
    output = io.BytesIO()
    try:
        with wave.open(output, "wb") as wav:
            # Piper otherwise sets the WAV header lazily. Initializing it here
            # converts a punctuation-only model response into a clear empty-audio
            # error instead of a low-level wave-module exception.
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            getattr(model, "synthesize_wav")(_piper_safe_text(text), wav, set_wav_format=False)
    except VoiceNarrationError:
        raise
    except Exception as exc:
        raise VoiceNarrationError(f"The Piper voice model could not create narration: {exc}") from exc
    return _validate_wav_bytes(output.getvalue())


def synthesize_speech(text: str, voice: str = DEFAULT_PIPER_VOICE) -> bytes:
    """Synthesize all text locally with Piper and return one complete PCM WAV."""
    narration, _ = prepare_narration(text)
    # Resolve the voice before consuming time on chunk synthesis, so a missing
    # model is reported once with an actionable message.
    _piper_voice_path(voice)
    chunks = _narration_chunks(narration)
    segments: list[bytes] = []
    for index, chunk in enumerate(chunks, start=1):
        try:
            segments.extend(_synthesize_complete_segment(chunk, _synthesize_with_piper, voice))
        except VoiceNarrationError as exc:
            raise VoiceNarrationError(
                f"Narration part {index} of {len(chunks)} failed: {exc}"
            ) from exc
    completed = _join_wav_segments(segments)
    _validate_segment_completion(narration, completed)
    return completed
