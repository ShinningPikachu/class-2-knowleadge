"""On-demand, structure-preserving translation of completed lecture notes."""

from __future__ import annotations

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
from .utils import dump_json


TranslationProgressCallback = Callable[[int, int, str], None]
TRANSLATION_VERSION = 1


class TranslationError(RuntimeError):
    """Raised when an on-demand translation cannot be completed safely."""


class MarkdownTranslator:
    """Translate finished English Markdown without changing its information."""

    SYSTEM_PROMPT = """You are a precise document translator.
Translate the supplied English lecture notes into exactly the requested target
language. Preserve meaning, qualifications, technical terminology, formulas,
timestamps, URLs, citations, Markdown heading levels, bullet structure, and
paragraph order. Do not summarize, explain, correct, add, or remove information.
Return only the translated Markdown fragment with no preamble or code fence."""

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
            raise TranslationError(
                "ollama is required for local translation. Run: pip install -r requirements.txt"
            ) from exc
        self._client = ollama.Client(host=config.ollama_host)

    def translate(
        self,
        source_markdown: str,
        target_language: str,
        output_path: str | Path,
        progress: TranslationProgressCallback | None = None,
    ) -> str:
        """Translate in resumable batches only after an explicit user request."""
        target_language = self.validate_target_language(target_language)
        source_markdown = source_markdown.strip()
        if not source_markdown:
            raise TranslationError("The completed English notes are empty and cannot be translated.")

        output_path = Path(output_path)
        source_digest = self._digest(source_markdown)
        existing = self._load_completed_output(output_path, source_digest, target_language)
        if existing is not None:
            return existing

        character_limit = min(12_000, max(2_000, int(self.config.ollama_num_ctx * 1.2)))
        batches = self._make_batches(source_markdown, character_limit)
        checkpoint_dir = output_path.parent / "checkpoints" / output_path.stem
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        partial_path = output_path.with_suffix(".partial.md")
        translated_batches: list[str] = []
        report = progress or (lambda _index, _total, _message: None)

        for index, source_batch in enumerate(batches, start=1):
            checkpoint_path = checkpoint_dir / f"batch_{index:04d}.json"
            batch_digest = self._digest(source_batch)
            translated = self._load_checkpoint(
                checkpoint_path,
                batch_digest,
                target_language,
                source_batch,
            )
            reused = translated is not None
            if translated is None:
                translated = self._translate_batch(source_batch, target_language, index, len(batches))
                dump_json(
                    checkpoint_path,
                    {
                        "translation_version": TRANSLATION_VERSION,
                        "target_language": target_language,
                        "source_digest": batch_digest,
                        "translated_markdown": translated,
                    },
                )
            translated_batches.append(translated)
            self._write_text(partial_path, "\n\n".join(translated_batches).strip() + "\n")
            action = "Reused" if reused else "Translated"
            report(index, len(batches), f"{action} translation batch {index}/{len(batches)}")

        result = "\n\n".join(translated_batches).strip() + "\n"
        self._write_text(output_path, result)
        dump_json(
            self._metadata_path(output_path),
            {
                "translation_version": TRANSLATION_VERSION,
                "source_language": "English",
                "target_language": target_language,
                "source_digest": source_digest,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "batch_count": len(batches),
            },
        )
        return result

    def _translate_batch(
        self,
        source_batch: str,
        target_language: str,
        batch_index: int,
        total_batches: int,
    ) -> str:
        prompt = f"""Translate this English Markdown into {target_language}.
This is batch {batch_index} of {total_batches}. Translate every line exactly once.
Keep all Markdown markers and document structure. Do not answer or expand questions
that appear in the notes. Return only the translated Markdown.

ENGLISH MARKDOWN:
{source_batch}"""
        last_error = ""
        for attempt in range(2):
            response = self._chat(
                prompt if attempt == 0 else prompt + "\nThe previous response changed the structure. Try again."
            )
            try:
                return self._validate_translation(source_batch, response)
            except TranslationError as exc:
                last_error = str(exc)
        raise TranslationError(
            f"Translation batch {batch_index} changed the document structure twice: {last_error}"
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
                    think=False,
                    options={"temperature": 0.0, "num_ctx": self.config.ollama_num_ctx},
                    keep_alive=self.config.ollama_keep_alive,
                )
            message = response.get("message") if isinstance(response, dict) else getattr(response, "message", None)
            content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
            if not content or not str(content).strip():
                raise TranslationError("The translation model returned an empty response.")
            return str(content).strip()
        except (TranslationError, TaskControlSignal):
            raise
        except Exception as exc:
            raise TranslationError(
                f"Could not translate with local model '{self.config.llm_model}': {exc}"
            ) from exc

    @staticmethod
    def validate_target_language(value: str) -> str:
        target = re.sub(r"\s+", " ", value or "").strip()
        if not 2 <= len(target) <= 80 or not re.fullmatch(r"[\w ()-]+", target, flags=re.UNICODE):
            raise TranslationError("Enter a language name using letters, spaces, parentheses, or hyphens.")
        if target.casefold() in {"en", "eng", "english"}:
            raise TranslationError("The finished lecture notes are already in English.")
        return target

    @classmethod
    def _validate_translation(cls, source: str, translated: str) -> str:
        translated = translated.strip()
        if not translated:
            raise TranslationError("The translated batch is empty.")
        if cls._markdown_signature(source) != cls._markdown_signature(translated):
            raise TranslationError("Markdown headings, lists, or code fences were changed.")
        ratio = len(translated) / max(1, len(source))
        if not 0.15 <= ratio <= 3.0:
            raise TranslationError("The translated batch changed length too aggressively.")
        return translated

    @staticmethod
    def _markdown_signature(markdown: str) -> list[str]:
        signature: list[str] = []
        for line in markdown.splitlines():
            if match := re.match(r"^(#{1,6})\s+", line):
                signature.append(f"heading:{len(match.group(1))}")
            elif re.match(r"^\s*[-*+]\s+", line):
                signature.append("bullet")
            elif re.match(r"^\s*\d+[.)]\s+", line):
                signature.append("numbered")
            elif line.lstrip().startswith("```"):
                signature.append("fence")
            elif line.lstrip().startswith(">"):
                signature.append("quote")
        return signature

    @classmethod
    def _make_batches(cls, markdown: str, maximum_chars: int) -> list[str]:
        blocks: list[str] = []
        for block in re.split(r"\n\s*\n", markdown.strip()):
            block = block.strip()
            if not block:
                continue
            blocks.extend(cls._split_oversized_block(block, maximum_chars))

        batches: list[str] = []
        current: list[str] = []
        current_size = 0
        for block in blocks:
            separator_size = 2 if current else 0
            if current and current_size + separator_size + len(block) > maximum_chars:
                batches.append("\n\n".join(current))
                current = []
                current_size = 0
                separator_size = 0
            current.append(block)
            current_size += separator_size + len(block)
        if current:
            batches.append("\n\n".join(current))
        return batches

    @staticmethod
    def _split_oversized_block(block: str, maximum_chars: int) -> list[str]:
        if len(block) <= maximum_chars:
            return [block]
        pieces: list[str] = []
        remaining = block
        while len(remaining) > maximum_chars:
            boundary = remaining.rfind("\n", 0, maximum_chars)
            if boundary < maximum_chars // 2:
                boundary = remaining.rfind(" ", 0, maximum_chars)
            if boundary < maximum_chars // 2:
                boundary = maximum_chars
            pieces.append(remaining[:boundary].strip())
            remaining = remaining[boundary:].strip()
        if remaining:
            pieces.append(remaining)
        return pieces

    @classmethod
    def _load_checkpoint(
        cls,
        path: Path,
        source_digest: str,
        target_language: str,
        source_batch: str,
    ) -> str | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                int(payload.get("translation_version", 0)) != TRANSLATION_VERSION
                or payload.get("source_digest") != source_digest
                or payload.get("target_language") != target_language
            ):
                return None
            return cls._validate_translation(source_batch, str(payload["translated_markdown"]))
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError, TranslationError):
            return None

    @classmethod
    def _load_completed_output(
        cls,
        output_path: Path,
        source_digest: str,
        target_language: str,
    ) -> str | None:
        metadata_path = cls._metadata_path(output_path)
        if not output_path.is_file() or not metadata_path.is_file():
            return None
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if (
                int(metadata.get("translation_version", 0)) != TRANSLATION_VERSION
                or metadata.get("source_digest") != source_digest
                or metadata.get("target_language") != target_language
            ):
                return None
            translated = output_path.read_text(encoding="utf-8")
            return translated if translated.strip() else None
        except (TypeError, ValueError, OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _metadata_path(output_path: Path) -> Path:
        return output_path.with_suffix(output_path.suffix + ".metadata.json")

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _write_text(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(f".{path.name}.tmp")
        temporary_path.write_text(value, encoding="utf-8")
        temporary_path.replace(path)
