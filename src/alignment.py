"""Map timestamped transcript paragraphs to presentation slides."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .config import PipelineConfig
from .embeddings import OllamaEmbedder
from .utils import dump_json, seconds_to_timestamp


class AlignmentError(RuntimeError):
    """Raised when transcript-to-slide mapping cannot be completed."""


class SlideAligner:
    """Use spoken slide references first and local semantic similarity second."""

    _SLIDE_REFERENCE = re.compile(r"\b(?:slide|page|figure)\s*(?:number\s*)?(\d{1,3})\b", re.IGNORECASE)

    def __init__(self, config: PipelineConfig, embedder: OllamaEmbedder) -> None:
        self.config = config
        self.embedder = embedder

    def align(
        self,
        slides: list[dict[str, Any]],
        transcript: dict[str, Any],
        output_path: str | Path,
    ) -> dict[str, Any]:
        """Save ``alignment.json`` with a slide number for every paragraph.

        Explicit spoken references win.  Other paragraphs use cosine similarity
        of local embeddings; only weak matches use a clearly-labelled temporal
        fallback, so downstream notes retain provenance.
        """
        paragraphs = transcript.get("paragraphs", [])
        if not slides:
            raise AlignmentError("There are no slides to align.")
        if not paragraphs:
            raise AlignmentError("There are no transcript paragraphs to align.")

        slide_numbers = [int(slide["slide"]) for slide in slides]
        slide_texts = [self._slide_text(slide) for slide in slides]
        try:
            slide_vectors = self.embedder.embed_documents(slide_texts)
            paragraph_vectors = self.embedder.embed_documents(paragraph.get("text", "") for paragraph in paragraphs)
        except Exception as exc:
            raise AlignmentError(f"Semantic alignment requires the local embedding model. {exc}") from exc

        duration = max(float(paragraph.get("end", 0.0)) for paragraph in paragraphs) or 1.0
        assignments: list[dict[str, Any]] = []
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        method_counts: Counter[str] = Counter()
        for paragraph, vector in zip(paragraphs, paragraph_vectors):
            reference = self._spoken_slide_reference(paragraph.get("text", ""), set(slide_numbers))
            if reference is not None:
                slide_number, confidence, method = reference, 1.0, "spoken_reference"
            else:
                scores = [self._cosine(vector, slide_vector) for slide_vector in slide_vectors]
                best_index = max(range(len(scores)), key=scores.__getitem__)
                score = scores[best_index]
                if score >= self.config.semantic_alignment_threshold:
                    slide_number, confidence, method = slide_numbers[best_index], score, "semantic"
                else:
                    # This keeps every timestamp accessible, but identifies low-confidence alignment.
                    position = min(len(slide_numbers) - 1, int((float(paragraph.get("start", 0.0)) / duration) * len(slide_numbers)))
                    slide_number, confidence, method = slide_numbers[position], max(score, 0.0), "temporal_fallback"
            record = {
                "paragraph_id": int(paragraph["id"]),
                "slide": slide_number,
                "start": float(paragraph.get("start", 0.0)),
                "end": float(paragraph.get("end", 0.0)),
                "start_time": paragraph.get("start_time", seconds_to_timestamp(0)),
                "end_time": paragraph.get("end_time", seconds_to_timestamp(0)),
                "confidence": round(float(confidence), 4),
                "method": method,
            }
            assignments.append(record)
            grouped[slide_number].append({**paragraph, "alignment": record})
            method_counts[method] += 1

        slide_alignment = []
        for slide in slides:
            number = int(slide["slide"])
            mapped = grouped[number]
            slide_alignment.append(
                {
                    "slide": number,
                    "slide_time": mapped[0]["start_time"] if mapped else None,
                    "paragraph_ids": [item["id"] for item in mapped],
                    "paragraphs": mapped,
                }
            )
        payload = {
            "metadata": {
                "paragraph_count": len(paragraphs),
                "method_counts": dict(method_counts),
                "semantic_threshold": self.config.semantic_alignment_threshold,
            },
            "paragraph_alignment": assignments,
            "slides": slide_alignment,
        }
        dump_json(Path(output_path), payload)
        return payload

    @staticmethod
    def _slide_text(slide: dict[str, Any]) -> str:
        return " ".join(
            str(value)
            for value in (slide.get("title"), slide.get("content"), slide.get("notes"), " ".join(slide.get("visual_text", [])))
            if value
        )

    def _spoken_slide_reference(self, text: str, allowed: set[int]) -> int | None:
        match = self._SLIDE_REFERENCE.search(text)
        if not match:
            return None
        candidate = int(match.group(1))
        return candidate if candidate in allowed else None

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        numerator = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0
