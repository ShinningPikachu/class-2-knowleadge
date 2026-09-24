"""Configuration shared by the lecture processing pipeline.

All defaults point to models running on the local machine.  Values can be
changed in the Streamlit UI or supplied programmatically through this class.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass
class PipelineConfig:
    """Models and processing limits for one lecture run."""

    # Ollama is deliberately bound to loopback by default: no cloud API is used.
    ollama_host: str = "http://127.0.0.1:11434"
    # 27B Q4 is the quality-first profile for a 32 GB machine. The UI remains
    # configurable for machines where a different local model is preferred.
    llm_model: str = "qwen3.5:27b"
    embedding_model: str = "nomic-embed-text"
    llm_temperature: float = 0.10
    ollama_num_ctx: int = 16_384
    ollama_keep_alive: str = "45m"
    ollama_thinking: str | bool = "high"
    quality_review: bool = True

    # A local path may be used instead of the model name after downloading it.
    whisper_model: str = "large-v3"
    whisper_device: str = "auto"
    whisper_compute_type: str = "float32"
    # Two CTranslate2 workers process independent long-recording chunks in
    # parallel. This is a safe quality-first ceiling for a 32 GB CPU machine.
    whisper_parallel_workers: int = 2
    # Lectures are English by default. Translation is an explicit post-processing
    # job and never changes the source transcript or canonical notes.
    language: str | None = "en"
    media_chunk_seconds: int = 1_800
    media_overlap_seconds: int = 15
    enable_transcript_cleanup: bool = True
    transcript_cleanup_batch_chars: int = 12_000

    chunk_size: int = 900
    chunk_overlap: int = 140
    semantic_alignment_threshold: float = 0.20
    max_slide_context_chars: int = 28_000
    enable_ocr: bool = True

    def validate(self) -> None:
        """Fail early on values that would otherwise cause subtle errors."""
        if not self.llm_model.strip():
            raise ValueError("An Ollama LLM model name is required.")
        if not self.embedding_model.strip():
            raise ValueError("An Ollama embedding model name is required.")
        for model in (self.llm_model, self.embedding_model):
            lowered = model.lower()
            if lowered.endswith("-cloud") or ":cloud" in lowered:
                raise ValueError("Cloud-tagged Ollama models are disabled; configure a fully local model.")
        parsed_host = urlparse(self.ollama_host)
        allowed_hosts = {"127.0.0.1", "localhost", "::1", "host.docker.internal"}
        if parsed_host.scheme not in {"http", "https"} or parsed_host.hostname not in allowed_hosts:
            raise ValueError(
                "For offline operation, ollama_host must use localhost, 127.0.0.1, ::1, "
                "or host.docker.internal."
            )
        if self.chunk_size < 200:
            raise ValueError("chunk_size must be at least 200 characters.")
        if not 0 <= self.chunk_overlap < self.chunk_size:
            raise ValueError("chunk_overlap must be non-negative and smaller than chunk_size.")
        if self.max_slide_context_chars < 2_000:
            raise ValueError("max_slide_context_chars must be at least 2000.")
        if self.ollama_num_ctx < 4_096:
            raise ValueError("ollama_num_ctx must be at least 4096 for grounded lecture notes.")
        if not isinstance(self.ollama_thinking, bool) and self.ollama_thinking not in {"low", "medium", "high"}:
            raise ValueError("ollama_thinking must be a boolean or one of: low, medium, high.")
        if self.media_chunk_seconds < 300:
            raise ValueError("media_chunk_seconds must be at least 300 seconds.")
        if not 0 <= self.media_overlap_seconds < self.media_chunk_seconds / 4:
            raise ValueError("media_overlap_seconds must be non-negative and smaller than one quarter of a chunk.")
        if self.whisper_parallel_workers not in {1, 2}:
            raise ValueError("whisper_parallel_workers must be 1 or 2.")
        if not 2_000 <= self.transcript_cleanup_batch_chars <= 24_000:
            raise ValueError("transcript_cleanup_batch_chars must be between 2000 and 24000.")


def project_path(*parts: str) -> Path:
    """Return a path rooted at the project directory, not the current shell."""
    return Path(__file__).resolve().parents[1].joinpath(*parts)
