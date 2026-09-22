"""Streamlit interface for the fully local lecture AI assistant."""

from __future__ import annotations

import json
import re
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import streamlit as st

from src.config import PipelineConfig, project_path
from src.pipeline import LecturePipeline
from src.utils import safe_filename


st.set_page_config(page_title="class-2-knowleadge", page_icon="🎓", layout="wide")


def _save_temporary_upload(upload: Any, directory: Path) -> Path:
    """Materialize an uploaded file only for the duration of the pipeline run."""
    destination = directory / safe_filename(upload.name)
    destination.write_bytes(upload.getbuffer())
    return destination


def _render_result() -> None:
    result = st.session_state.get("lecture_result")
    if not result:
        return
    st.success(f"Notes generated successfully — {result['slides']} slides and {result['chunks']} indexed chunks.")
    st.markdown(result["notes"])
    left, right = st.columns(2)
    with left:
        st.download_button(
            "Download Markdown",
            data=result["markdown_path"].read_bytes(),
            file_name="lecture_notes.md",
            mime="text/markdown",
            use_container_width=True,
        )


def _progress_percent(stage: str, message: str) -> int:
    """Map pipeline milestones to honest, monotonically increasing UI progress."""
    base = {"preflight": 2, "slides": 64, "alignment": 70, "quality": 76, "rag": 82, "export": 98, "complete": 100}
    if stage == "recording":
        match = re.search(r"(\d+)/(\d+) chunks complete", message)
        if match:
            return min(60, 5 + round(55 * int(match.group(1)) / max(1, int(match.group(2)))))
        return 5
    if stage == "notes":
        match = re.search(r"\((\d+)/(\d+)\)", message)
        if match:
            return min(96, 84 + round(12 * int(match.group(1)) / max(1, int(match.group(2)))))
        return 84
    return base.get(stage, 1)
    with right:
        st.download_button(
            "Download PDF",
            data=result["pdf_path"].read_bytes(),
            file_name="lecture_notes.pdf",
            mime="application/pdf",
            use_container_width=True,
        )
    with st.expander("Inspect local processing artifacts"):
        st.code(
            "\n".join(
                [
                    f"Run folder: {result['run_dir']}",
                    f"Transcript: {result['transcript_path']}",
                    f"Slides: {result['slides_path']}",
                    f"Alignment: {result['alignment_path']}",
                    f"Quality report: {result['quality_report_path']}",
                ]
            )
        )


st.title("🎓 class-2-knowleadge")
st.caption("Offline transcription, slide extraction, local RAG, and Ollama-generated study notes.")

with st.sidebar:
    st.header("Local model settings")
    st.caption("32 GB quality profile: the 27B model runs after Whisper is released from memory.")
    llm_model = st.text_input("Study-note model", value="qwen3.5:27b")
    embedding_model = st.text_input("Embedding model", value="nomic-embed-text")
    ollama_host = st.text_input("Ollama URL", value="http://127.0.0.1:11434")
    whisper_model = st.text_input("faster-whisper model or local path", value="large-v3")
    language = st.text_input("Spoken language (optional ISO code)", value="")
    with st.expander("Advanced processing settings"):
        llm_temperature = st.slider("LLM temperature", 0.0, 1.0, 0.10, 0.05)
        ollama_num_ctx = st.select_slider("Ollama context window", [4096, 8192, 16384, 32768], value=16384)
        st.caption("Model thinking and the second factual review are always enabled in the quality profile.")
        whisper_device = st.selectbox("Whisper device", ["auto", "cpu", "cuda"], index=0)
        whisper_compute_type = st.selectbox("Whisper compute type", ["float32", "int8", "float16"], index=0)
        whisper_parallel_workers = st.selectbox(
            "Parallel transcription workers", [1, 2], index=1,
            help="Two workers use the same large-v3 quality settings on separate recording chunks. Do not use more on a 32 GB machine.",
        )
        enable_ocr = st.checkbox("Use local Tesseract OCR for diagram/image text", value=True)
        semantic_threshold = st.slider("Semantic alignment threshold", 0.0, 0.8, 0.20, 0.01)
        media_chunk_minutes = st.slider("Recording chunk length (minutes)", 10, 45, 30, 5)
        media_overlap_seconds = st.slider("Chunk boundary overlap (seconds)", 5, 30, 15, 5)
        chunk_size = st.number_input("RAG chunk size (characters)", 200, 3000, 900, 50)
        chunk_overlap = st.number_input("RAG overlap (characters)", 0, 1000, 140, 10)
        max_slide_context = st.number_input("Maximum context per slide (characters)", 2000, 50000, 28000, 1000)

st.write("Provide a lecture recording, a PDF/PPT/PPTX deck, or both. Inputs and every intermediate JSON artifact stay on this computer.")
input_mode = st.radio("Input method", ["Upload files", "Use local file paths"], horizontal=True)
audio_upload = None
slides_upload = None
recording_local = ""
slides_local = ""
input_left, input_right = st.columns(2)
if input_mode == "Upload files":
    with input_left:
        audio_upload = st.file_uploader(
            "Lecture recording (optional)",
            type=["mp3", "wav", "m4a", "aac", "flac", "ogg", "opus", "mp4", "mov", "mkv", "webm", "m4v"],
            help="Audio or video; the video audio track is transcribed. Visual-only material must be present in the deck.",
        )
    with input_right:
        slides_upload = st.file_uploader("Lecture slides (optional)", type=["pdf", "ppt", "pptx"], help="PDF, PPTX, or legacy PPT (LibreOffice required for PPT)")
else:
    with input_left:
        recording_local = st.text_input("Local audio/video path (optional)", placeholder="/path/to/lecture.mp4")
    with input_right:
        slides_local = st.text_input("Local slide-deck path (optional)", placeholder="/path/to/lecture.pdf")
    st.caption("Local paths avoid loading multi-gigabyte video files into browser memory.")
lecture_title = st.text_input("Lecture title (optional)", placeholder="Defaults to the first slide title")
resume_run_directory = st.text_input(
    "Resume interrupted run folder (optional)",
    placeholder="/path/to/class-2-knowleadge/runs/lecture_...",
    help="Reuses completed transcription checkpoints. Leave blank to create a new run.",
)

if st.button("Generate Lecture Notes", type="primary", use_container_width=True):
    st.session_state.pop("lecture_result", None)
    missing_upload = input_mode == "Upload files" and not (audio_upload or slides_upload)
    missing_path = input_mode == "Use local file paths" and not (recording_local.strip() or slides_local.strip())
    if not resume_run_directory.strip() and (missing_upload or missing_path):
        st.error("Please provide at least one source: a lecture recording or a PDF/PPT/PPTX slide deck.")
    else:
        config = PipelineConfig(
            ollama_host=ollama_host.strip(),
            llm_model=llm_model.strip(),
            embedding_model=embedding_model.strip(),
            llm_temperature=llm_temperature,
            ollama_num_ctx=int(ollama_num_ctx),
            ollama_thinking="high",
            quality_review=True,
            whisper_model=whisper_model.strip(),
            whisper_device=whisper_device,
            whisper_compute_type=whisper_compute_type,
            whisper_parallel_workers=int(whisper_parallel_workers),
            language=language.strip() or None,
            enable_ocr=enable_ocr,
            semantic_alignment_threshold=semantic_threshold,
            media_chunk_seconds=int(media_chunk_minutes * 60),
            media_overlap_seconds=int(media_overlap_seconds),
            chunk_size=int(chunk_size),
            chunk_overlap=int(chunk_overlap),
            max_slide_context_chars=int(max_slide_context),
        )
        stage_placeholder = st.empty()
        progress_bar = st.progress(0, text="Preparing local lecture pipeline…")
        with st.status("Preparing local lecture pipeline…", expanded=True) as status:
            def on_stage(stage: str, message: str) -> None:
                stage_placeholder.info(f"**{stage.title()}** — {message}")
                percent = _progress_percent(stage, message)
                progress_bar.progress(percent, text=f"{percent}% — {message.split('Confirmed transcript preview:')[0][:160]}")
                status.write(f"**{stage.title()}** — {message}")
                status.update(label=f"{percent}% — {stage.title()}", state="running")

            try:
                with ExitStack() as stack:
                    if resume_run_directory.strip():
                        audio_path = None
                        presentation_path = None
                    elif input_mode == "Upload files":
                        temp_dir = stack.enter_context(tempfile.TemporaryDirectory(prefix="lecture_ai_"))
                        temporary_dir = Path(temp_dir)
                        audio_path = _save_temporary_upload(audio_upload, temporary_dir) if audio_upload else None
                        presentation_path = _save_temporary_upload(slides_upload, temporary_dir) if slides_upload else None
                    else:
                        audio_path = Path(recording_local).expanduser().resolve() if recording_local.strip() else None
                        presentation_path = Path(slides_local).expanduser().resolve() if slides_local.strip() else None
                        if (audio_path and not audio_path.is_file()) or (presentation_path and not presentation_path.is_file()):
                            raise FileNotFoundError("A supplied local input path does not point to a readable file.")
                    pipeline = LecturePipeline(config, project_path())
                    if resume_run_directory.strip():
                        result = pipeline.resume(
                            resume_run_directory.strip(),
                            lecture_title.strip() or None,
                            on_stage=on_stage,
                        )
                    else:
                        result = pipeline.run(
                            audio_path,
                            presentation_path,
                            lecture_title.strip() or None,
                            on_stage=on_stage,
                        )
                st.session_state["lecture_result"] = {
                    "notes": result.notes_markdown,
                    "run_dir": result.run_dir,
                    "markdown_path": result.markdown_path,
                    "pdf_path": result.pdf_path,
                    "transcript_path": result.transcript_path,
                    "slides_path": result.slides_path,
                    "alignment_path": result.alignment_path,
                    "quality_report_path": result.quality_report_path,
                    "chunks": result.indexed_chunks,
                    "slides": len(json.loads(result.slides_path.read_text(encoding="utf-8"))["slides"]),
                }
                status.update(label="Lecture notes generated locally", state="complete", expanded=False)
                progress_bar.progress(100, text="100% — Lecture notes generated locally")
                stage_placeholder.empty()
            except Exception as exc:
                status.update(label="Processing stopped", state="error", expanded=True)
                st.exception(exc)

_render_result()
