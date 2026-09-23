"""Tests for dependency-specific runtime safeguards."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from src.pdf_runtime import muted_mupdf_errors


class _DisplayErrors:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls: list[bool] = []

    def __call__(self, enabled: bool | None = None) -> int | bool:
        if enabled is None:
            return int(self.enabled)
        self.enabled = enabled
        self.calls.append(enabled)
        return enabled


class RuntimeSafeguardsTest(unittest.TestCase):
    def test_mupdf_error_setting_is_restored_after_success(self) -> None:
        display_errors = _DisplayErrors(enabled=True)
        fitz = SimpleNamespace(TOOLS=SimpleNamespace(mupdf_display_errors=display_errors))

        with muted_mupdf_errors(fitz):
            self.assertFalse(display_errors.enabled)

        self.assertTrue(display_errors.enabled)
        self.assertEqual(display_errors.calls, [False, True])

    def test_mupdf_error_setting_is_restored_after_failure(self) -> None:
        display_errors = _DisplayErrors(enabled=True)
        fitz = SimpleNamespace(TOOLS=SimpleNamespace(mupdf_display_errors=display_errors))

        with self.assertRaisesRegex(RuntimeError, "broken PDF"):
            with muted_mupdf_errors(fitz):
                raise RuntimeError("broken PDF")

        self.assertTrue(display_errors.enabled)


if __name__ == "__main__":
    unittest.main()
