"""Interactive search-result cards that open a result at its matched passage."""

from __future__ import annotations

from pathlib import Path

import streamlit.components.v1 as components

from ..library import SearchResult


_COMPONENT_PATH = Path(__file__).parent / "components" / "search_results"
_search_results = components.declare_component(
    "class_knowledge_search_results",
    path=str(_COMPONENT_PATH),
)


def render_search_result_cards(results: list[SearchResult], *, key: str) -> None:
    """Show result snippets whose double-click opens the focused source file."""
    _search_results(
        results=[
            {
                "document_id": result.document_id,
                "document_name": result.document_name,
                "subject_name": result.subject_name,
                "locator": result.locator[:160],
                # A search snippet is intentionally bounded, both for the URL and
                # for displaying an immediate, trustworthy match in the new tab.
                "text": result.text[:1_200],
            }
            for result in results
        ],
        key=key,
        default=None,
    )
