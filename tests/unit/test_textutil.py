"""Tests for core.textutil — cell hygiene, note-HTML extraction, safe names."""

from pathlib import Path

import pytest

from anastomosis.core.textutil import (
    MAX_NAME_CHARS,
    MAX_PATH_CHARS,
    budgeted_name,
    clean_cell,
    clean_numeric,
    format_phone,
    html_to_text,
    safe_name,
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


# --- filesystem names -------------------------------------------------------
#
# Synthetic ids only (``feedface-`` prefixes), and every value here is a made-up
# identifier, never a patient value.

_SYNTHETIC_ID = "feedface-0000-0000-0000-0000000000aa"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (_SYNTHETIC_ID, _SYNTHETIC_ID),
        ("Ada Q Fixture", "Ada_Q_Fixture"),
        ("../../etc/passwd", "etc_passwd"),
        ("", "fallback"),
        (None, "fallback"),
        ("///", "fallback"),
    ],
)
def test_safe_name_short_values_pass_through_unchanged(raw: str | None, expected: str) -> None:
    # PIN: the delivered layouts (chart filenames, archive/bundle patient dirs,
    # C-CDA filenames) are built from these components. Every real-length value
    # must come back byte-identical to what it was before the length bound
    # existed — a change here renames files an operator already holds.
    assert safe_name(raw, "fallback") == expected


def test_safe_name_caps_an_unbounded_value() -> None:
    # A 300-char component is rejected by NAME_MAX (POSIX) and by MAX_PATH
    # (Windows) — the delivered chart simply fails to write.
    capped = safe_name("A" * 300, "unknown")
    assert len(capped) == MAX_NAME_CHARS
    assert capped.startswith("A" * 100)


def test_safe_name_caps_a_long_fallback_too() -> None:
    # The bound is a property of what safe_name RETURNS, not of one branch.
    assert len(safe_name(None, "f" * 300)) == MAX_NAME_CHARS


def test_safe_name_long_values_stay_distinct_past_the_cap() -> None:
    # Two ids that differ ONLY past the cap must not collapse onto one name:
    # a collision here would file one patient's chart on top of another's.
    base = "feedface-" + "a" * 300
    first = safe_name(base + "-one", "unknown")
    second = safe_name(base + "-two", "unknown")
    assert first != second
    assert first[:-9] == second[:-9]  # same visible prefix, different hash tag
    assert len(first) == len(second) == MAX_NAME_CHARS


def test_safe_name_cut_is_deterministic() -> None:
    # A re-run must allocate the same name (the idempotent-skip contract).
    value = "feedface-" + "b" * 400
    assert safe_name(value, "unknown") == safe_name(value, "unknown")


def test_budgeted_name_passes_through_when_the_path_fits(tmp_path: Path) -> None:
    assert budgeted_name(_SYNTHETIC_ID, "unknown", parent=tmp_path) == _SYNTHETIC_ID
    assert budgeted_name(_SYNTHETIC_ID, "unknown", parent=tmp_path, suffix=".html") == _SYNTHETIC_ID


def test_budgeted_name_shortens_for_a_deep_parent(tmp_path: Path) -> None:
    parent = tmp_path / ("d" * 120)
    name = budgeted_name("feedface-" + "c" * 300, "unknown", parent=parent, suffix=".html")
    full = parent / f"{name}.html"
    assert len(str(full)) <= MAX_PATH_CHARS
    assert len(name) < MAX_NAME_CHARS  # cut further than the component cap
    (parent).mkdir(parents=True)
    full.write_text("x", encoding="utf-8")  # and the write actually lands
    assert full.is_file()


def test_budgeted_name_stays_distinct_when_shortened(tmp_path: Path) -> None:
    parent = tmp_path / ("d" * 120)
    base = "feedface-" + "d" * 300
    first = budgeted_name(base + "-one", "unknown", parent=parent, suffix=".html")
    second = budgeted_name(base + "-two", "unknown", parent=parent, suffix=".html")
    assert first != second


def test_budgeted_name_refuses_when_no_distinct_name_fits(tmp_path: Path) -> None:
    # Fail loudly rather than hand back a name that could collide.
    parent = tmp_path / ("d" * 230)
    with pytest.raises(ValueError, match="path budget"):
        budgeted_name(_SYNTHETIC_ID, "unknown", parent=parent, suffix=".html")


def test_budgeted_name_message_carries_no_path(tmp_path: Path) -> None:
    # PHI: an output directory can be named after a patient, so the message
    # reports lengths only — never the path itself.
    parent = tmp_path / ("d" * 230)
    with pytest.raises(ValueError) as excinfo:
        budgeted_name(_SYNTHETIC_ID, "unknown", parent=parent, suffix=".html")
    assert str(parent) not in str(excinfo.value)
    assert "d" * 20 not in str(excinfo.value)


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
    # A crafted sample of every repair sanitize_soap_html makes: TSV-exported
    # \n inside inline content → <br>, empty filler blocks stripped, wrapped
    # once in pf-rich-text.
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
    # No tags (no "<"): escape entities, then turn newlines into <br>.
    out = sanitize_soap_html("Tylenol & rest\\nRTC if worse")
    assert out == "Tylenol &amp; rest<br>RTC if worse"


def test_sanitize_soap_html_empty_inputs() -> None:
    assert sanitize_soap_html(None) == ""
    assert sanitize_soap_html("") == ""


def test_sanitize_soap_html_idempotent_wrap() -> None:
    # Already wrapped → not double-wrapped (idempotent-wrap guard).
    wrapped = '<div class="pf-rich-text"><p>Note.</p></div>'
    assert sanitize_soap_html(wrapped).count("pf-rich-text") == 1


# --- sanitize_soap_html allowlist (XSS / unsafe-markup defense) ------------
#
# These exercise the allowlist sanitizer the SOAP render path puts between
# source HTML and Chromium's renderer. The render path templates the section
# with Jinja ``| safe`` (autoescape off, by design — legitimate inline
# formatting must survive), so nothing else stops a stored ``<script>`` from
# executing inside the local PDF renderer. Every assertion follows the same
# pattern: the dangerous shape is gone, and a benign neighbour proves the
# sanitizer is not just deleting everything.


def test_sanitize_soap_html_drops_script_tag_and_body() -> None:
    out = sanitize_soap_html("<p>Before</p><script>alert('x')</script><p>After</p>")
    # Tag and body both gone (alert text must not become visible chart text).
    assert "<script" not in out
    assert "</script" not in out
    assert "alert" not in out
    # Benign neighbours survive.
    assert "<p>Before</p>" in out
    assert "<p>After</p>" in out


def test_sanitize_soap_html_drops_style_tag_and_body() -> None:
    out = sanitize_soap_html("<p>Note</p><style>@import url('http://evil/x.css');</style>")
    assert "<style" not in out
    assert "@import" not in out
    assert "evil" not in out
    assert "<p>Note</p>" in out


def test_sanitize_soap_html_strips_event_handler_attrs() -> None:
    out = sanitize_soap_html('<p onclick="alert(1)" onmouseover="x()">Click</p>')
    # The <p> tag survives — paragraph IS in the allowlist; only the
    # event handlers are stripped (the surrounding text remains).
    assert "onclick" not in out.lower()
    assert "onmouseover" not in out.lower()
    assert "alert" not in out.lower()
    assert ">Click</p>" in out


def test_sanitize_soap_html_drops_iframe() -> None:
    out = sanitize_soap_html("<p>x</p><iframe src='http://evil/'></iframe><p>y</p>")
    assert "<iframe" not in out
    assert "evil" not in out
    assert "<p>x</p>" in out and "<p>y</p>" in out


def test_sanitize_soap_html_drops_object_and_embed() -> None:
    out = sanitize_soap_html(
        '<p>before</p><object data="evil.swf"></object><embed src="evil"><p>after</p>'
    )
    assert "<object" not in out
    assert "<embed" not in out
    assert "evil" not in out
    assert "<p>before</p>" in out and "<p>after</p>" in out


def test_sanitize_soap_html_drops_img_with_onerror() -> None:
    # <img> is not in the allowlist (clinical SOAP fixture has none) — the
    # whole tag goes, taking the onerror handler with it.
    out = sanitize_soap_html('<p>x</p><img src="x" onerror="alert(1)"><p>y</p>')
    assert "<img" not in out
    assert "onerror" not in out
    assert "alert" not in out
    assert "<p>x</p>" in out and "<p>y</p>" in out


def test_sanitize_soap_html_drops_anchor_javascript_url() -> None:
    # <a> is not in the allowlist either; the surrounding text (the link
    # label "click") survives because we drop the tag but keep its data.
    out = sanitize_soap_html('<p>Visit <a href="javascript:alert(1)">click</a> now</p>')
    assert "<a " not in out and "<a>" not in out
    assert "javascript:" not in out
    assert "alert" not in out
    assert "Visit " in out and "click" in out and " now" in out


def test_sanitize_soap_html_drops_comments() -> None:
    # Comments are a classic mXSS bypass vector (parser disagreement between
    # HTMLParser and Chromium on ``<!--<script-->``).
    out = sanitize_soap_html("<p>x</p><!--<script>alert(1)</script>--><p>y</p>")
    assert "<!--" not in out
    assert "alert" not in out
    assert "<p>x</p>" in out and "<p>y</p>" in out


def test_sanitize_soap_html_drops_form_controls() -> None:
    out = sanitize_soap_html(
        '<p>x</p><form action="javascript:alert(1)">'
        '<input name="x"><button>go</button></form><p>y</p>'
    )
    assert "<form" not in out
    assert "<input" not in out
    assert "<button" not in out
    assert "javascript:" not in out
    assert "<p>x</p>" in out and "<p>y</p>" in out


def test_sanitize_soap_html_uppercase_handlers_normalized() -> None:
    # HTMLParser lowercases tag names; the handler-name comparison must also
    # be case-insensitive — verify with all-uppercase input.
    out = sanitize_soap_html('<P ONCLICK="alert(1)">X</P>')
    assert "onclick" not in out.lower()
    assert "alert" not in out.lower()
    assert ">X</p>" in out


def test_sanitize_soap_html_meta_link_dropped() -> None:
    out = sanitize_soap_html(
        '<meta http-equiv="refresh" content="0;url=http://evil"><link rel="stylesheet" href="http://evil/x"><p>note</p>'
    )
    assert "<meta" not in out
    assert "<link" not in out
    assert "evil" not in out
    assert "<p>note</p>" in out


def test_sanitize_soap_html_svg_dropped() -> None:
    # SVG can host <script> too; drop the whole subtree.
    out = sanitize_soap_html("<p>x</p><svg><script>alert(1)</script></svg><p>y</p>")
    assert "<svg" not in out
    assert "<script" not in out
    assert "alert" not in out
    assert "<p>x</p>" in out and "<p>y</p>" in out


def test_sanitize_soap_html_unknown_tag_drops_tag_keeps_text() -> None:
    # Graceful degradation: unrecognized wrapper disappears but its text
    # remains, so an old export with an obscure tag still surfaces content.
    out = sanitize_soap_html("<p>Refer to <font color='red'>RED</font> band</p>")
    assert "<font" not in out
    assert "color" not in out
    assert "RED" in out
    assert "Refer to " in out and " band" in out


def test_sanitize_soap_html_drops_style_attr() -> None:
    # ``style`` carries no semantic the templates need and is an
    # expression()/url(javascript:) carrier; the allowlist refuses it.
    out = sanitize_soap_html('<p style="background:url(javascript:alert(1))">x</p>')
    assert "style=" not in out
    assert "javascript:" not in out
    assert ">x</p>" in out


def test_sanitize_soap_html_preserves_class_attr() -> None:
    # ``class`` IS allowed — the templates style on it (``pf-rich-text`` etc).
    out = sanitize_soap_html('<p class="note-emphasis">hi</p>')
    assert 'class="note-emphasis"' in out
    assert "<p" in out and ">hi</p>" in out


def test_sanitize_soap_html_multiline_open_tag_does_not_corrupt() -> None:
    # Pretty-printed EHR exports sometimes wrap a long attribute list across
    # lines: ``<p\n  class="foo"\n>x</p>``. The source-bytes-preservation
    # shortcut must NOT emit that verbatim — the downstream stray-newline
    # regex would otherwise inject ``<br>`` inside the open tag, producing
    # ``<p<br>\n  class="foo"<br>\n>`` and silently dropping the class.
    # Reconstruction collapses the open tag to a clean single line.
    out = sanitize_soap_html('<p\n  class="foo"\n>x</p>')
    # The dangerous corruption shape MUST be absent.
    assert "<p<br>" not in out
    assert "<br>\n  " not in out
    # And the class survives intact.
    assert 'class="foo"' in out
    assert ">x</p>" in out
    # Same for a multi-line open tag inside a table cell (the polymerase
    # leading-strand's table-cell probe).
    out = sanitize_soap_html('<table><tr><td\n  class="narrow">A</td></tr></table>')
    assert "<td<br>" not in out
    assert 'class="narrow"' in out
    assert ">A</td>" in out


def test_sanitize_soap_html_byte_identical_on_fixture_shape() -> None:
    # Pins the byte-identity invariant the e2e goldens rely on: every shape
    # the PF/Tebra v9 fixture carries (paragraphs with text, paragraphs with
    # ``<br/>``, the ``\\n``-escaped TSV form) round-trips with no surprise
    # rewrites from the allowlist pass.
    shapes = [
        "<p>Reports good medication adherence. No dizziness or headache.</p>",
        "<p>Continue lisinopril 10 mg daily.<br/>Recheck in 3 months.</p>",
        "<p>Step 1.\\nStep 2.\\nStep 3.</p>",
    ]
    for shape in shapes:
        out = sanitize_soap_html(shape)
        # Wrap added once; the inner content is preserved verbatim
        # (including the <br/> self-closing form).
        assert out.startswith('<div class="pf-rich-text">')
        assert out.endswith("</div>")
        # The fixture's bytes survive, modulo the wrap.
        inner = out[len('<div class="pf-rich-text">') : -len("</div>")]
        # \\n in the test source means literal two-char "\n" → the
        # legacy transform turns it into a real newline, then into <br>.
        expected_inner = shape.replace("\\n", "<br>\n")
        assert inner == expected_inner, f"{inner!r} != {expected_inner!r}"
