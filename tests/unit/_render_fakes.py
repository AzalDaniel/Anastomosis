"""The stand-in renderer the pipeline unit tests share.

Chromium is not available in the unit lane, so every pipeline test substitutes a
fake renderer that writes a REAL PDF carrying the rendered text — QA then runs
for real against what was "rendered". Each test module used to spell that fake
out itself as one ``insert_textbox`` onto one Letter page, which was fine while
the only documents were single-visit charts.

It stopped being fine when a run began writing the whole-patient record summary
into every bundle. That document is far larger than one page, and
``insert_textbox`` does not clip when the text does not fit — it inserts
NOTHING and returns a negative number. A fake that silently drops what it was
handed would give QA a blank page and make the suite assert on a document the
real renderer would never produce; a stand-in that loses the content it was
given is the exact failure this project exists to prevent, so it lives here once
and spills onto as many pages as the text needs.

Synthetic by construction: the text comes from the repo's ``feedface-``
fixtures.
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
    """Lay the whole text out in one text box, the way the original fakes did.

    Returns False (leaving the page in place for the caller to drop) when the
    box could not hold it — ``insert_textbox`` reports that as a negative
    number and writes nothing at all.
    """
    import pymupdf

    page = doc.new_page(width=_LETTER[0], height=_LETTER[1])  # type: ignore[attr-defined]
    box = pymupdf.Rect(_MARGIN, _MARGIN, _LETTER[0] - _MARGIN, _LETTER[1] - _MARGIN)
    return bool(page.insert_textbox(box, text, fontsize=_FONT_SIZE) >= 0)


def _spill_across_pages(doc: object, text: str) -> None:
    """Place the text as wrapped lines over as many pages as it takes.

    Blank chunks are skipped rather than emitted: a trailing page of whitespace
    is a blank page, and QA fails blank pages — correctly, which is why the fake
    must not manufacture one.
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
