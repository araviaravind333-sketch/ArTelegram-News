"""Styled Word (.docx) briefing generator.

Produces a landscape A4 document with a cover block, a scan summary, and one
colour-coded table per category. Every table row carries the six fields the
newsroom brief requires: hook, core facts, virality score, drivers, CTA and
source link.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

import config
from analyzer.virality_engine import ScoredItem, window_label

log = logging.getLogger(__name__)

# --- Palette ---------------------------------------------------------------
BRAND_NAVY = RGBColor(0x0B, 0x1F, 0x3A)
BRAND_RED = RGBColor(0xC0, 0x1B, 0x2E)
INK = RGBColor(0x1A, 0x1A, 0x1A)
MUTED = RGBColor(0x5A, 0x5A, 0x5A)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

HEADER_FILL = "0B1F3A"
ZEBRA_FILL = "F4F6F9"
CATEGORY_FILL = "C01B2E"

SCORE_COLOURS = (
    (85, RGBColor(0x0F, 0x7B, 0x3C)),   # 85+  scorching
    (70, RGBColor(0x1E, 0x88, 0x5E)),   # 70+  strong
    (55, RGBColor(0xB8, 0x6E, 0x00)),   # 55+  workable
    (0,  RGBColor(0x8A, 0x8A, 0x8A)),   # rest filler
)

#: Instagram-fit verdict -> text colour, for the 4th column.
FIT_COLOURS = {
    "YES": RGBColor(0x0F, 0x7B, 0x3C),
    "MAYBE": RGBColor(0xB8, 0x6E, 0x00),
    "NO": RGBColor(0xA6, 0x1B, 0x1B),
}
FIT_ICONS = {"YES": "✅", "MAYBE": "➡", "NO": "❌"}

#: Four columns, exactly as requested: headline, link, score, Instagram fit.
COLUMNS = (
    ("#", 0.35),
    ("News Headline", 4.10),
    ("News Link", 1.55),
    ("Score", 0.65),
    ("Fits Your Instagram? (based on account insights & past reels)", 4.15),
)


# ---------------------------------------------------------------------------
# Low-level docx helpers
# ---------------------------------------------------------------------------

def _shade(cell, hex_fill: str) -> None:
    """Apply a solid background fill to a table cell."""
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), hex_fill)
    cell._tc.get_or_add_tcPr().append(shading)


def _cell_margins(table, top=60, bottom=60, left=90, right=90) -> None:
    """Set uniform cell padding (values are twentieths of a point)."""
    tbl_pr = table._tbl.tblPr
    margins = OxmlElement("w:tblCellMar")
    for tag, value in (("top", top), ("left", left), ("bottom", bottom), ("right", right)):
        node = OxmlElement(f"w:{tag}")
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")
        margins.append(node)
    tbl_pr.append(margins)


def _repeat_header(row) -> None:
    """Mark a table row as a header that repeats on every page."""
    tr_pr = row._tr.get_or_add_trPr()
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "true")
    tr_pr.append(header)


def _hyperlink(paragraph, url: str, text: str) -> None:
    """Insert a real clickable hyperlink (python-docx has no native helper)."""
    part = paragraph.part
    r_id = part.relate_to(
        url,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)

    run = OxmlElement("w:r")
    run_pr = OxmlElement("w:rPr")

    colour = OxmlElement("w:color")
    colour.set(qn("w:val"), "1155CC")
    run_pr.append(colour)

    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    run_pr.append(underline)

    size = OxmlElement("w:sz")
    size.set(qn("w:val"), "16")  # half-points -> 8pt
    run_pr.append(size)

    run.append(run_pr)
    text_node = OxmlElement("w:t")
    text_node.text = text
    run.append(text_node)
    link.append(run)
    paragraph._p.append(link)


def _write(cell, text: str, *, size=8.5, bold=False, colour=INK,
           align=WD_ALIGN_PARAGRAPH.LEFT, italic=False):
    """Replace a cell's content with a single styled run."""
    cell.text = ""
    paragraph = cell.paragraphs[0]
    paragraph.alignment = align
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.space_before = Pt(0)
    run = paragraph.add_run(text)
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = colour
    run.font.name = "Calibri"
    return paragraph


def _score_colour(score: int) -> RGBColor:
    for threshold, colour in SCORE_COLOURS:
        if score >= threshold:
            return colour
    return MUTED


# ---------------------------------------------------------------------------
# Document sections
# ---------------------------------------------------------------------------

def _configure_page(document: Document) -> None:
    section = document.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = Inches(11.69), Inches(8.27)
    section.left_margin = section.right_margin = Inches(0.4)
    section.top_margin = section.bottom_margin = Inches(0.45)

    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(9)

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = footer.add_run(
        "Aravind News 24 — automated virality briefing. "
        "Verify every fact against the source link before publishing."
    )
    run.font.size = Pt(7.5)
    run.font.color.rgb = MUTED


def _cover(document: Document, start: datetime, end: datetime,
           generated_at: datetime, total_items: int, sources: int) -> None:
    title = document.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.LEFT
    title.paragraph_format.space_after = Pt(2)
    run = title.add_run("ARAVIND NEWS 24 — DAILY VIRALITY BRIEFING")
    run.font.size = Pt(22)
    run.font.bold = True
    run.font.color.rgb = BRAND_NAVY

    subtitle = document.add_paragraph()
    subtitle.paragraph_format.space_after = Pt(10)
    run = subtitle.add_run(
        f"Scan window: {window_label(start, end)}   •   "
        f"Generated: {generated_at.astimezone(config.IST).strftime('%d %b %Y, %I:%M %p IST')}   •   "
        f"{total_items} verified stories across {len(config.CATEGORIES)} categories   •   "
        f"{sources} free sources monitored"
    )
    run.font.size = Pt(9.5)
    run.font.color.rgb = MUTED

    legend = document.add_paragraph()
    legend.paragraph_format.space_after = Pt(12)
    run = legend.add_run(
        "Score key:  85-100 scorching  •  70-84 strong  •  55-69 workable  •  "
        "below 55 backup.    Fit column:  ✅ YES = supported by this account's "
        "own reel history or a strong score where no history exists yet  •  "
        "➡ MAYBE = close to your average, worth trying  •  "
        "❌ NO = this account's reels in that category have underperformed, "
        "or the story is too weak to risk without history. "
        "Fit is recalculated daily from data/benchmarks.json as your reels post."
    )
    run.font.size = Pt(8)
    run.font.italic = True
    run.font.color.rgb = MUTED


def _category_heading(document: Document, index: int, name: str, count: int) -> None:
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(10)
    paragraph.paragraph_format.space_after = Pt(4)
    run = paragraph.add_run(f"{index}. {name.upper()}")
    run.font.size = Pt(13)
    run.font.bold = True
    run.font.color.rgb = BRAND_RED

    note = paragraph.add_run(f"    ({count} items)")
    note.font.size = Pt(8.5)
    note.font.bold = False
    note.font.color.rgb = MUTED


def _category_table(document: Document, entries: list[ScoredItem]) -> None:
    table = document.add_table(rows=1, cols=len(COLUMNS))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    _cell_margins(table)

    header = table.rows[0]
    _repeat_header(header)
    for cell, (label, width) in zip(header.cells, COLUMNS):
        cell.width = Inches(width)
        _shade(cell, HEADER_FILL)
        _write(cell, label, size=8.5, bold=True, colour=WHITE,
               align=WD_ALIGN_PARAGRAPH.CENTER)

    for position, entry in enumerate(entries, start=1):
        row = table.add_row()
        cells = row.cells
        for cell, (_, width) in zip(cells, COLUMNS):
            cell.width = Inches(width)
        if position % 2 == 0:
            for cell in cells:
                _shade(cell, ZEBRA_FILL)

        # 1. #
        _write(cells[0], str(position), size=8.5, bold=True,
               align=WD_ALIGN_PARAGRAPH.CENTER, colour=MUTED)

        # 2. News Headline - the original, plain journalistic headline
        #    (not the reel hook - this column is for editorial reference).
        _write(cells[1], entry.item.title, size=9, bold=True, colour=BRAND_NAVY)

        # 3. News Link - publisher + timestamp, clickable through to the
        #    actual source URL.
        cells[2].text = ""
        link_para = cells[2].paragraphs[0]
        link_para.paragraph_format.space_after = Pt(0)
        run = link_para.add_run(
            f"{entry.publisher}\n{entry.published_ist.strftime('%d %b, %I:%M %p')}\n"
        )
        run.font.size = Pt(7.5)
        run.font.color.rgb = MUTED
        _hyperlink(link_para, entry.link, "Open article")

        # 4. Score
        _write(cells[3], str(entry.score), size=13, bold=True,
               colour=_score_colour(entry.score), align=WD_ALIGN_PARAGRAPH.CENTER)

        # 5. Instagram fit - verdict (coloured, icon-led) + one-line reason,
        #    derived from this account's own reel history where it exists.
        cells[4].text = ""
        fit_para = cells[4].paragraphs[0]
        fit_para.paragraph_format.space_after = Pt(0)
        verdict_run = fit_para.add_run(
            f"{FIT_ICONS.get(entry.fit_verdict, '')} {entry.fit_verdict}\n"
        )
        verdict_run.font.size = Pt(9)
        verdict_run.font.bold = True
        verdict_run.font.color.rgb = FIT_COLOURS.get(entry.fit_verdict, INK)
        reason_run = fit_para.add_run(entry.fit_reason)
        reason_run.font.size = Pt(8)
        reason_run.font.color.rgb = MUTED

    # Per-table scoring footnote keeps the audit trail visible to the editor.
    note = document.add_paragraph()
    note.paragraph_format.space_before = Pt(2)
    note.paragraph_format.space_after = Pt(2)
    drivers = ", ".join(
        f"#{i} {e.rationale}" for i, e in enumerate(entries[:3], start=1) if e.rationale
    )
    run = note.add_run(f"Top scoring drivers — {drivers}" if drivers else "")
    run.font.size = Pt(7)
    run.font.italic = True
    run.font.color.rgb = MUTED


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_filename(end: datetime) -> str:
    stamp = end.astimezone(config.IST).strftime("%Y-%m-%d_%H%M")
    return f"AravindNews24_Virality_Briefing_{stamp}.docx"


def generate(
    briefing: dict[str, list[ScoredItem]],
    start: datetime,
    end: datetime,
    output_path: Path | None = None,
    source_count: int = 0,
) -> Path:
    """Render the briefing to a .docx and return the written path."""
    config.ensure_directories()
    generated_at = datetime.now(config.UTC)
    total = sum(len(v) for v in briefing.values())

    document = Document()
    _configure_page(document)
    _cover(document, start, end, generated_at, total, source_count)

    for index, category in enumerate(config.CATEGORIES, start=1):
        entries = briefing.get(category, [])
        _category_heading(document, index, category, len(entries))
        if entries:
            _category_table(document, entries)
        else:
            paragraph = document.add_paragraph()
            run = paragraph.add_run(
                "No stories cleared verification in this window. "
                "Widen the scan window or re-run after the next news cycle."
            )
            run.font.size = Pt(9)
            run.font.italic = True
            run.font.color.rgb = MUTED

    path = output_path or (config.OUTPUT_DIR / build_filename(end))
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(path))
    log.info("Briefing written: %s (%d items)", path, total)
    return path
