"""Tests for core.textutil — cell hygiene and note-HTML extraction."""

import pytest

from anastomosis.core.textutil import clean_cell, clean_numeric, format_phone, html_to_text

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


def test_html_to_text_plain_text_passthrough() -> None:
    assert html_to_text("Just a plain sentence.") == "Just a plain sentence."
    assert html_to_text("") is None
    assert html_to_text(None) is None
