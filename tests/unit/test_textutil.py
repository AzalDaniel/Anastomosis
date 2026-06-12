"""Tests for core.textutil — cell hygiene and note-HTML extraction."""

import pytest

from anastomosis.core.textutil import (
    clean_cell,
    clean_numeric,
    format_phone,
    html_to_text,
    sanitize_soap_html,
)

# --- cell hygiene -----------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "   ", r"\N", " \\N ", "NULL", "null"])
def test_clean_cell_sentinels(raw: str | None) -> None:
    assert clean_cell(raw) is None


def test_clean_cell_strips_but_preserves_content() -> None:
    assert clean_cell("  Lisinopril 10mg  ") == "Lisinopril 10mg"
    # A field whose *content* mentions null-ish words is not a sentinel.
    assert clean_cell("null pointer noted in device log") is not None


@pytest.mark.parametrize("raw", ["-1", "-1.0", r"\N", ""])
def test_clean_numeric_sentinels(raw: str) -> None:
    assert clean_numeric(raw) is None


@pytest.mark.parametrize("raw", ["0", "98.6", "-2", "120/80"])
def test_clean_numeric_keeps_real_values(raw: str) -> None:
    assert clean_numeric(raw) == raw


# --- phones -----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw", ["2065550123", "206-555-0123", "(206) 555-0123", "1-206-555-0123", "+1 206 555 0123"]
)
def test_format_phone_normalizes_ten_digits(raw: str) -> None:
    assert format_phone(raw) == "(206) 555-0123"


def test_format_phone_preserves_partials() -> None:
    # Losing a partial number would violate the lossless guarantee.
    assert format_phone("  555-0123 ") == "555-0123"
    assert format_phone("") is None
    assert format_phone(None) is None


# --- note HTML --------------------------------------------------------------


def test_html_to_text_paragraph_structure() -> None:
    html = "<p>Patient reports improvement.</p><p>Continue current plan.</p>"
    assert html_to_text(html) == "Patient reports improvement.\n\nContinue current plan."


def test_html_to_text_br_and_entities() -> None:
    assert html_to_text("BP stable<br/>HR &amp; rhythm regular") == "BP stable\nHR & rhythm regular"


def test_html_to_text_lists_and_inline_markup() -> None:
    html = "<ul><li><b>Aspirin</b> 81mg</li><li>Atorvastatin 40mg</li></ul>"
    assert html_to_text(html) == "Aspirin 81mg\nAtorvastatin 40mg"


def test_html_to_text_drops_script_and_style() -> None:
    html = "<style>p{color:red}</style><p>Visible</p><script>alert(1)</script>"
    assert html_to_text(html) == "Visible"


def test_html_to_text_collapses_source_whitespace() -> None:
    html = "<p>Line\n   one</p>\n\n\n<p>Line two</p>"
    assert html_to_text(html) == "Line one\n\nLine two"


def test_html_to_text_table_cells_never_fuse() -> None:
    # Fused cells ("height64in") hide values from boundary-anchored QA.
    html = (
        "<table><tr><th>Measure</th><th>Value</th></tr>"
        "<tr><td>Body height</td><td>64</td><td>in</td></tr></table>"
    )
    assert html_to_text(html) == "Measure Value\nBody height 64 in"


def test_html_to_text_plain_text_passthrough() -> None:
    assert html_to_text("Just a plain sentence.") == "Just a plain sentence."
    assert html_to_text("") is None
    assert html_to_text(None) is None


# --- sanitize_soap_html (the rich-HTML rendering path) ----------------------


def test_sanitize_soap_html_repairs_ragged_export() -> None:
    # A crafted sample of every repair the predecessor's sanitize_soap_html
    # makes (gpdfs:137): TSV-exported \n inside inline content → <br>, empty
    # filler blocks stripped, wrapped once in pf-rich-text.
    raw = "<p>Injection sites:\\n1. Left deltoid\\n2. Right deltoid</p><p>&nbsp;</p><div></div>"
    out = sanitize_soap_html(raw)
    assert out.startswith('<div class="pf-rich-text">')
    assert out.endswith("</div>")
    # The stray \n inside the <p> became <br> (inline line breaks survive).
    assert "1. Left deltoid<br>" in out
    assert "2. Right deltoid" in out
    # Empty <p>&nbsp;</p> and empty <div></div> filler blocks are gone.
    assert "&nbsp;" not in out
    assert "<div></div>" not in out
    # Wrapped exactly once.
    assert out.count("pf-rich-text") == 1


def test_sanitize_soap_html_plain_text_escapes_and_breaks() -> None:
    # No tags (no "<"): escape entities, then turn newlines into <br> (gpdfs:146).
    out = sanitize_soap_html("Tylenol & rest\\nRTC if worse")
    assert out == "Tylenol &amp; rest<br>RTC if worse"


def test_sanitize_soap_html_empty_inputs() -> None:
    assert sanitize_soap_html(None) == ""
    assert sanitize_soap_html("") == ""


def test_sanitize_soap_html_idempotent_wrap() -> None:
    # Already wrapped → not double-wrapped (gpdfs:158 guard).
    wrapped = '<div class="pf-rich-text"><p>Note.</p></div>'
    assert sanitize_soap_html(wrapped).count("pf-rich-text") == 1
