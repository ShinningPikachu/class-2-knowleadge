"""Export generated Markdown notes as a portable PDF without web services."""

from __future__ import annotations

import re
from pathlib import Path


class ExportError(RuntimeError):
    """Raised if a local PDF export cannot be created."""


def export_markdown(markdown: str, output_path: str | Path) -> Path:
    """Save canonical UTF-8 Markdown and return its path."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return path


def export_pdf(markdown: str, output_path: str | Path) -> Path:
    """Render simple Markdown headings, bullets, and paragraphs with ReportLab.

    This intentionally avoids browser engines and cloud conversion services, so
    the export works offline on all supported local deployments.
    """
    try:
        from reportlab.lib.colors import HexColor
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer
    except ImportError as exc:
        raise ExportError("reportlab is not installed. Run: pip install -r requirements.txt") from exc

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    project_models = Path(__file__).resolve().parents[1] / "models"
    regular_candidates = [
        project_models / "NotoSans-Regular.ttf",
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    bold_candidates = [
        project_models / "NotoSans-Bold.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    body_font = "Helvetica"
    bold_font = "Helvetica-Bold"
    regular_path = next((candidate for candidate in regular_candidates if candidate.is_file()), None)
    if regular_path:
        try:
            pdfmetrics.registerFont(TTFont("LectureSans", str(regular_path)))
            body_font = "LectureSans"
            bold_path = next((candidate for candidate in bold_candidates if candidate.is_file()), regular_path)
            pdfmetrics.registerFont(TTFont("LectureSansBold", str(bold_path)))
            bold_font = "LectureSansBold"
        except Exception:
            body_font = "Helvetica"
            bold_font = "Helvetica-Bold"
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "LectureBody",
        parent=styles["BodyText"],
        fontName=body_font,
        fontSize=9.7,
        leading=14,
        spaceAfter=6,
        alignment=TA_LEFT,
    )
    h1 = ParagraphStyle("LectureH1", parent=styles["Heading1"], fontName=bold_font, fontSize=17, leading=22, spaceBefore=15, spaceAfter=9, textColor=HexColor("#16213e"))
    h2 = ParagraphStyle("LectureH2", parent=styles["Heading2"], fontName=bold_font, fontSize=12.5, leading=17, spaceBefore=10, spaceAfter=6, textColor=HexColor("#0f4c5c"))
    bullet_style = ParagraphStyle("LectureBullet", parent=body, leftIndent=7 * mm, firstLineIndent=0, spaceAfter=2)

    def escaped(value: str) -> str:
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    story = []
    bullet_items: list[ListItem] = []

    def flush_bullets() -> None:
        nonlocal bullet_items
        if bullet_items:
            story.append(ListFlowable(bullet_items, bulletType="bullet", leftIndent=12 * mm, bulletFontSize=7))
            story.append(Spacer(1, 2))
            bullet_items = []

    for original_line in markdown.splitlines():
        line = original_line.strip()
        if not line:
            flush_bullets()
            story.append(Spacer(1, 3))
        elif line.startswith("# "):
            flush_bullets()
            story.append(Paragraph(escaped(line[2:]), h1))
        elif line.startswith("## "):
            flush_bullets()
            story.append(Paragraph(escaped(line[3:]), h2))
        elif re.match(r"^[-*]\s+", line):
            bullet_items.append(ListItem(Paragraph(escaped(re.sub(r"^[-*]\s+", "", line)), bullet_style)))
        else:
            flush_bullets()
            html = escaped(line)
            html = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html)
            story.append(Paragraph(html, body))
    flush_bullets()

    try:
        document = SimpleDocTemplate(
            str(path), pagesize=A4, rightMargin=17 * mm, leftMargin=17 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
            title="Lecture Notes",
        )
        document.build(story)
    except Exception as exc:
        raise ExportError(f"Could not create PDF export: {exc}") from exc
    return path
