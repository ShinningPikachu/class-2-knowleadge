"""Quality-first offline transcription for long audio and lecture videos."""

from __future__ import annotations

import gc
import json
import os
import platform
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import PipelineConfig
from .task_control import TaskControlSignal
from .utils import clean_text, dump_json, seconds_to_timestamp


SUPPORTED_AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
SUPPORTED_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
SUPPORTED_RECORDING_SUFFIXES = SUPPORTED_AUDIO_SUFFIXES | SUPPORTED_VIDEO_SUFFIXES
TranscriptionProgressCallback = Callable[[dict[str, Any]], None]


class AudioProcessingError(RuntimeError):
    """Raised when a recording cannot be transcribed locally."""


@dataclass(frozen=True)
class MediaChunkPlan:
    """A core time range plus overlap used only as transcription context."""

    index: int
    core_start: float
    core_end: float
    extract_start: float
    extract_end: float

    @property
    def extract_duration(self) -> float:
        return self.extract_end - self.extract_start


class AudioProcessor:
    """Transcribe long recordings with bounded, quality-preserving parallelism."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    def transcribe(
        self,
        audio_path: str | Path,
        output_path: str | Path,
        progress: TranscriptionProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Transcribe audio/video into absolute timestamps and checkpoint each chunk.

        Long recordings are split into overlapping core ranges. Speech in the
        overlap gives Whisper boundary context, but midpoint filtering assigns
        every segment to exactly one core range so text is not duplicated.
        """
        audio_path = Path(audio_path)
        output_path = Path(output_path)
        if not audio_path.is_file():
            raise AudioProcessingError(f"Recording file not found: {audio_path}")
        if audio_path.suffix.lower() not in SUPPORTED_RECORDING_SUFFIXES:
            allowed = ", ".join(sorted(SUPPORTED_RECORDING_SUFFIXES))
            raise AudioProcessingError(f"Unsupported recording type '{audio_path.suffix}'. Use: {allowed}.")

        try:
            from faster_whisper import WhisperModel
            from faster_whisper.audio import decode_audio
        except ImportError as exc:
            raise AudioProcessingError(
                "faster-whisper is not installed. Run: pip install -r requirements.txt"
            ) from exc

        try:
            decoded_audio = decode_audio(str(audio_path), sampling_rate=16_000)
        except Exception as exc:
            raise AudioProcessingError(f"Could not decode the recording's audio track locally: {exc}") from exc
        decoded_audio = self._validate_decoded_audio(decoded_audio)
        duration = len(decoded_audio) / 16_000
        chunk_plan = self._build_chunk_plan(
            duration,
            float(self.config.media_chunk_seconds),
            float(self.config.media_overlap_seconds),
        )
        checkpoint_dir = output_path.parent / "transcript_chunks"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        partial_output_path = output_path.with_name("transcript.partial.json")
        if output_path.is_file():
            try:
                completed_transcript = json.loads(output_path.read_text(encoding="utf-8"))
                if isinstance(completed_transcript.get("segments"), list) and isinstance(
                    completed_transcript.get("paragraphs"), list
                ):
                    return completed_transcript
            except (OSError, json.JSONDecodeError, AttributeError):
                # Do not trust a partially written transcript; rebuild it from
                # independently validated chunk checkpoints instead.
                pass
        checkpoint_records = self._load_checkpoint_records(checkpoint_dir, chunk_plan)
        completed_indices = {record["chunk"] for record in checkpoint_records}
        plans_to_process = [plan for plan in chunk_plan if plan.index not in completed_indices]
        report = progress or (lambda _event: None)

        def persist_chunk(record: dict[str, Any]) -> None:
            """Persist one chunk and atomically rebuild the readable partial transcript."""
            dump_json(checkpoint_dir / f"chunk_{record['chunk']:04d}.json", record)
            self._write_partial_transcript(
                audio_path=audio_path,
                output_path=partial_output_path,
                checkpoint_dir=checkpoint_dir,
                chunk_plan=chunk_plan,
                duration=duration,
                language=detected_language,
            )

        def report_chunk(record: dict[str, Any], state: str) -> None:
            """Persist a usable checkpoint and provide a UI-safe main-thread update."""
            persist_chunk(record)
            preview = " ".join(segment.get("text", "") for segment in record["segments"][:2])[:280]
            report(
                {
                    "event": state,
                    "completed_chunks": len(completed_indices) + len(completed),
                    "total_chunks": len(chunk_plan),
                    "chunk": record["chunk"],
                    "start_time": seconds_to_timestamp(float(record["core_start"])),
                    "end_time": seconds_to_timestamp(float(record["core_end"])),
                    "preview": preview,
                }
            )

        segments: list[dict[str, Any]] = []
        detected_language = self.config.language
        language_probability = 0.0
        model: Any | None = None

        try:
            completed: list[tuple[dict[str, Any], str | None, float]] = []
            report(
                {
                    "event": "started",
                    "completed_chunks": len(checkpoint_records),
                    "total_chunks": len(chunk_plan),
                    "active_chunks": [plan.index for plan in plans_to_process[: self.config.whisper_parallel_workers]],
                }
            )
            for checkpoint_record in checkpoint_records:
                report_chunk(checkpoint_record, "reused")
            if plans_to_process:
                model = WhisperModel(
                    self.config.whisper_model,
                    device=self.config.whisper_device,
                    compute_type=self.config.whisper_compute_type,
                    # CTranslate2 maintains this many model workers for concurrent
                    # calls. Split CPU threads so two workers do not oversubscribe
                    # the machine. The model, beam size, and precision are unchanged.
                    cpu_threads=max(1, (os.cpu_count() or 2) // self.config.whisper_parallel_workers),
                    num_workers=self.config.whisper_parallel_workers,
                    local_files_only=True,
                )
            # Auto-detection must happen once before independent workers run;
            # otherwise different chunks could choose different languages. When
            # the user provides (for example) "en", all chunks start at once.
            initial_plans = []
            if not detected_language and plans_to_process and plans_to_process[0].index == 1:
                initial_plans = [plans_to_process[0]]
            for plan in initial_plans:
                assert model is not None
                result = self._transcribe_plan(model, plan, decoded_audio, duration, len(chunk_plan), detected_language)
                completed.append(result)
                if result[1] and result[2] >= 0.65:
                    detected_language, language_probability = result[1], result[2]
                report_chunk(result[0], "completed")

            initial_indices = {plan.index for plan in initial_plans}
            remaining_plans = [plan for plan in plans_to_process if plan.index not in initial_indices]
            if self.config.whisper_parallel_workers == 1 or len(remaining_plans) < 2:
                for plan in remaining_plans:
                    assert model is not None
                    completed.append(
                        self._transcribe_plan(model, plan, decoded_audio, duration, len(chunk_plan), detected_language)
                    )
                    report_chunk(completed[-1][0], "completed")
            else:
                executor = ThreadPoolExecutor(max_workers=self.config.whisper_parallel_workers)
                futures = [
                    executor.submit(
                        self._transcribe_plan, model, plan, decoded_audio, duration, len(chunk_plan), detected_language
                    )
                    for plan in remaining_plans
                ]
                try:
                    for future in as_completed(futures):
                        result = future.result()
                        completed.append(result)
                        report_chunk(result[0], "completed")
                except Exception as exc:
                    # A pause/cancel signal is raised only after report_chunk has
                    # durably saved the finished chunk. Do not start queued chunks;
                    # allow only currently running Whisper calls to finish safely.
                    for future in futures:
                        future.cancel()
                    executor.shutdown(wait=True, cancel_futures=True)
                    if isinstance(exc, TaskControlSignal):
                        recorded_chunks = {item[0]["chunk"] for item in completed}
                        for future in futures:
                            if future.cancelled() or not future.done():
                                continue
                            try:
                                finished = future.result()
                            except Exception:
                                continue
                            if finished[0]["chunk"] in recorded_chunks:
                                continue
                            completed.append(finished)
                            recorded_chunks.add(finished[0]["chunk"])
                            persist_chunk(finished[0])
                    raise
                else:
                    executor.shutdown(wait=True)

            chunk_records = checkpoint_records + [item[0] for item in completed]
            chunk_records.sort(key=lambda record: record["chunk"])
            for chunk_record in chunk_records:
                for record in chunk_record["segments"]:
                    record["id"] = len(segments) + 1
                    segments.append(record)
                chunk_record["segment_count"] = len(chunk_record["segments"])
                dump_json(checkpoint_dir / f"chunk_{chunk_record['chunk']:04d}.json", chunk_record)
        except (AudioProcessingError, TaskControlSignal):
            raise
        except Exception as exc:  # library errors include invalid codec/model files
            raise AudioProcessingError(
                "Local transcription failed. Verify the recording and that "
                f"Whisper model '{self.config.whisper_model}' is cached locally or is a valid "
                f"local directory. Completed chunk checkpoints remain in {checkpoint_dir}. "
                f"Details: {exc}"
            ) from exc
        finally:
            if model is not None:
                del model
            del decoded_audio
            # Release the large-v3 CTranslate2 object before Ollama loads the 27B LLM.
            gc.collect()

        if not segments:
            raise AudioProcessingError(
                "No speech was detected in the recording. Check the audio track and language setting."
            )

        payload: dict[str, Any] = {
            "metadata": {
                "transcript_kind": "raw",
                "source_file": audio_path.name,
                "source_type": "video" if audio_path.suffix.lower() in SUPPORTED_VIDEO_SUFFIXES else "audio",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "model": self.config.whisper_model,
                "language": detected_language,
                "language_probability": round(language_probability, 4),
                "duration_seconds": round(duration, 3),
                "chunk_count": len(chunk_plan),
                "chunk_seconds": self.config.media_chunk_seconds,
                "overlap_seconds": self.config.media_overlap_seconds,
                "decoded_audio_megabytes": round(duration * 16_000 * 4 / 1_048_576, 1),
                "parallel_workers": self.config.whisper_parallel_workers,
                "resumed_chunks": len(checkpoint_records),
                "chunks": [{key: value for key, value in record.items() if key != "segments"} for record in chunk_records],
            },
            "segments": segments,
            "paragraphs": self._make_paragraphs(segments),
        }
        dump_json(output_path, payload)
        return payload

    def _write_partial_transcript(
        self,
        audio_path: Path,
        output_path: Path,
        checkpoint_dir: Path,
        chunk_plan: list[MediaChunkPlan],
        duration: float,
        language: str | None,
    ) -> None:
        """Assemble completed chunks into a readable transcript after every checkpoint."""
        chunk_records = self._load_checkpoint_records(checkpoint_dir, chunk_plan)
        chunk_records.sort(key=lambda record: int(record["chunk"]))
        segments: list[dict[str, Any]] = []
        for chunk_record in chunk_records:
            for raw_segment in chunk_record["segments"]:
                segment = dict(raw_segment)
                segment["id"] = len(segments) + 1
                segments.append(segment)
        payload = {
            "metadata": {
                "transcript_kind": "raw",
                "source_file": audio_path.name,
                "source_type": "video"
                if audio_path.suffix.lower() in SUPPORTED_VIDEO_SUFFIXES
                else "audio",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "model": self.config.whisper_model,
                "language": language,
                "duration_seconds": round(duration, 3),
                "is_partial": len(chunk_records) < len(chunk_plan),
                "completed_chunks": len(chunk_records),
                "total_chunks": len(chunk_plan),
                "chunk_seconds": self.config.media_chunk_seconds,
                "overlap_seconds": self.config.media_overlap_seconds,
                "chunks": [
                    {key: value for key, value in record.items() if key != "segments"}
                    for record in chunk_records
                ],
            },
            "segments": segments,
            "paragraphs": self._make_paragraphs(segments),
        }
        dump_json(output_path, payload)

    @staticmethod
    def _load_checkpoint_records(
        checkpoint_dir: Path, chunk_plan: list[MediaChunkPlan]
    ) -> list[dict[str, Any]]:
        """Reuse only complete, matching checkpoints from an interrupted run."""
        plans_by_index = {plan.index: plan for plan in chunk_plan}
        records: list[dict[str, Any]] = []
        for path in sorted(checkpoint_dir.glob("chunk_*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                index = int(record["chunk"])
                plan = plans_by_index[index]
                if (
                    not isinstance(record.get("segments"), list)
                    or float(record.get("core_start")) != plan.core_start
                    or float(record.get("core_end")) != plan.core_end
                ):
                    continue
                records.append(record)
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
                continue
        return records

    @staticmethod
    def _validate_decoded_audio(decoded_audio: Any) -> Any:
        """Reject invalid decoder output before it reaches Whisper workers."""
        try:
            import numpy as np
        except ImportError:
            return decoded_audio

        # faster-whisper's decoder contract is a one-dimensional NumPy array.
        # Lightweight stand-ins used by callers and tests can retain their own
        # sequence implementation without forcing a multi-gigabyte allocation.
        if not isinstance(decoded_audio, np.ndarray):
            return decoded_audio
        if decoded_audio.ndim != 1 or decoded_audio.size == 0:
            raise AudioProcessingError("The recording decoded to an empty or invalid audio track.")
        if not bool(np.isfinite(decoded_audio).all()):
            raise AudioProcessingError(
                "The recording decoded to non-finite audio samples. Convert it to PCM WAV and try again."
            )
        peak = float(np.max(np.abs(decoded_audio)))
        if peak > 1.5:
            raise AudioProcessingError(
                "The recording decoded outside the expected audio range. Convert it to PCM WAV and try again."
            )
        return np.ascontiguousarray(decoded_audio, dtype=np.float32)

    @staticmethod
    def _configure_whisper_warning_filter() -> None:
        """Hide a known false-positive NumPy/Accelerate warning on Apple silicon."""
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            return
        warnings.filterwarnings(
            "ignore",
            message=r"(?:divide by zero|overflow|invalid value) encountered in matmul",
            category=RuntimeWarning,
            module=r"faster_whisper\.feature_extractor",
        )

    @staticmethod
    def _transcribe_plan(
        model: Any,
        plan: MediaChunkPlan,
        decoded_audio: Any,
        duration: float,
        total_chunks: int,
        language: str | None,
    ) -> tuple[dict[str, Any], str | None, float]:
        """Transcribe one overlapping range; returned records are ordered later."""
        sample_start = int(plan.extract_start * 16_000)
        sample_end = min(len(decoded_audio), int(plan.extract_end * 16_000))
        media_input = decoded_audio[sample_start:sample_end]
        AudioProcessor._configure_whisper_warning_filter()
        segments_iter, info = model.transcribe(
            media_input,
            language=language or None,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=True,
            word_timestamps=False,
        )
        chunk_segments: list[dict[str, Any]] = []
        for segment in segments_iter:
            text = clean_text(segment.text)
            if not text:
                continue
            absolute_start = plan.extract_start + float(segment.start)
            absolute_end = min(duration, plan.extract_start + float(segment.end))
            midpoint = (absolute_start + absolute_end) / 2
            is_last_core = plan.index == total_chunks
            if midpoint < plan.core_start or (midpoint >= plan.core_end and not is_last_core):
                continue
            chunk_segments.append(
                {
                    "start": round(absolute_start, 3),
                    "end": round(absolute_end, 3),
                    "start_time": seconds_to_timestamp(absolute_start),
                    "end_time": seconds_to_timestamp(absolute_end),
                    "text": text,
                    "media_chunk": plan.index,
                }
            )
        chunk_record = {
            "chunk": plan.index,
            "core_start": plan.core_start,
            "core_end": plan.core_end,
            "extract_start": plan.extract_start,
            "extract_end": plan.extract_end,
            "segment_count": len(chunk_segments),
            "segments": chunk_segments,
        }
        return (
            chunk_record,
            getattr(info, "language", None),
            float(getattr(info, "language_probability", 0.0) or 0.0),
        )

    @staticmethod
    def _build_chunk_plan(duration: float, chunk_seconds: float, overlap_seconds: float) -> list[MediaChunkPlan]:
        """Cover the complete duration with non-overlapping cores and overlapping extracts."""
        if duration <= 0:
            raise AudioProcessingError("The recording duration must be greater than zero.")
        plans: list[MediaChunkPlan] = []
        core_start = 0.0
        index = 1
        while core_start < duration:
            core_end = min(duration, core_start + chunk_seconds)
            plans.append(
                MediaChunkPlan(
                    index=index,
                    core_start=core_start,
                    core_end=core_end,
                    extract_start=max(0.0, core_start - overlap_seconds),
                    extract_end=min(duration, core_end + overlap_seconds),
                )
            )
            core_start = core_end
            index += 1
        return plans

    @staticmethod
    def _make_paragraphs(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Join nearby speech segments into readable, timestamped paragraphs."""
        paragraphs: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for segment in segments:
            should_start = current is None
            if current is not None:
                long_pause = segment["start"] - current["end"] > 2.75
                too_long = len(current["text"]) + len(segment["text"]) > 900
                should_start = long_pause or too_long
            if should_start:
                if current:
                    paragraphs.append(current)
                current = {
                    "id": len(paragraphs) + 1,
                    "start": segment["start"],
                    "end": segment["end"],
                    "start_time": segment["start_time"],
                    "end_time": segment["end_time"],
                    "text": segment["text"],
                    "segment_ids": [segment["id"]],
                }
            else:
                assert current is not None
                current["end"] = segment["end"]
                current["end_time"] = segment["end_time"]
                current["text"] = f"{current['text']} {segment['text']}"
                current["segment_ids"].append(segment["id"])
        if current:
            paragraphs.append(current)
        return paragraphs
