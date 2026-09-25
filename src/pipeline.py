"""End-to-end orchestration for one completely local lecture-processing run."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import shutil
from typing import Any, ContextManager
from uuid import uuid4

from .agent import LectureAgent
from .alignment import SlideAligner
from .audio_processor import AudioProcessor, SUPPORTED_RECORDING_SUFFIXES
from .config import PipelineConfig, project_path
from .embeddings import OllamaEmbedder, build_source_documents
from .exporter import export_markdown, export_pdf
from .lecture_naming import infer_lecture_identity
from .pdf_processor import PDFProcessor
from .quality import validate_evidence_quality, validate_final_notes
from .rag import LocalRAG
from .transcript_cleaner import TranscriptCleaner
from .utils import clean_text, dump_json, seconds_to_timestamp


StageCallback = Callable[[str, str], None]


@dataclass
class PipelineResult:
    """Locations and user-facing values produced by a successful run."""

    run_id: str
    run_dir: Path
    notes_markdown: str
    markdown_path: Path
    pdf_path: Path
    transcript_path: Path
    raw_transcript_path: Path
    transcript_text_path: Path
    slides_path: Path
    slide_summaries_path: Path
    alignment_path: Path
    quality_report_path: Path
    manifest_path: Path
    lecture_name: str
    lecture_title: str
    indexed_chunks: int


class LecturePipeline:
    """Compose all modules in the requested extraction → alignment → RAG flow."""

    def __init__(
        self,
        config: PipelineConfig,
        project_root: str | Path | None = None,
        qwen_guard: Callable[[], ContextManager[Any]] | None = None,
    ) -> None:
        config.validate()
        self.config = config
        self.project_root = Path(project_root) if project_root else project_path()
        self.qwen_guard = qwen_guard
        self.current_run_dir: Path | None = None

    def run(
        self,
        audio_path: str | Path | None,
        presentation_path: str | Path | None,
        lecture_title: str | None = None,
        on_stage: StageCallback | None = None,
    ) -> PipelineResult:
        """Run every processing stage and retain all intermediate artifacts.

        Keeping JSON and Chroma artifacts per run makes the result reproducible:
        a learner can inspect exactly what text and mapping informed the notes.
        """
        identity = infer_lecture_identity(lecture_title, audio_path, presentation_path)
        run_id = f"{identity.base_name}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}"
        run_dir = self.project_root / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        self.current_run_dir = run_dir
        if audio_path is None and presentation_path is None:
            raise ValueError("Provide at least one source: a recording or a PDF/PPT/PPTX deck.")
        # Preserve immutable input copies beside the output for later auditing.
        input_dir = run_dir / "input"
        input_dir.mkdir()
        stored_audio = (
            input_dir / f"{identity.base_name}_Recording{Path(audio_path).suffix.lower()}" if audio_path else None
        )
        stored_presentation = (
            input_dir / f"{identity.base_name}_Slides{Path(presentation_path).suffix.lower()}"
            if presentation_path
            else None
        )
        try:
            if audio_path and stored_audio:
                shutil.copy2(audio_path, stored_audio)
            if presentation_path and stored_presentation:
                shutil.copy2(presentation_path, stored_presentation)
        except OSError as exc:
            raise RuntimeError(f"Could not preserve lecture input files for this run: {exc}") from exc

        return self._process_run(
            run_id, run_dir, stored_audio, stored_presentation, identity.display_title, on_stage
        )

    def resume(
        self,
        run_dir: str | Path,
        lecture_title: str | None = None,
        on_stage: StageCallback | None = None,
    ) -> PipelineResult:
        """Continue an interrupted run without discarding its checkpoints."""
        run_dir = Path(run_dir).expanduser().resolve()
        self.current_run_dir = run_dir
        input_dir = run_dir / "input"
        if not input_dir.is_dir():
            raise RuntimeError(f"Cannot resume: input directory not found in {run_dir}")
        recordings = [path for path in input_dir.iterdir() if path.suffix.lower() in SUPPORTED_RECORDING_SUFFIXES]
        decks = [path for path in input_dir.iterdir() if path.suffix.lower() in {".pdf", ".ppt", ".pptx"}]
        if len(recordings) > 1 or len(decks) > 1 or not (recordings or decks):
            raise RuntimeError("Cannot resume: the run must contain one recording, one slide deck, or both.")
        return self._process_run(
            run_dir.name,
            run_dir,
            recordings[0] if recordings else None,
            decks[0] if decks else None,
            lecture_title,
            on_stage,
        )

    def _process_run(
        self,
        run_id: str,
        run_dir: Path,
        stored_audio: Path | None,
        stored_presentation: Path | None,
        lecture_title: str | None,
        on_stage: StageCallback | None,
    ) -> PipelineResult:
        notify = on_stage or (lambda _stage, _message: None)

        notify("preflight", "Checking the configured models on the local Ollama server")
        embedder = OllamaEmbedder(self.config)
        embedder.verify_local_models()

        transcript_path = run_dir / "transcript.json"
        raw_transcript_path = run_dir / "transcript.raw.json"

        def transcription_progress(event: dict[str, object]) -> None:
            completed = event.get("completed_chunks", 0)
            total = event.get("total_chunks", 0)
            if event.get("event") == "started":
                active = ", ".join(str(index) for index in event.get("active_chunks", []))
                notify(
                    "recording",
                    f"Transcription progress: {completed}/{total} chunks complete; active chunks: {active}",
                )
                return
            preview = str(event.get("preview", "")).strip()
            message = (
                f"Transcription progress: {completed}/{total} chunks complete — "
                f"chunk {event.get('chunk')} ({event.get('start_time')}–{event.get('end_time')})"
            )
            if event.get("event") == "reused":
                message += " reused from checkpoint"
            if preview:
                message += f". Confirmed transcript preview: {preview}"
            notify("recording", message)

        identity = infer_lecture_identity(lecture_title, stored_audio, stored_presentation)
        slides_path = run_dir / "slides.json"
        raw_transcript: dict[str, Any]
        if stored_audio:
            # Runs created before transcript cleanup used transcript.json for the
            # raw Whisper output. Preserve that evidence when resuming them.
            if transcript_path.is_file() and not raw_transcript_path.is_file():
                shutil.copy2(transcript_path, raw_transcript_path)
            notify(
                "recording",
                f"Transcribing locally with faster-whisper (up to {self.config.whisper_parallel_workers} CPU workers)",
            )
            raw_transcript = AudioProcessor(self.config).transcribe(
                stored_audio, raw_transcript_path, progress=transcription_progress
            )
        else:
            raw_transcript = {"metadata": {"source_type": "none"}, "segments": [], "paragraphs": []}
            dump_json(raw_transcript_path, raw_transcript)

        # Extract slides before cleanup so the cleaner can distinguish the
        # lecture subject from personal conversation and background chatter.
        if stored_presentation:
            notify("slides", "Extracting slide text, titles, notes, and local image OCR")
            slide_payload = PDFProcessor(self.config).process(stored_presentation, slides_path)
            slides = slide_payload["slides"]
        else:
            slides = []

        if stored_audio:
            if self.config.enable_transcript_cleanup:
                notify(
                    "cleanup",
                    "Cleaning lecture speech and removing clearly unrelated background conversation",
                )

                def cleanup_progress(index: int, total: int, message: str) -> None:
                    notify("cleanup", f"{message} ({index}/{total})")

                transcript = TranscriptCleaner(self.config, chat_guard=self.qwen_guard).clean(
                    raw_transcript,
                    transcript_path,
                    progress=cleanup_progress,
                    lecture_context=self._lecture_context(identity.display_title, slides),
                )
            else:
                transcript = {
                    "metadata": {
                        **dict(raw_transcript.get("metadata", {})),
                        "transcript_kind": "raw",
                        "cleanup_enabled": False,
                        "raw_transcript_file": raw_transcript_path.name,
                    },
                    "segments": raw_transcript.get("segments", []),
                    "paragraphs": raw_transcript.get("paragraphs", []),
                }
                dump_json(transcript_path, transcript)
        else:
            transcript = {"metadata": {"source_type": "none"}, "segments": [], "paragraphs": []}
            dump_json(transcript_path, transcript)

        if not stored_presentation:
            notify("alignment", "Creating timestamped recording sections from the cleaned professor transcript")
            slides = self._recording_sections(transcript)
            dump_json(
                slides_path,
                {"metadata": {"source_type": "recording", "section_count": len(slides)}, "slides": slides},
            )

        transcript_text_path = run_dir / "transcript.txt"
        transcript_text_path.write_text(self._transcript_text(transcript), encoding="utf-8")

        alignment_path = run_dir / "alignment.json"
        if stored_audio and stored_presentation:
            notify("alignment", "Aligning timestamped professor speech to slides using local embeddings")
            alignment = SlideAligner(self.config, embedder).align(slides, transcript, alignment_path)
        elif stored_audio:
            notify("alignment", "Assigning timestamped transcript paragraphs to recording sections")
            alignment = self._recording_alignment(slides, transcript, alignment_path)
        else:
            notify("alignment", "No recording supplied; retaining slide-only evidence")
            alignment = self._empty_alignment(slides, alignment_path)

        notify("quality", "Checking transcript coverage and alignment reliability")
        quality_report_path = run_dir / "quality_report.json"
        validate_evidence_quality(slides, transcript, alignment, quality_report_path)

        notify("rag", "Indexing slide and transcript chunks in the local Chroma database")
        documents = build_source_documents(slides, transcript, self.config, alignment)
        rag = LocalRAG(run_dir / "database", embedder)
        indexed_chunks = rag.index(documents)

        if self.config.note_generation_profile == "fast":
            note_mode = "fast baseline mode (one bounded, non-thinking call per slide)"
        else:
            note_mode = "deep reviewed mode (high reasoning plus a second factual audit per slide)"
        notify("notes", f"Generating grounded study notes in {note_mode}")
        agent = LectureAgent(self.config, rag, chat_guard=self.qwen_guard)

        def note_progress(index: int, total: int, message: str) -> None:
            notify("notes", f"{message} ({index}/{total})")

        effective_title = identity.display_title
        slide_summaries_path = run_dir / "slide_summaries.json"
        notes = agent.generate_lecture_notes(
            slides,
            alignment,
            effective_title,
            progress=note_progress,
            checkpoint_dir=run_dir / "notes_checkpoints",
            partial_output_path=run_dir / "lecture_notes.partial.md",
            slide_summaries_path=slide_summaries_path,
        )
        section_label = "Recording section" if slides and slides[0].get("section_kind") == "recording" else "Slide"
        validate_final_notes(notes, len(slides), section_label=section_label)
        markdown_path = export_markdown(notes, run_dir / "lecture_notes.md")

        notify("export", "Rendering an offline PDF copy of the lecture notes")
        pdf_path = export_pdf(notes, run_dir / "lecture_notes.pdf")
        manifest_path = run_dir / "lecture_manifest.json"
        dump_json(
            manifest_path,
            {
                "lecture_name": identity.base_name,
                "lecture_title": identity.display_title,
                "run_id": run_id,
                "files": {
                    "recording": str(stored_audio) if stored_audio else "",
                    "slides_source": str(stored_presentation) if stored_presentation else "",
                    "raw_transcript": str(raw_transcript_path),
                    "cleaned_transcript_json": str(transcript_path),
                    "cleaned_transcript_text": str(transcript_text_path),
                    "extracted_slides": str(slides_path),
                    "slide_summaries": str(slide_summaries_path),
                    "alignment": str(alignment_path),
                    "quality_report": str(quality_report_path),
                    "notes_markdown": str(markdown_path),
                    "notes_pdf": str(pdf_path),
                },
            },
        )
        notify("complete", "Lecture notes are ready")
        return PipelineResult(
            run_id=run_id,
            run_dir=run_dir,
            notes_markdown=notes,
            markdown_path=markdown_path,
            pdf_path=pdf_path,
            transcript_path=transcript_path,
            raw_transcript_path=raw_transcript_path,
            transcript_text_path=transcript_text_path,
            slides_path=slides_path,
            slide_summaries_path=slide_summaries_path,
            alignment_path=alignment_path,
            quality_report_path=quality_report_path,
            manifest_path=manifest_path,
            lecture_name=identity.base_name,
            lecture_title=identity.display_title,
            indexed_chunks=indexed_chunks,
        )

    @staticmethod
    def _lecture_context(lecture_title: str, slides: list[dict[str, Any]]) -> str:
        blocks = [f"Lecture: {lecture_title}"]
        for slide in slides:
            number = slide.get("slide", "?")
            title = clean_text(str(slide.get("title", "")))
            content = clean_text(str(slide.get("content", "")))
            blocks.append(f"Slide {number}: {title}\n{content[:800]}")
            if sum(len(item) for item in blocks) >= 12_000:
                break
        return "\n\n".join(blocks)[:12_000]

    @staticmethod
    def _transcript_text(transcript: dict[str, Any]) -> str:
        lines: list[str] = []
        for paragraph in transcript.get("paragraphs", []):
            if not isinstance(paragraph, dict):
                continue
            text = clean_text(str(paragraph.get("text", "")))
            if not text:
                continue
            timestamp = f"{paragraph.get('start_time', '')}–{paragraph.get('end_time', '')}".strip("–")
            lines.append(f"[{timestamp}] {text}" if timestamp else text)
        return "\n\n".join(lines) + ("\n" if lines else "")

    @staticmethod
    def _recording_sections(transcript: dict[str, Any], section_seconds: int = 900) -> list[dict[str, Any]]:
        """Create neutral timestamped note sections when no slide deck exists."""
        paragraphs = transcript.get("paragraphs", [])
        if not paragraphs:
            raise RuntimeError(
                "The recording did not produce usable speech, so recording-only notes cannot be created."
            )
        duration = max(float(paragraph.get("end", 0.0)) for paragraph in paragraphs)
        sections: list[dict[str, Any]] = []
        start = 0.0
        number = 1
        while start < duration:
            end = min(duration, start + section_seconds)
            sections.append(
                {
                    "slide": number,
                    "title": f"{seconds_to_timestamp(start)}–{seconds_to_timestamp(end)}",
                    "content": "",
                    "notes": "",
                    "images": [],
                    "visual_text": [],
                    "section_kind": "recording",
                    "section_start": start,
                    "section_end": end,
                }
            )
            start = end
            number += 1
        return sections

    @staticmethod
    def _empty_alignment(slides: list[dict[str, Any]], output_path: Path) -> dict[str, Any]:
        payload = {
            "metadata": {"mode": "slides_only", "paragraph_count": 0, "method_counts": {}},
            "paragraph_alignment": [],
            "slides": [
                {"slide": int(slide["slide"]), "slide_time": None, "paragraph_ids": [], "paragraphs": []}
                for slide in slides
            ],
        }
        dump_json(output_path, payload)
        return payload

    @staticmethod
    def _recording_alignment(
        sections: list[dict[str, Any]], transcript: dict[str, Any], output_path: Path
    ) -> dict[str, Any]:
        grouped: dict[int, list[dict[str, Any]]] = {int(section["slide"]): [] for section in sections}
        assignments: list[dict[str, Any]] = []
        for paragraph in transcript.get("paragraphs", []):
            midpoint = (float(paragraph["start"]) + float(paragraph["end"])) / 2
            section = next(
                (item for item in sections if midpoint < float(item["section_end"])), sections[-1]
            )
            record = {
                "paragraph_id": int(paragraph["id"]),
                "slide": int(section["slide"]),
                "start": float(paragraph["start"]),
                "end": float(paragraph["end"]),
                "start_time": paragraph["start_time"],
                "end_time": paragraph["end_time"],
                "confidence": 1.0,
                "method": "recording_section",
            }
            assignments.append(record)
            grouped[int(section["slide"])].append({**paragraph, "alignment": record})
        payload: dict[str, Any] = {
            "metadata": {
                "mode": "recording_only",
                "paragraph_count": len(assignments),
                "method_counts": {"recording_section": len(assignments)},
            },
            "paragraph_alignment": assignments,
            "slides": [
                {
                    "slide": int(section["slide"]),
                    "slide_time": seconds_to_timestamp(float(section["section_start"])),
                    "paragraph_ids": [item["id"] for item in grouped[int(section["slide"])]],
                    "paragraphs": grouped[int(section["slide"])],
                }
                for section in sections
            ],
        }
        dump_json(output_path, payload)
        return payload
