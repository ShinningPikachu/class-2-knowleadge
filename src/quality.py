"""Fail-closed quality checks for lecture inputs, alignment, and final notes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .utils import dump_json


class QualityGateError(RuntimeError):
    """Raised when evidence quality is too weak for trustworthy notes."""


def validate_evidence_quality(
    slides: list[dict[str, Any]],
    transcript: dict[str, Any],
    alignment: dict[str, Any],
    output_path: str | Path,
) -> dict[str, Any]:
    """Measure source coverage and stop when alignment is mostly guesswork."""
    paragraphs = transcript.get("paragraphs", [])
    assignments = alignment.get("paragraph_alignment", [])
    temporal_fallbacks = sum(item.get("method") == "temporal_fallback" for item in assignments)
    fallback_ratio = temporal_fallbacks / len(assignments) if assignments else 0.0
    mapped_slide_numbers = {
        int(item["slide"])
        for item in alignment.get("slides", [])
        if item.get("paragraph_ids")
    }
    source_empty_slides = [
        int(slide["slide"])
        for slide in slides
        if not any(
            [
                str(slide.get("content", "")).strip(),
                str(slide.get("notes", "")).strip(),
                slide.get("visual_text"),
                int(slide["slide"]) in mapped_slide_numbers,
            ]
        )
    ]
    warnings: list[str] = []
    errors: list[str] = []
    if fallback_ratio > 0.35:
        warnings.append(
            f"{fallback_ratio:.0%} of transcript paragraphs required temporal fallback alignment."
        )
    if fallback_ratio > 0.70:
        errors.append(
            "More than 70% of transcript alignment is low-confidence temporal fallback; "
            "the system will not generate potentially misleading notes."
        )
    if source_empty_slides:
        warnings.append(
            "No extractable slide or professor evidence was found for slides: "
            + ", ".join(map(str, source_empty_slides))
        )
    if not paragraphs and assignments:
        errors.append("Transcript alignment exists but the transcript contains no usable paragraphs.")
    if not paragraphs and slides:
        warnings.append("No recording was supplied; notes are grounded in the slide deck only.")

    report = {
        "status": "failed" if errors else ("warning" if warnings else "passed"),
        "metrics": {
            "slide_count": len(slides),
            "transcript_paragraph_count": len(paragraphs),
            "alignment_count": len(assignments),
            "temporal_fallback_count": temporal_fallbacks,
            "temporal_fallback_ratio": round(fallback_ratio, 4),
            "slides_with_aligned_speech": len(mapped_slide_numbers),
            "slides_without_any_evidence": source_empty_slides,
        },
        "warnings": warnings,
        "errors": errors,
    }
    dump_json(Path(output_path), report)
    if errors:
        raise QualityGateError(" ".join(errors) + f" Review {output_path} for measured details.")
    return report


def validate_final_notes(markdown: str, slide_count: int, section_label: str = "Slide") -> None:
    """Ensure every source section has exactly one concise exported note."""
    lines = markdown.splitlines()
    rendered_slides = sum(
        line.startswith(f"# {section_label} ") or line.startswith(f"## {section_label} ")
        for line in lines
    )
    if rendered_slides != slide_count:
        raise QualityGateError(
            f"Final notes contain {rendered_slides} {section_label.lower()} sections for {slide_count} source sections."
        )
    if "## Concise summary" in markdown:
        concise_sections = sum(line.strip() == "## Concise summary" for line in lines)
        if concise_sections != slide_count:
            raise QualityGateError(
                f"Final notes contain {concise_sections} concise summaries for {slide_count} source sections."
            )
