"""Ollama-powered agent that turns aligned lecture material into study notes."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from typing import Any, ContextManager

from .config import PipelineConfig
from .rag import LocalRAG
from .task_control import TaskControlSignal
from .utils import clean_text


class AgentError(RuntimeError):
    """Raised when the local Ollama generation model cannot complete a note."""


ProgressCallback = Callable[[int, int, str], None]


class LectureAgent:
    """Generate grounded, per-slide study notes with a local Ollama model."""

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
    ) -> str:
        """Generate the requested complete Markdown document, section by section."""
        if not slides:
            raise AgentError("Cannot generate notes because no usable lecture sections were available.")
        title = clean_text(lecture_title or slides[0].get("title", "") or "Lecture Notes")
        section_label = "Recording section" if slides[0].get("section_kind") == "recording" else "Slide"
        aligned_by_slide = {int(item["slide"]): item for item in alignment.get("slides", [])}

        total = len(slides)
        slide_sections: list[tuple[int, str, str]] = []
        for index, slide in enumerate(slides, start=1):
            number = int(slide["slide"])
            if progress:
                progress(index, total, f"Writing study notes for {section_label.lower()} {number} of {total}")
            aligned = aligned_by_slide.get(number, {"paragraphs": []})
            response = self._generate_slide(slide, aligned, section_label)
            slide_sections.append((number, str(slide.get("title", f"Slide {number}")), response))

        if progress:
            progress(total, total, "Building a complete hierarchical lecture digest")
        digest = self._hierarchical_digest(slide_sections)
        overview = self._generate_overview(title, digest)
        sections = [f"# {title}", "", "## Overall Summary", "", overview]
        for number, slide_title, response in slide_sections:
            sections.extend(["", f"# {section_label} {number}: {slide_title}", "", response])
        final_sections = self._generate_final_sections(title, digest)
        sections.extend(["", final_sections.strip(), ""])
        return "\n".join(sections)

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
        result = self._review_slide(slide_number, context, draft) if self.config.quality_review else draft
        self._require_headings(
            result,
            ["## Slide content", "## Professor explanation", "## Important concepts", "## Exam points"],
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
Do not improve the draft using outside knowledge. If evidence is absent, state that
plainly. Return only the corrected Markdown with exactly these headings:

## Slide content
## Professor explanation
## Important concepts
## Exam points

SOURCES:
{context}

DRAFT TO AUDIT:
{draft}"""
        return self._chat(prompt)

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
                    },
                    think=self.config.ollama_thinking,
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
