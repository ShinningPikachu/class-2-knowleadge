"""Persistent local Chroma vector store exposed through LangChain."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .embeddings import LocalEmbeddingError, OllamaEmbedder, SourceDocument


class RAGError(RuntimeError):
    """Raised for local vector-store initialization or query failures."""


class LocalRAG:
    """Store Ollama vectors in an on-disk Chroma collection through LangChain."""

    def __init__(self, database_path: str | Path, embedder: OllamaEmbedder, collection_name: str = "lecture") -> None:
        self.embedder = embedder
        self.collection_name = collection_name
        try:
            from chromadb.config import Settings
            from langchain_chroma import Chroma
        except ImportError as exc:
            raise RAGError("langchain-chroma is not installed. Run: pip install -r requirements.txt") from exc
        try:
            Path(database_path).mkdir(parents=True, exist_ok=True)
            self._store = Chroma(
                collection_name=collection_name,
                embedding_function=embedder,
                persist_directory=str(database_path),
                collection_metadata={"hnsw:space": "cosine"},
                client_settings=Settings(anonymized_telemetry=False),
            )
        except Exception as exc:
            raise RAGError(f"Could not initialize local Chroma database: {exc}") from exc

    def index(self, documents: list[SourceDocument], batch_size: int = 48) -> int:
        """Embed and store all chunks.  No embedding data leaves localhost."""
        if not documents:
            raise RAGError("No slide or transcript text was available to index.")
        try:
            from langchain_core.documents import Document

            for start in range(0, len(documents), batch_size):
                batch = documents[start : start + batch_size]
                langchain_documents = [
                    Document(page_content=document.text, metadata=document.metadata) for document in batch
                ]
                self._store.add_documents(
                    documents=langchain_documents,
                    ids=[document.id for document in batch],
                )
            return len(documents)
        except LocalEmbeddingError:
            raise
        except Exception as exc:
            raise RAGError(f"Could not index lecture material: {exc}") from exc

    def search(self, query: str, n_results: int = 8, where: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Return source text plus its original metadata for transparent prompting."""
        if not query.strip():
            return []
        try:
            results = self._store.similarity_search_with_score(
                query=query,
                k=n_results,
                filter=where,
            )
            return [
                {"text": document.page_content, "metadata": document.metadata, "distance": float(score)}
                for document, score in results
            ]
        except LocalEmbeddingError:
            raise
        except Exception as exc:
            raise RAGError(f"Could not retrieve lecture context: {exc}") from exc
