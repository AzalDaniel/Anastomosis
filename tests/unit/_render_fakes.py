"""The stand-in renderer the pipeline unit tests share.

Chromium is not available in the unit lane, so every pipeline test
substitutes a fake renderer that writes a REAL PDF carrying the rendered
text — QA then runs for real against it. ``insert_textbox`` writes
NOTHING when the text does not fit, so this spills a large document onto
as many pages as it needs rather than silently handing QA a blank one.

Synthetic by construction: text from the repo's ``feedface-`` fixtures.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

# US Letter in PDF points — what the packs and the whole-patient view declare,
# and what QA's layout check measures against.
_LETTER = (612.0, 792.0)
_MARGIN = 18.0
_FONT_SIZE = 7
#: Wrapped-line budget for a spilled page: 110 characters is comfortably inside
#: the text width at this font, and 88 lines clear the bottom margin.
_WRAP_COLUMNS = 110
_LINES_PER_PAGE = 88


def _fits_on_one_page(doc: object, text: str) -> bool:
    """Lay the whole text out in one text box. Returns False (leaving the
    page in place for the caller to drop) when the box could not hold it
    — ``insert_textbox`` reports that as a negative number and writes
    nothing at all."""
    import pymupdf

    page = doc.new_page(width=_LETTER[0], height=_LETTER[1])  # type: ignore[attr-defined]
    box = pymupdf.Rect(_MARGIN, _MARGIN, _LETTER[0] - _MARGIN, _LETTER[1] - _MARGIN)
    return bool(page.insert_textbox(box, text, fontsize=_FONT_SIZE) >= 0)


def _spill_across_pages(doc: object, text: str) -> None:
    """Place the text as wrapped lines over as many pages as it takes.
    Blank chunks are skipped rather than emitted: a trailing page of
    whitespace is a blank page, and QA correctly fails blank pages, so
    the fake must not manufacture one.
    """
    lines = [
        wrapped
        for line in text.splitlines()
        for wrapped in (textwrap.wrap(line, _WRAP_COLUMNS) or [""])
    ]
    for start in range(0, max(len(lines), 1), _LINES_PER_PAGE):
        chunk = lines[start : start + _LINES_PER_PAGE]
        if not "".join(chunk).strip():
            continue
        page = doc.new_page(width=_LETTER[0], height=_LETTER[1])  # type: ignore[attr-defined]
        page.insert_text((_MARGIN, _MARGIN + 6), "\n".join(chunk), fontsize=_FONT_SIZE)


def write_text_pdf(html: str, pdf_path: Path) -> None:
    """Write ``html``'s text into a real Letter-geometry PDF at ``pdf_path``."""
    import pymupdf

    from anastomosis.core.textutil import html_to_text

    text = html_to_text(html) or "(empty)"
    doc = pymupdf.open()
    if not _fits_on_one_page(doc, text):
        doc.delete_page(0)
        _spill_across_pages(doc, text)
    doc.save(str(pdf_path))
    doc.close()


class FakeChromium:
    """Stands in for Chromium: a real PDF carrying the rendered text."""

    def __init__(self, **kwargs: object) -> None:
        pass

    def render(self, html: str, pdf_path: Path) -> None:
        write_text_pdf(html, pdf_path)

    def close(self) -> None:
        pass
