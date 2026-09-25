"""Grounded local cleanup for noisy automatic lecture transcripts."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, ContextManager

from .config import PipelineConfig
from .task_control import TaskControlSignal
from .utils import clean_text, dump_json


CleanupProgressCallback = Callable[[int, int, str], None]
CLEANUP_VERSION = 3


class TranscriptCleanupError(RuntimeError):
    """Raised when the local model cannot safely clean transcript text."""


class TranscriptCleaner:
    """Repair readability while preserving timestamps, meaning, and raw evidence."""

    SYSTEM_PROMPT = """You edit and filter an automatic lecture transcript made from imperfect audio.
Your only source is the supplied transcript text. Improve punctuation, capitalization,
sentence boundaries, repeated fragments, and obvious speech-recognition mistakes when
the surrounding words make the correction strongly supported. Preserve technical terms,
numbers, formulas, qualifications, and the lecturer's meaning. Never add explanations,
facts, examples, or transitions that were not spoken. Never silently guess uncertain
content: write [unclear] for words that cannot be responsibly recovered. Mark personal
conversations, background chatter, greetings, breaks, scheduling, attendance, file
availability, platform instructions, grading administration, and all other non-subject
logistics for removal. Retain only speech that teaches, explains, questions, demonstrates,
or qualifies the lecture subject. When a passage does not add subject knowledge, remove it.
Return every input paragraph ID exactly once, even when it is marked for removal. Keep the
result in English and never translate it. Return valid JSON only."""

    def __init__(
        self,
        config: PipelineConfig,
        chat_guard: Callable[[], ContextManager[Any]] | None = None,
    ) -> None:
        self.config = config
        self._chat_guard = chat_guard
        try:
            import ollama
        except ImportError as exc:
            raise TranscriptCleanupError(
                "ollama is required for local transcript cleanup. Run: pip install -r requirements.txt"
            ) from exc
        self._client = ollama.Client(host=config.ollama_host)

    def clean(
        self,
        raw_transcript: dict[str, Any],
        output_path: str | Path,
        progress: CleanupProgressCallback | None = None,
        lecture_context: str = "",
    ) -> dict[str, Any]:
        """Clean timestamped paragraphs in resumable batches and retain raw segments."""
        output_path = Path(output_path)
        raw_paragraphs = raw_transcript.get("paragraphs", [])
        if not isinstance(raw_paragraphs, list):
            raise TranscriptCleanupError("The raw transcript has an invalid paragraphs field.")
        paragraphs = [dict(item) for item in raw_paragraphs if isinstance(item, dict)]
        source_digest = self._cleanup_source_digest(paragraphs, lecture_context)
        existing = self._load_completed_output(output_path, source_digest)
        if existing is not None:
            return existing
        if not paragraphs:
            payload = self._build_payload(
                raw_transcript, [], {}, output_path, 0, 0, source_digest=source_digest, is_partial=False
            )
            dump_json(output_path, payload)
            return payload

        self._validate_paragraphs(paragraphs)
        context_safe_batch_chars = max(2_000, int(self.config.ollama_num_ctx * 1.2))
        batch_chars = min(self.config.transcript_cleanup_batch_chars, context_safe_batch_chars)
        batches = self._make_batches(paragraphs, batch_chars)
        checkpoint_dir = output_path.parent / "transcript_cleanup"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        partial_path = output_path.with_name("transcript.cleanup.partial.json")
        cleaned_by_id: dict[int, dict[str, Any]] = {}
        report = progress or (lambda _index, _total, _message: None)

        for index, batch in enumerate(batches, start=1):
            checkpoint_path = checkpoint_dir / f"batch_{index:04d}.json"
            batch_digest = self._cleanup_source_digest(batch, lecture_context)
            cleaned = self._load_checkpoint(checkpoint_path, batch, batch_digest)
            reused = cleaned is not None
            if cleaned is None:
                cleaned = self._clean_batch(batch, index, len(batches), lecture_context)
                dump_json(
                    checkpoint_path,
                    {
                        "cleanup_version": CLEANUP_VERSION,
                        "batch": index,
                        "source_digest": batch_digest,
                        "paragraph_ids": [int(item["id"]) for item in batch],
                        "paragraphs": cleaned,
                    },
                )
            for item in cleaned:
                cleaned_by_id[int(item["id"])] = dict(item)
            partial_paragraphs = self._merge_cleaned_paragraphs(paragraphs, cleaned_by_id, completed_only=True)
            partial_payload = self._build_payload(
                raw_transcript,
                partial_paragraphs,
                cleaned_by_id,
                output_path,
                index,
                len(batches),
                source_digest=source_digest,
                is_partial=index < len(batches),
            )
            dump_json(partial_path, partial_payload)
            action = "Reused" if reused else "Cleaned"
            report(index, len(batches), f"{action} transcript cleanup batch {index}/{len(batches)}")

        cleaned_paragraphs = self._merge_cleaned_paragraphs(paragraphs, cleaned_by_id)
        payload = self._build_payload(
            raw_transcript,
            cleaned_paragraphs,
            cleaned_by_id,
            output_path,
            len(batches),
            len(batches),
            source_digest=source_digest,
            is_partial=False,
        )
        dump_json(output_path, payload)
        return payload

    def _clean_batch(
        self,
        batch: list[dict[str, Any]],
        batch_index: int,
        total_batches: int,
        lecture_context: str,
    ) -> list[dict[str, Any]]:
        source = {
            "paragraphs": [
                {
                    "id": int(item["id"]),
                    "start_time": item.get("start_time", ""),
                    "end_time": item.get("end_time", ""),
                    "text": str(item["text"]),
                }
                for item in batch
            ]
        }
        prompt = f"""Clean transcript batch {batch_index} of {total_batches}.
Return exactly this JSON shape and no other text:
{{"paragraphs": [{{"id": 1, "text": "cleaned text", "keep": true, "reason": "lecture content"}}]}}

Rules:
- Return the exact input IDs in the same order; do not merge, split, omit, or add IDs.
- Set keep=false for personal conversation, background discussion, greetings/farewells,
  breaks, scheduling, attendance, slide/file availability, grading administration,
  platform instructions, and any other material that does not teach the lecture subject.
- For keep=false, use an empty text value and give a short reason. When relevance is
  uncertain, use the lecture context and remove the passage if it adds no subject knowledge.
- Preserve subject-matter examples, questions, definitions, explanations, formulas,
  arguments, and professor clarifications.
- Do not keep a passage merely because it concerns the course; it must add knowledge
  about the academic subject itself.
- Make fragmented speech read naturally, but preserve the original claims and level of certainty.
- Remove accidental word repetitions and verbal filler only when meaning is unchanged.
- Correct a recognized word only when context strongly establishes the intended word.
- Use [unclear] instead of guessing damaged speech.
- Do not summarize or shorten substantive lecture material.
- Write in English only; do not translate the transcript.

LECTURE SUBJECT CONTEXT:
{lecture_context or '(No slide context was available; keep uncertain material.)'}

INPUT JSON:
{json.dumps(source, ensure_ascii=False)}"""
        last_error = ""
        for attempt in range(2):
            response_text = self._chat(prompt if attempt == 0 else prompt + "\nYour previous JSON was invalid. Try again.")
            try:
                return self._parse_cleaned_response(response_text, batch)
            except TranscriptCleanupError as exc:
                last_error = str(exc)
        raise TranscriptCleanupError(
            f"Transcript cleanup batch {batch_index} returned unsafe or invalid content twice: {last_error}"
        )

    def _chat(self, prompt: str) -> str:
        try:
            guard = self._chat_guard() if self._chat_guard else nullcontext()
            with guard:
                response = self._client.chat(
                    model=self.config.llm_model,
                    messages=[
                        {"role": "system", "content": self.SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    format="json",
                    think=False,
                    options={"temperature": 0.0, "num_ctx": self.config.ollama_num_ctx},
                    keep_alive=self.config.ollama_keep_alive,
                )
            message = response.get("message") if isinstance(response, dict) else getattr(response, "message", None)
            content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
            if not content or not str(content).strip():
                raise TranscriptCleanupError("The cleanup model returned an empty response.")
            return str(content).strip()
        except (TranscriptCleanupError, TaskControlSignal):
            raise
        except Exception as exc:
            raise TranscriptCleanupError(
                f"Could not clean the transcript with local model '{self.config.llm_model}': {exc}"
            ) from exc

    @staticmethod
    def _parse_cleaned_response(response_text: str, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        start = response_text.find("{")
        end = response_text.rfind("}")
        if start < 0 or end < start:
            raise TranscriptCleanupError("The cleanup response did not contain a JSON object.")
        try:
            payload = json.loads(response_text[start : end + 1])
            cleaned = payload["paragraphs"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise TranscriptCleanupError("The cleanup response JSON has an invalid structure.") from exc
        if not isinstance(cleaned, list):
            raise TranscriptCleanupError("The cleanup response paragraphs field is not a list.")
        expected_ids = [int(item["id"]) for item in batch]
        try:
            actual_ids = [int(item["id"]) for item in cleaned]
        except (KeyError, TypeError, ValueError) as exc:
            raise TranscriptCleanupError("The cleanup response contains invalid paragraph IDs.") from exc
        if actual_ids != expected_ids:
            raise TranscriptCleanupError("The cleanup response changed, omitted, or reordered paragraph IDs.")

        normalized: list[dict[str, Any]] = []
        source_by_id = {int(item["id"]): item for item in batch}
        kept_source_text: list[str] = []
        for item in cleaned:
            keep_value = item.get("keep", True)
            if not isinstance(keep_value, bool):
                raise TranscriptCleanupError("The cleanup response contains a non-boolean keep decision.")
            keep = keep_value
            text = clean_text(str(item.get("text", "")))
            reason = clean_text(str(item.get("reason", "lecture content" if keep else "unrelated speech")))
            if keep and not text:
                raise TranscriptCleanupError("The cleanup response erased a kept transcript paragraph.")
            if not keep:
                text = ""
                if not reason:
                    raise TranscriptCleanupError("A removed transcript paragraph needs a reason.")
            else:
                kept_source_text.append(str(source_by_id[int(item["id"])]["text"]))
            normalized.append({"id": int(item["id"]), "text": text, "keep": keep, "reason": reason})
        raw_length = sum(len(text) for text in kept_source_text)
        cleaned_length = sum(len(item["text"]) for item in normalized if item["keep"])
        if raw_length and not 0.45 <= cleaned_length / raw_length <= 1.65:
            raise TranscriptCleanupError("The cleanup response changed the transcript length too aggressively.")
        raw_words = Counter(re.findall(r"[\w'-]+", " ".join(text.lower() for text in kept_source_text)))
        cleaned_words = Counter(
            re.findall(r"[\w'-]+", " ".join(item["text"].lower() for item in normalized if item["keep"]))
        )
        retained_words = sum((raw_words & cleaned_words).values())
        if raw_words and retained_words / sum(raw_words.values()) < 0.55:
            raise TranscriptCleanupError("The cleanup response replaced too much source wording.")
        return normalized

    @staticmethod
    def _validate_paragraphs(paragraphs: list[dict[str, Any]]) -> None:
        ids: list[int] = []
        for paragraph in paragraphs:
            try:
                paragraph_id = int(paragraph["id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise TranscriptCleanupError("Every raw transcript paragraph needs a numeric ID.") from exc
            if not clean_text(str(paragraph.get("text", ""))):
                raise TranscriptCleanupError(f"Raw transcript paragraph {paragraph_id} is empty.")
            ids.append(paragraph_id)
        if len(ids) != len(set(ids)):
            raise TranscriptCleanupError("Raw transcript paragraph IDs must be unique.")

    @staticmethod
    def _make_batches(paragraphs: list[dict[str, Any]], maximum_chars: int) -> list[list[dict[str, Any]]]:
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_length = 0
        for paragraph in paragraphs:
            paragraph_length = len(str(paragraph["text"])) + 120
            if current and current_length + paragraph_length > maximum_chars:
                batches.append(current)
                current = []
                current_length = 0
            current.append(paragraph)
            current_length += paragraph_length
        if current:
            batches.append(current)
        return batches

    @staticmethod
    def _merge_cleaned_paragraphs(
        paragraphs: list[dict[str, Any]],
        cleaned_by_id: dict[int, dict[str, Any]],
        completed_only: bool = False,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for paragraph in paragraphs:
            paragraph_id = int(paragraph["id"])
            if paragraph_id not in cleaned_by_id:
                if completed_only:
                    continue
                raise TranscriptCleanupError(f"Cleaned transcript is missing paragraph {paragraph_id}.")
            decision = cleaned_by_id[paragraph_id]
            if not bool(decision.get("keep", True)):
                continue
            cleaned = dict(paragraph)
            cleaned["text"] = str(decision["text"])
            output.append(cleaned)
        return output

    def _build_payload(
        self,
        raw_transcript: dict[str, Any],
        paragraphs: list[dict[str, Any]],
        decisions_by_id: dict[int, dict[str, Any]],
        output_path: Path,
        completed_batches: int,
        total_batches: int,
        *,
        source_digest: str,
        is_partial: bool,
    ) -> dict[str, Any]:
        metadata = dict(raw_transcript.get("metadata", {}))
        metadata.update(
            {
                "transcript_kind": "cleaned",
                "output_language": "English",
                "cleanup_version": CLEANUP_VERSION,
                "cleanup_model": self.config.llm_model,
                "cleanup_source_digest": source_digest,
                "cleanup_created_at": datetime.now(timezone.utc).isoformat(),
                "cleanup_completed_batches": completed_batches,
                "cleanup_total_batches": total_batches,
                "cleanup_batch_char_limit": min(
                    self.config.transcript_cleanup_batch_chars,
                    max(2_000, int(self.config.ollama_num_ctx * 1.2)),
                ),
                "is_partial": is_partial,
                "raw_transcript_file": "transcript.raw.json",
                "raw_segment_count": len(raw_transcript.get("segments", [])),
                "cleaned_transcript_file": output_path.name,
                "kept_paragraph_count": sum(
                    1 for decision in decisions_by_id.values() if bool(decision.get("keep", True))
                ),
                "removed_paragraph_count": sum(
                    1 for decision in decisions_by_id.values() if not bool(decision.get("keep", True))
                ),
            }
        )
        return {
            "metadata": metadata,
            # Raw segment wording remains in transcript.raw.json. Keeping the
            # cleaned artifact paragraph-only prevents noisy duplicate text from
            # entering the subject library and RAG index.
            "segments": [],
            "paragraphs": paragraphs,
            "removed_paragraphs": [
                {"id": paragraph_id, "reason": str(decision.get("reason", "unrelated speech"))}
                for paragraph_id, decision in sorted(decisions_by_id.items())
                if not bool(decision.get("keep", True))
            ],
        }

    @staticmethod
    def _load_checkpoint(
        checkpoint_path: Path,
        batch: list[dict[str, Any]],
        source_digest: str,
    ) -> list[dict[str, Any]] | None:
        if not checkpoint_path.is_file():
            return None
        try:
            payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if int(payload.get("cleanup_version", 0)) != CLEANUP_VERSION:
                return None
            if payload.get("source_digest") != source_digest:
                return None
            cleaned = payload["paragraphs"]
            expected_ids = [int(item["id"]) for item in batch]
            if [int(item["id"]) for item in cleaned] != expected_ids:
                return None
            normalized: list[dict[str, Any]] = []
            for item in cleaned:
                keep_value = item.get("keep", True)
                if not isinstance(keep_value, bool):
                    return None
                keep = keep_value
                text = clean_text(str(item.get("text", "")))
                if keep and not text:
                    return None
                normalized.append(
                    {
                        "id": int(item["id"]),
                        "text": text if keep else "",
                        "keep": keep,
                        "reason": clean_text(str(item.get("reason", "lecture content" if keep else "unrelated speech"))),
                    }
                )
            return normalized
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _load_completed_output(output_path: Path, source_digest: str) -> dict[str, Any] | None:
        if not output_path.is_file():
            return None
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            metadata = payload.get("metadata", {})
            if (
                int(metadata.get("cleanup_version", 0)) == CLEANUP_VERSION
                and metadata.get("transcript_kind") == "cleaned"
                and metadata.get("cleanup_source_digest") == source_digest
                and not bool(metadata.get("is_partial", False))
                and isinstance(payload.get("paragraphs"), list)
            ):
                return payload
        except (TypeError, ValueError, OSError, json.JSONDecodeError, AttributeError):
            pass
        return None

    @staticmethod
    def _paragraph_digest(paragraphs: list[dict[str, Any]]) -> str:
        """Fingerprint source wording and timing before reusing cleanup work."""
        source = [
            {
                "id": item.get("id"),
                "start": item.get("start"),
                "end": item.get("end"),
                "text": item.get("text"),
            }
            for item in paragraphs
        ]
        encoded = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _cleanup_source_digest(cls, paragraphs: list[dict[str, Any]], lecture_context: str) -> str:
        source = f"{cls._paragraph_digest(paragraphs)}\n{clean_text(lecture_context)}"
        return hashlib.sha256(source.encode("utf-8")).hexdigest()
