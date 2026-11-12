"""Fail-closed quality checks for lecture inputs, alignment, and final notes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .utils import dump_json


class QualityGateError(RuntimeError):
    """Raised when evidence quality is too weak for trustworthy notes."""


def section_source_coverage(
    slide: dict[str, Any],
    aligned_section: dict[str, Any] | None = None,
) -> dict[str, bool]:
    """Report whether a lecture section has slide and teacher-audio evidence."""
    number = int(slide.get("slide", 0))
    title = " ".join(str(slide.get("title", "")).split())
    meaningful_title = bool(title) and title.casefold() not in {
        f"slide {number}".casefold(),
        str(number),
    }
    return {
        "slide": meaningful_title
        or any(
            [
                str(slide.get("content", "")).strip(),
                str(slide.get("notes", "")).strip(),
                slide.get("visual_text"),
            ]
        ),
        "teacher_audio": any(
            str(paragraph.get("text", "")).strip()
            for paragraph in (aligned_section or {}).get("paragraphs", [])
            if isinstance(paragraph, dict)
        ),
    }


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
    unexpected_backtracks = [
        (previous.get("slide"), current.get("slide"))
        for previous, current in zip(assignments, assignments[1:])
        if int(current.get("slide", 0)) < int(previous.get("slide", 0))
        and current.get("method") != "spoken_reference"
    ]
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
    if unexpected_backtracks:
        errors.append(
            f"{len(unexpected_backtracks)} transcript paragraphs jump backward without a spoken slide reference."
        )
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
            "unexpected_backtrack_count": len(unexpected_backtracks),
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


def validate_slide_summaries(
    slides: list[dict[str, Any]],
    alignment: dict[str, Any],
    summaries: list[dict[str, Any]],
) -> None:
    """Ensure every source section produced one bounded, auditable combined note."""
    expected_numbers = [int(slide["slide"]) for slide in slides]
    aligned_by_slide = {
        int(item["slide"]): item
        for item in alignment.get("slides", [])
        if isinstance(item, dict) and str(item.get("slide", "")).isdigit()
    }
    observed: dict[int, dict[str, Any]] = {}
    duplicates: list[int] = []
    for item in summaries:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item["slide"])
        except (KeyError, TypeError, ValueError):
            continue
        if number in observed:
            duplicates.append(number)
        observed[number] = item

    missing = [number for number in expected_numbers if number not in observed]
    unexpected = sorted(number for number in observed if number not in set(expected_numbers))
    errors: list[str] = []
    if missing:
        errors.append("Missing combined notes for sections: " + ", ".join(map(str, missing)) + ".")
    if duplicates:
        errors.append("Duplicate combined notes for sections: " + ", ".join(map(str, sorted(set(duplicates)))) + ".")
    if unexpected:
        errors.append("Combined notes reference unknown sections: " + ", ".join(map(str, unexpected)) + ".")

    placeholder_starts = (
        "no concise source-grounded summary is available",
        "a combined slide-and-lecture note has not been generated",
    )
    for slide in slides:
        number = int(slide["slide"])
        item = observed.get(number)
        if item is None:
            continue
        summary = " ".join(str(item.get("summary", "")).split())
        if not summary:
            errors.append(f"Section {number} has an empty combined note.")
            continue
        if len(summary.split()) > 90:
            errors.append(f"Section {number} exceeds the 90-word combined-note limit.")

        expected_sources = section_source_coverage(slide, aligned_by_slide.get(number))
        recorded_sources = item.get("sources")
        if not isinstance(recorded_sources, dict) or any(
            bool(recorded_sources.get(source)) != present
            for source, present in expected_sources.items()
        ):
            errors.append(f"Section {number} has incorrect source-coverage metadata.")
        if any(expected_sources.values()) and summary.casefold().startswith(placeholder_starts):
            errors.append(f"Section {number} has source material but only a placeholder note.")

    if errors:
        raise QualityGateError(" ".join(errors))
