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
        if len(slide_vectors) != len(slides) or len(paragraph_vectors) != len(paragraphs):
            raise AlignmentError(
                "The local embedding model returned an incomplete batch; "
                "no transcript paragraphs were aligned."
            )

        duration = max(float(paragraph.get("end", 0.0)) for paragraph in paragraphs) or 1.0
        indices, references = self._sequence_path(
            slide_numbers, slide_vectors, paragraphs, paragraph_vectors, duration,
        )
        assignments: list[dict[str, Any]] = []
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        method_counts: Counter[str] = Counter()
        for paragraph, vector, index, reference in zip(paragraphs, paragraph_vectors, indices, references):
            slide_number = slide_numbers[index]
            similarity = self._cosine(vector, slide_vectors[index])
            if reference is not None:
                confidence, method = 1.0, "spoken_reference"
            elif similarity >= self.config.semantic_alignment_threshold:
                confidence, method = similarity, "semantic"
            else:
                confidence, method = max(similarity, 0.0), "temporal_fallback"
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
                "alignment_version": 2,
                "strategy": "monotonic_semantic_with_spoken_anchors",
            },
            "paragraph_alignment": assignments,
            "slides": slide_alignment,
        }
        dump_json(Path(output_path), payload)
        return payload

    def _sequence_path(
        self,
        slide_numbers: list[int],
        slide_vectors: list[list[float]],
        paragraphs: list[dict[str, Any]],
        paragraph_vectors: list[list[float]],
        duration: float,
    ) -> tuple[list[int], list[int | None]]:
        """Align speech in slide order, permitting explicit spoken revisits.

        Global best-match embeddings alone can jump from late to early slides.
        Dynamic programming uses semantic fit, relative time and slide-order
        constraints; only a spoken slide reference permits a backward jump.
        """
        n = len(slide_numbers)
        allowed = set(slide_numbers)
        references = [
            self._spoken_slide_reference(str(item.get("text", "")), allowed)
            for item in paragraphs
        ]
        prior = [0.0] * n
        predecessors: list[list[int]] = []
        jump_cost = 0.025
        time_cost = 0.14
        for paragraph, vector, reference in zip(paragraphs, paragraph_vectors, references):
            midpoint = (float(paragraph.get("start", 0.0)) + float(paragraph.get("end", 0.0))) / 2
            relative_time = min(1.0, max(0.0, midpoint / duration))
            emission = [
                self._cosine(vector, slide_vector)
                - time_cost * abs(index / max(1, n - 1) - relative_time)
                for index, slide_vector in enumerate(slide_vectors)
            ]
            current = [float("-inf")] * n
            previous_indices = [-1] * n
            if reference is not None:
                index = slide_numbers.index(reference)
                previous = max(range(n), key=prior.__getitem__)
                current[index] = prior[previous] + emission[index]
                previous_indices[index] = previous
            else:
                best_value = float("-inf")
                best_index = 0
                for index in range(n):
                    candidate = prior[index] + jump_cost * index
                    if candidate > best_value:
                        best_value, best_index = candidate, index
                    current[index] = best_value - jump_cost * index + emission[index]
                    previous_indices[index] = best_index
            prior = current
            predecessors.append(previous_indices)
        position = max(range(n), key=prior.__getitem__)
        path = []
        for step in range(len(predecessors) - 1, -1, -1):
            path.append(position)
            position = predecessors[step][position]
        path.reverse()
        return path, references

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
