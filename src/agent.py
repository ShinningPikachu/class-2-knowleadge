"""Ollama-powered agent that turns aligned lecture material into study notes."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from hashlib import sha256
import json
from pathlib import Path
from time import monotonic
from typing import Any, ContextManager

from .config import PipelineConfig
from .rag import LocalRAG
from .task_control import TaskControlSignal
from .utils import clean_text, dump_json


class AgentError(RuntimeError):
    """Raised when the local Ollama generation model cannot complete a note."""


ProgressCallback = Callable[[int, int, str], None]


class LectureAgent:
    """Generate grounded, per-slide study notes with a local Ollama model."""

    CHECKPOINT_SCHEMA_VERSION = 1
    SLIDE_PROMPT_VERSION = "lecture-slide-notes-v3"
    SYNTHESIS_PROMPT_VERSION = "lecture-synthesis-v1"

    SYSTEM_PROMPT = """You are a university lecture assistant.
Your task is to transform raw lecture material into professional study notes.

You have access to lecture-slide source material and/or a professor transcript.
Explain every supplied section clearly, preserve technical accuracy, and remove greetings,
hesitation, repetition, logistics, and unrelated speech. Include definitions and
examples only when the supplied source supports them. Identify important exam
concepts only when they are stated, emphasized, defined, contrasted, repeated,
or otherwise supported by the sources.

Never invent information, examples, formulas, citations, or claims. Do not use
outside knowledge to fill gaps. When a detail comes only from spoken material,
place it in the Professor explanation section and begin the relevant sentence
with "Professor explanation:". If the audio provides no useful addition, say
"No additional professor explanation was aligned with this slide." Be concise
but explanatory. Treat OCR text as potentially imperfect and do not infer a
diagram's meaning unless the slide text or transcript supports it. Write the
canonical lecture notes in English only. Never translate them during generation;
translation is a separate operation requested by the user after completion."""

    def __init__(
        self,
        config: PipelineConfig,
        rag: LocalRAG,
        chat_guard: Callable[[], ContextManager[Any]] | None = None,
    ) -> None:
        self.config = config
        self.rag = rag
        self._chat_guard = chat_guard
        try:
            import ollama
        except ImportError as exc:
            raise AgentError("ollama is not installed. Run: pip install -r requirements.txt") from exc
        self._client = ollama.Client(host=config.ollama_host)

    def generate_lecture_notes(
        self,
        slides: list[dict[str, Any]],
        alignment: dict[str, Any],
        lecture_title: str | None = None,
        progress: ProgressCallback | None = None,
        checkpoint_dir: str | Path | None = None,
        partial_output_path: str | Path | None = None,
        slide_summaries_path: str | Path | None = None,
    ) -> str:
        """Generate a complete Markdown document with durable model-call checkpoints."""
        if not slides:
            raise AgentError("Cannot generate notes because no usable lecture sections were available.")
        title = clean_text(lecture_title or slides[0].get("title", "") or "Lecture Notes")
        section_label = "Recording section" if slides[0].get("section_kind") == "recording" else "Slide"
        aligned_by_slide = {int(item["slide"]): item for item in alignment.get("slides", [])}
        checkpoint_root = Path(checkpoint_dir) if checkpoint_dir is not None else None
        partial_path = Path(partial_output_path) if partial_output_path is not None else None
        summaries_path = Path(slide_summaries_path) if slide_summaries_path is not None else None
        if checkpoint_root is not None:
            checkpoint_root.mkdir(parents=True, exist_ok=True)

        total = len(slides)
        prepared: list[tuple[dict[str, Any], dict[str, Any], int, str, str, str | None]] = []
        reused = 0
        for slide in slides:
            number = int(slide["slide"])
            slide_title = str(slide.get("title", f"Slide {number}"))
            aligned = aligned_by_slide.get(number, {"paragraphs": []})
            signature = self._slide_checkpoint_signature(slide, aligned, section_label)
            cached = self._load_slide_checkpoint(
                checkpoint_root / f"slide_{number:04d}.json" if checkpoint_root else None,
                signature,
                number,
            )
            if cached is not None:
                reused += 1
            prepared.append((slide, aligned, number, slide_title, signature, cached))

        if reused and progress:
            progress(reused, total, f"Reusing {reused} completed {section_label.lower()} note checkpoints")

        slide_sections: list[tuple[int, str, str]] = []
        for index, (slide, aligned, number, slide_title, signature, cached) in enumerate(prepared, start=1):
            started_at = monotonic()
            if cached is None and progress:
                progress(index, total, f"Writing study notes for {section_label.lower()} {number} of {total}")
            response = cached if cached is not None else self._generate_slide(slide, aligned, section_label)
            slide_sections.append((number, slide_title, response))
            self._write_slide_summaries(summaries_path, title, section_label, slide_sections, total)
            if cached is None:
                self._save_checkpoint(
                    checkpoint_root / f"slide_{number:04d}.json" if checkpoint_root else None,
                    "slide",
                    signature,
                    response,
                    {"slide": number, "title": slide_title},
                )
                self._write_partial_notes(partial_path, title, section_label, slide_sections, total)
                if progress:
                    elapsed = max(0, round(monotonic() - started_at))
                    duration = f"{elapsed}s" if elapsed < 60 else f"{elapsed // 60}m {elapsed % 60}s"
                    progress(
                        index,
                        total,
                        f"Completed {section_label.lower()} {number} and saved its checkpoint in {duration}",
                    )
        self._write_partial_notes(partial_path, title, section_label, slide_sections, total)

        synthesis_source = [
            {"slide": number, "title": slide_title, "notes": response}
            for number, slide_title, response in slide_sections
        ]
        digest_signature = self._checkpoint_signature(
            "digest",
            {"prompt_version": self.SYNTHESIS_PROMPT_VERSION, "sections": synthesis_source},
        )
        digest = self._load_checkpoint(
            checkpoint_root / "digest.json" if checkpoint_root else None,
            "digest",
            digest_signature,
        )
        if digest is None:
            if progress:
                progress(total, total, "Building a complete hierarchical lecture digest")
            digest = self._hierarchical_digest(slide_sections)
            self._save_checkpoint(
                checkpoint_root / "digest.json" if checkpoint_root else None,
                "digest",
                digest_signature,
                digest,
            )

        overview_signature = self._checkpoint_signature(
            "overview",
            {"prompt_version": self.SYNTHESIS_PROMPT_VERSION, "title": title, "digest": digest},
        )
        overview = self._load_checkpoint(
            checkpoint_root / "overview.json" if checkpoint_root else None,
            "overview",
            overview_signature,
        )
        if overview is None:
            if progress:
                progress(total, total, "Writing the overall lecture summary")
            overview = self._generate_overview(title, digest)
            self._save_checkpoint(
                checkpoint_root / "overview.json" if checkpoint_root else None,
                "overview",
                overview_signature,
                overview,
            )

        sections = [f"# {title}", "", "## Overall Summary", "", overview]
        for number, slide_title, response in slide_sections:
            sections.extend(["", f"# {section_label} {number}: {slide_title}", "", response])
        final_signature = self._checkpoint_signature(
            "final_sections",
            {"prompt_version": self.SYNTHESIS_PROMPT_VERSION, "title": title, "digest": digest},
        )
        final_sections = self._load_checkpoint(
            checkpoint_root / "final_sections.json" if checkpoint_root else None,
            "final_sections",
            final_signature,
        )
        if final_sections is not None:
            try:
                self._require_headings(
                    final_sections,
                    [
                        "# Complete Lecture Summary",
                        "# Key Definitions",
                        "# Important Formulas",
                        "# Possible Exam Questions",
                    ],
                    "final lecture summary checkpoint",
                )
            except AgentError:
                final_sections = None
        if final_sections is None:
            if progress:
                progress(total, total, "Writing the final revision sections")
            final_sections = self._generate_final_sections(title, digest)
            self._save_checkpoint(
                checkpoint_root / "final_sections.json" if checkpoint_root else None,
                "final_sections",
                final_signature,
                final_sections,
            )
        sections.extend(["", final_sections.strip(), ""])
        return "\n".join(sections)

    def generate_slide_note(
        self,
        slide: dict[str, Any],
        aligned: dict[str, Any],
        checkpoint_path: str | Path | None = None,
    ) -> str:
        """Generate or reuse one independently checkpointed slide-note body."""
        number = int(slide["slide"])
        section_label = "Recording section" if slide.get("section_kind") == "recording" else "Slide"
        path = Path(checkpoint_path) if checkpoint_path is not None else None
        signature = self._slide_checkpoint_signature(slide, aligned, section_label)
        cached = self._load_slide_checkpoint(path, signature, number)
        if cached is not None:
            return cached
        response = self._generate_slide(slide, aligned, section_label)
        self._save_checkpoint(
            path,
            "slide",
            signature,
            response,
            {"slide": number, "title": str(slide.get("title", f"Slide {number}"))},
        )
        return response

    def _slide_checkpoint_signature(
        self,
        slide: dict[str, Any],
        aligned: dict[str, Any],
        section_label: str,
    ) -> str:
        number = int(slide["slide"])
        return self._checkpoint_signature(
            "slide",
            {
                "prompt_version": self.SLIDE_PROMPT_VERSION,
                "section_label": section_label,
                "slide": {
                    "slide": number,
                    "title": slide.get("title", ""),
                    "content": slide.get("content", ""),
                    "notes": slide.get("notes", ""),
                    "visual_text": slide.get("visual_text", []),
                    "section_kind": slide.get("section_kind", ""),
                },
                "aligned_paragraphs": aligned.get("paragraphs", []),
            },
        )

    @classmethod
    def _load_slide_checkpoint(
        cls,
        path: Path | None,
        signature: str,
        number: int,
    ) -> str | None:
        cached = cls._load_checkpoint(path, "slide", signature)
        if cached is None:
            return None
        cached = cls._ensure_concise_summary(cached)
        try:
            cls._require_headings(
                cached,
                [
                    "## Concise summary",
                    "## Slide content",
                    "## Professor explanation",
                    "## Important concepts",
                    "## Exam points",
                ],
                f"slide {number} checkpoint",
            )
        except AgentError:
            return None
        return cached

    def _checkpoint_signature(self, kind: str, payload: dict[str, Any]) -> str:
        """Bind a checkpoint to its exact evidence, prompts, and model settings."""
        signature_payload = {
            "schema_version": self.CHECKPOINT_SCHEMA_VERSION,
            "kind": kind,
            "model": self.config.llm_model,
            "temperature": self.config.llm_temperature,
            "num_ctx": self.config.ollama_num_ctx,
            "thinking": self.config.ollama_thinking,
            "quality_review": self.config.quality_review,
            "note_generation_profile": self.config.note_generation_profile,
            "note_max_output_tokens": self.config.note_max_output_tokens,
            "max_slide_context_chars": self.config.max_slide_context_chars,
            "embedding_model": self.config.embedding_model,
            "chunk_size": self.config.chunk_size,
            "chunk_overlap": self.config.chunk_overlap,
            "system_prompt": self.SYSTEM_PROMPT,
            "payload": payload,
        }
        serialized = json.dumps(
            signature_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(serialized.encode("utf-8")).hexdigest()

    @classmethod
    def _load_checkpoint(cls, path: Path | None, kind: str, signature: str) -> str | None:
        if path is None or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != cls.CHECKPOINT_SCHEMA_VERSION:
            return None
        if payload.get("kind") != kind or payload.get("signature") != signature:
            return None
        content = payload.get("content")
        return content.strip() if isinstance(content, str) and content.strip() else None

    @classmethod
    def _save_checkpoint(
        cls,
        path: Path | None,
        kind: str,
        signature: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if path is None:
            return
        dump_json(
            path,
            {
                "schema_version": cls.CHECKPOINT_SCHEMA_VERSION,
                "kind": kind,
                "signature": signature,
                "content": content,
                **(metadata or {}),
            },
        )

    @staticmethod
    def _write_partial_notes(
        path: Path | None,
        title: str,
        section_label: str,
        slide_sections: list[tuple[int, str, str]],
        total: int,
    ) -> None:
        if path is None:
            return
        sections = [
            f"# {title}",
            "",
            f"> Partial notes checkpoint: {len(slide_sections)} of {total} {section_label.lower()}s completed.",
        ]
        for number, slide_title, response in slide_sections:
            sections.extend(["", f"# {section_label} {number}: {slide_title}", "", response])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(f".{path.name}.tmp")
        temporary_path.write_text("\n".join([*sections, ""]), encoding="utf-8")
        temporary_path.replace(path)

    def _generate_overview(self, title: str, digest: str) -> str:
        prompt = f"""Create a short, source-grounded overall summary for the lecture titled
"{title}". Use only the verified lecture digest. Return 1–2 coherent paragraphs
without a heading, lists, or preamble.

VERIFIED LECTURE DIGEST:
{digest}"""
        return self._chat(prompt)

    def _generate_slide(self, slide: dict[str, Any], aligned: dict[str, Any], section_label: str) -> str:
        slide_number = int(slide["slide"])
        aligned_paragraphs = aligned.get("paragraphs", [])
        transcript_lines = [
            f"[{item.get('start_time', 'unknown time')}–"
            f"{item.get('end_time', 'unknown time')}] {item.get('text', '')}"
            for item in aligned_paragraphs
        ]
        transcript = self._bounded_transcript_evidence(slide_number, transcript_lines)
        query = " ".join(
            part
            for part in [
                str(slide.get("title", "")),
                str(slide.get("content", "")),
                str(slide.get("notes", "")),
            ]
            if part
        )
        search_query = query or f"slide {slide_number}"
        # Strict slide filters prevent semantically similar material from another
        # part of the lecture being presented as evidence for this slide.
        retrieved = self.rag.search(search_query, n_results=4, where={"slide_number": slide_number})
        retrieved += self.rag.search(search_query, n_results=6, where={"aligned_slide": slide_number})
        retrieved_text = "\n".join(
            f"[{item['metadata'].get('source', 'source')} | {self._source_label(item['metadata'])}] {item['text']}"
            for item in retrieved
        )
        source_slide = self._slide_source(slide)
        context = f"""{section_label.upper()} {slide_number} SOURCE (authoritative for this section):
{source_slide}

ALIGNED PROFESSOR TRANSCRIPT (the timestamps were mapped to this section):
{transcript}

LOCAL RETRIEVAL CONTEXT (restricted to this slide and its aligned transcript):
{retrieved_text or '(No additional retrieval context.)'}"""
        if len(context) > self.config.max_slide_context_chars:
            # Retrieval is redundant evidence; preserve the authoritative slide
            # and the complete hierarchical transcript evidence first.
            available = max(0, self.config.max_slide_context_chars - len(context) + len(retrieved_text))
            retrieved_text = retrieved_text[:available]
            context = f"""SLIDE {slide_number} SOURCE (authoritative for this slide):
{source_slide}

ALIGNED PROFESSOR TRANSCRIPT (the timestamps were mapped to this slide):
{transcript}

LOCAL RETRIEVAL CONTEXT (restricted to this slide and its aligned transcript):
{retrieved_text or '(Omitted because authoritative evidence filled the context.)'}"""
        if len(context) > self.config.max_slide_context_chars:
            raise AgentError(
                f"Authoritative source context for slide {slide_number} exceeds the safe model window; "
                "increase Ollama context or reduce the media chunk size."
            )
        prompt = f"""Write the study-notes body for {section_label} {slide_number}: {slide.get('title', '')}.
Use only the sources below. Return exactly these Markdown sections, in this order:

## Concise summary
In 1–3 short sentences (maximum 90 words), state only what is needed to understand
this specific slide or small recording fragment. Combine the essential slide content
with directly relevant professor explanation. Remove repetition, general background,
speculation, unrelated context, and details belonging to other slides.

## Slide content
Clear explanation of material actually shown on this slide.

## Professor explanation
Only the meaningful additions from the aligned transcript. Prefix each spoken-only
claim with "Professor explanation:". Do not repeat the slide verbatim.

## Important concepts
- Grounded concept or "- No additional concepts stated."

## Exam points
- Grounded important detail or "- No exam-specific emphasis stated in the material."

SOURCES:
{context}"""
        draft = self._chat(prompt)
        should_review = self.config.note_generation_profile == "deep" and self.config.quality_review
        result = self._review_slide(slide_number, context, draft) if should_review else draft
        result = self._ensure_concise_summary(result)
        self._require_headings(
            result,
            [
                "## Concise summary",
                "## Slide content",
                "## Professor explanation",
                "## Important concepts",
                "## Exam points",
            ],
            f"slide {slide_number}",
        )
        return result

    def _bounded_transcript_evidence(self, slide_number: int, lines: list[str]) -> str:
        """Read all long transcript material through map-reduce instead of truncating it."""
        if not lines:
            return "(No transcript paragraph was aligned with this slide.)"
        blocks = lines
        round_number = 1
        while len("\n".join(blocks)) > 12_000:
            batches = self._pack_blocks(blocks, 9_000)
            reduced: list[str] = []
            for batch_index, batch in enumerate(batches, start=1):
                prompt = f"""Extract an exhaustive evidence digest from transcript batch
{batch_index} of {len(batches)} for Slide {slide_number} (round {round_number}).
Retain timestamps and every substantive definition, explanation, example, formula,
contrast, warning, and exam emphasis. Remove only filler and repetition. Do not use
outside knowledge and do not reinterpret uncertain speech. Return at most 3,500 characters.

TRANSCRIPT BATCH:
{batch}"""
                reduced.append(self._chat(prompt)[:4_000])
            if len("\n".join(reduced)) >= len("\n".join(blocks)):
                raise AgentError(f"Transcript evidence for slide {slide_number} could not be reduced safely.")
            blocks = reduced
            round_number += 1
            if round_number > 4:
                raise AgentError(f"Transcript evidence for slide {slide_number} remains too large after four passes.")
        return "\n".join(blocks)

    def _review_slide(self, slide_number: int, context: str, draft: str) -> str:
        prompt = f"""Act as a strict factual reviewer for Slide {slide_number}.
Compare the draft with the supplied sources line by line. Remove or correct every
claim, definition, example, formula, exam hint, or causal link that is not directly
supported. Preserve useful professor additions, prefixed with "Professor explanation:".
Explain source-supported concepts, relationships, context, qualifications, and important
details more deeply than the baseline draft. Keep the concise summary slide-specific,
non-repetitive, and no longer than 90 words.
Do not improve the draft using outside knowledge. If evidence is absent, state that
plainly. Return only the corrected Markdown with exactly these headings:

## Concise summary
## Slide content
## Professor explanation
## Important concepts
## Exam points

SOURCES:
{context}

DRAFT TO AUDIT:
{draft}"""
        return self._chat(prompt)

    @classmethod
    def concise_summary(cls, markdown: str) -> str:
        """Extract the model's concise per-slide summary from a note body."""
        marker = "## Concise summary"
        start = markdown.find(marker)
        if start < 0:
            return clean_text(markdown.split("##", 1)[0])
        remainder = markdown[start + len(marker) :].lstrip("\n ")
        end = remainder.find("\n## ")
        return remainder[:end].strip() if end >= 0 else remainder.strip()

    @classmethod
    def _ensure_concise_summary(cls, markdown: str) -> str:
        """Upgrade older/fallback slide notes without losing their grounded content."""
        if "## Concise summary" in markdown:
            return markdown

        def section(heading: str) -> str:
            start = markdown.find(heading)
            if start < 0:
                return ""
            remainder = markdown[start + len(heading) :].lstrip("\n ")
            end = remainder.find("\n## ")
            return remainder[:end].strip() if end >= 0 else remainder.strip()

        slide_text = clean_text(section("## Slide content"))
        professor_text = clean_text(section("## Professor explanation"))
        useful_professor = "" if professor_text.lower().startswith("no additional professor") else professor_text
        summary = " ".join(item for item in (slide_text, useful_professor) if item)
        if not summary:
            summary = "No concise source-grounded summary is available for this slide."
        return f"## Concise summary\n{summary}\n\n{markdown.lstrip()}"

    @classmethod
    def _write_slide_summaries(
        cls,
        path: Path | None,
        lecture_title: str,
        section_label: str,
        slide_sections: list[tuple[int, str, str]],
        total: int,
    ) -> None:
        if path is None:
            return
        dump_json(
            path,
            {
                "metadata": {
                    "lecture_title": lecture_title,
                    "section_label": section_label,
                    "completed": len(slide_sections),
                    "total": total,
                    "is_partial": len(slide_sections) < total,
                },
                "slides": [
                    {
                        "slide": number,
                        "title": title,
                        "summary": cls.concise_summary(notes),
                    }
                    for number, title, notes in slide_sections
                ],
            },
        )

    def _generate_final_sections(self, title: str, digest: str) -> str:
        prompt = f"""Create the final revision sections for "{title}" from only the source digest below.
Never add facts not found in the digest. Return exactly this Markdown structure:

# Complete Lecture Summary
A concise connected revision narrative.

# Key Definitions
- Term: source-grounded definition
(If none are explicitly defined, write "- No explicit definitions were provided.")

# Important Formulas
- Formula — meaning/conditions, only if stated
(If none, write "- No formulas were provided in the lecture material.")

# Possible Exam Questions
- A question that can be answered directly from the source material
(If no assessed emphasis is available, write "- Review the key concepts and their relationships described above.")

SOURCE DIGEST:
{digest}"""
        result = self._chat(prompt)
        self._require_headings(
            result,
            ["# Complete Lecture Summary", "# Key Definitions", "# Important Formulas", "# Possible Exam Questions"],
            "final lecture summary",
        )
        return result

    def _hierarchical_digest(self, slide_sections: list[tuple[int, str, str]]) -> str:
        """Compress every slide in bounded batches; no late slides are discarded."""
        blocks = [
            f"[Slide {number}: {title}]\n{notes}"
            for number, title, notes in slide_sections
        ]
        round_number = 1
        while sum(len(block) for block in blocks) > 24_000:
            batches = self._pack_blocks(blocks, 18_000)
            next_level: list[str] = []
            for batch_index, batch in enumerate(batches, start=1):
                prompt = f"""Create a loss-minimizing factual digest for lecture batch
{batch_index} of {len(batches)} (compression round {round_number}). Retain slide
numbers, professor-only additions, definitions, formulas, contrasts, examples,
and explicit exam emphasis. Remove repetition only. Do not add outside facts.
Return at most 3,500 characters.

BATCH MATERIAL:
{batch}"""
                next_level.append(self._chat(prompt)[:4_000])
            if sum(len(block) for block in next_level) >= sum(len(block) for block in blocks):
                raise AgentError("Hierarchical summarization did not reduce the lecture safely.")
            blocks = next_level
            round_number += 1
            if round_number > 4:
                raise AgentError("Lecture digest remained too large after four quality-preserving passes.")
        return "\n\n".join(blocks)

    @staticmethod
    def _pack_blocks(blocks: list[str], character_limit: int) -> list[str]:
        batches: list[str] = []
        current: list[str] = []
        current_size = 0
        for block in blocks:
            if current and current_size + len(block) > character_limit:
                batches.append("\n\n".join(current))
                current = []
                current_size = 0
            if len(block) > character_limit:
                # Slide notes are already bounded; this is a fail-closed safeguard.
                raise AgentError("A single slide note exceeds the safe hierarchical context limit.")
            current.append(block)
            current_size += len(block)
        if current:
            batches.append("\n\n".join(current))
        return batches

    @staticmethod
    def _require_headings(markdown: str, headings: list[str], label: str) -> None:
        missing = [heading for heading in headings if heading not in markdown]
        if missing:
            raise AgentError(f"The quality review for {label} omitted required sections: {', '.join(missing)}")

    @staticmethod
    def _slide_source(slide: dict[str, Any]) -> str:
        parts = [
            f"Title: {slide.get('title', '')}",
            f"Slide text: {slide.get('content', '') or '(No extractable text.)'}",
        ]
        if slide.get("notes"):
            parts.append(f"Speaker notes: {slide['notes']}")
        if slide.get("visual_text"):
            parts.append("Locally OCR-detected visual text: " + " | ".join(slide["visual_text"]))
        return "\n".join(parts)

    @staticmethod
    def _source_label(metadata: dict[str, Any]) -> str:
        if metadata.get("source") == "slide":
            return f"slide {metadata.get('slide_number', '?')}"
        aligned = metadata.get("aligned_slide")
        mapping = f", aligned slide {aligned}" if aligned is not None else ""
        return f"transcript at {metadata.get('start', '?')}s{mapping}"

    def _chat(self, user_prompt: str) -> str:
        try:
            guard = self._chat_guard() if self._chat_guard else nullcontext()
            with guard:
                result = self._client.chat(
                    model=self.config.llm_model,
                    messages=[
                        {"role": "system", "content": self.SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    options={
                        "temperature": self.config.llm_temperature,
                        "num_ctx": self.config.ollama_num_ctx,
                        "num_predict": self.config.note_max_output_tokens,
                    },
                    think=(
                        False
                        if self.config.note_generation_profile == "fast"
                        else self.config.ollama_thinking
                    ),
                    keep_alive=self.config.ollama_keep_alive,
                )
            message = result.get("message") if isinstance(result, dict) else getattr(result, "message", None)
            content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
            if not content or not str(content).strip():
                raise AgentError("Ollama returned an empty response.")
            return str(content).strip()
        except (AgentError, TaskControlSignal):
            raise
        except Exception as exc:
            raise AgentError(
                f"Could not generate notes with local Ollama model '{self.config.llm_model}' at "
                f"{self.config.ollama_host}. Start Ollama and run `ollama pull {self.config.llm_model}`. "
                f"Details: {exc}"
            ) from exc
