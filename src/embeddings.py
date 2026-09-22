"""Local Ollama embeddings and source-aware chunk construction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .config import PipelineConfig
from .utils import clean_text


class LocalEmbeddingError(RuntimeError):
    """Raised when the local Ollama embedding endpoint is unavailable."""


@dataclass
class SourceDocument:
    """A Chroma-ready document with source metadata retained for citations."""

    id: str
    text: str
    metadata: dict[str, str | int | float | bool]


class OllamaEmbedder:
    """Generate vectors via Ollama running on the same machine."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        try:
            import ollama
        except ImportError as exc:
            raise LocalEmbeddingError("ollama is not installed. Run: pip install -r requirements.txt") from exc
        self._client = ollama.Client(host=config.ollama_host)

    def embed_documents(self, texts: Iterable[str]) -> list[list[float]]:
        """Embed a batch with a modern Ollama API and an older-client fallback."""
        values = [text if text.strip() else "[empty]" for text in texts]
        if not values:
            return []
        try:
            result = self._client.embed(model=self.config.embedding_model, input=values)
            vectors = self._read_field(result, "embeddings")
            if vectors and len(vectors) == len(values):
                return [list(map(float, vector)) for vector in vectors]
        except AttributeError:
            # ollama-python < 0.4 exposed one-input `embeddings` instead.
            pass
        except Exception as exc:
            raise LocalEmbeddingError(self._helpful_error(exc)) from exc

        try:
            vectors = []
            for value in values:
                result = self._client.embeddings(model=self.config.embedding_model, prompt=value)
                vectors.append(list(map(float, self._read_field(result, "embedding"))))
            return vectors
        except Exception as exc:
            raise LocalEmbeddingError(self._helpful_error(exc)) from exc

    def embed_query(self, text: str) -> list[float]:
        """LangChain Embeddings-compatible single-query method."""
        vectors = self.embed_documents([text])
        if not vectors:
            raise LocalEmbeddingError("Ollama did not return an embedding for the query.")
        return vectors[0]

    def verify_local_models(self) -> None:
        """Fail fast if either configured Ollama model is absent locally."""
        for purpose, model in (
            ("embedding", self.config.embedding_model),
            ("generation", self.config.llm_model),
        ):
            try:
                self._client.show(model)
            except Exception as exc:
                raise LocalEmbeddingError(
                    f"The local Ollama {purpose} model '{model}' is unavailable at "
                    f"{self.config.ollama_host}. Start Ollama and run `ollama pull {model}` "
                    f"before going offline. Details: {exc}"
                ) from exc

    @staticmethod
    def _read_field(result: Any, field: str) -> Any:
        if isinstance(result, dict):
            return result.get(field)
        return getattr(result, field, None)

    def _helpful_error(self, error: Exception) -> str:
        return (
            f"Could not use local Ollama embedding model '{self.config.embedding_model}' at "
            f"{self.config.ollama_host}. Start Ollama and run "
            f"`ollama pull {self.config.embedding_model}`. Details: {error}"
        )


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """Split text on word boundaries while preserving a small retrieval overlap."""
    text = clean_text(text)
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind(" ", start, end)
            if boundary > start + size // 2:
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        next_start = max(end - overlap, start + 1)
        while next_start < len(text) and text[next_start].isspace():
            next_start += 1
        start = next_start
    return chunks


def build_source_documents(
    slides: list[dict[str, Any]],
    transcript: dict[str, Any],
    config: PipelineConfig,
    alignment: dict[str, Any] | None = None,
) -> list[SourceDocument]:
    """Build separately tagged slide and transcript chunks for local RAG."""
    documents: list[SourceDocument] = []
    paragraph_alignment = {
        int(item["paragraph_id"]): item for item in (alignment or {}).get("paragraph_alignment", [])
    }
    for slide in slides:
        slide_number = int(slide["slide"])
        parts = [f"Slide {slide_number}: {slide.get('title', '')}", slide.get("content", "")]
        if slide.get("notes"):
            parts.append(f"Speaker notes: {slide['notes']}")
        if slide.get("visual_text"):
            parts.append("Text detected in diagrams/images: " + " ".join(slide["visual_text"]))
        text = "\n".join(part for part in parts if clean_text(part))
        for chunk_index, chunk in enumerate(chunk_text(text, config.chunk_size, config.chunk_overlap), start=1):
            documents.append(
                SourceDocument(
                    id=f"slide-{slide_number}-chunk-{chunk_index}",
                    text=chunk,
                    metadata={"source": "slide", "slide_number": slide_number, "chunk": chunk_index},
                )
            )

    for paragraph in transcript.get("paragraphs", []):
        paragraph_id = int(paragraph["id"])
        mapped = paragraph_alignment.get(paragraph_id, {})
        for chunk_index, chunk in enumerate(
            chunk_text(paragraph.get("text", ""), config.chunk_size, config.chunk_overlap), start=1
        ):
            metadata: dict[str, str | int | float | bool] = {
                "source": "transcript",
                "paragraph_id": paragraph_id,
                "start": float(paragraph.get("start", 0.0)),
                "end": float(paragraph.get("end", 0.0)),
                "chunk": chunk_index,
            }
            if mapped:
                metadata.update(
                    {
                        "aligned_slide": int(mapped["slide"]),
                        "alignment_method": str(mapped["method"]),
                        "alignment_confidence": float(mapped["confidence"]),
                    }
                )
            documents.append(
                SourceDocument(
                    id=f"transcript-{paragraph_id}-chunk-{chunk_index}",
                    text=chunk,
                    metadata=metadata,
                )
            )
    return documents
