"""GUI web-asset tests — offline guarantee, one document, design system, packaging.

The desktop GUI's html/css/js/fonts ship bundled and must be network-free (the
archive's offline rule applies — fonts are LOCAL files served under a strict
``font-src 'self'`` CSP). These tests scan the shipped assets for network
references, check the CSS parses, confirm the app is ONE document with four
views (DESIGN_LANGUAGE §7/§9), pin the Porcelain & Oxblood token sheet to its
real values, hold the anti-slop ledger (§12) to what it removed, and confirm a
built wheel actually contains ``gui/web`` and the fonts (the registry.yaml
precedent).

Behaviour lives in ``tests/gui_e2e`` (the pages driven in a real browser); these
are the static guarantees that must hold before a browser is ever opened.
"""

from __future__ import annotations

import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "src" / "anastomosis" / "gui" / "web"
FONTS = WEB / "fonts"

#: The shipped assets: ONE document, the two stylesheets, the shell, and one
#: script per view (Teach hosts two modes, so two of them).
ASSETS = (
    "index.html",
    "tokens.css",
    "app.css",
    "shell.js",
    "app.js",
    "wizard.js",
    "console.js",
    "packgen.js",
    "source.js",
)

#: The per-view scripts and the event flow each one owns.
VIEW_SCRIPTS = (
    ("app.js", "pipeline"),
    ("wizard.js", "migration"),
    ("console.js", "upload"),
    ("packgen.js", "pack_init"),
    ("source.js", "source_init"),
)

#: The bundled SIL OFL variable fonts.
FONT_FILES = ("MonaSansVF.woff2", "JetBrainsMonoVF.woff2", "FrauncesVF.woff2")

#: The four views the single document ships.
VIEWS = ("charts", "migrate", "uploads", "teach")

# The exact forbidden-substring set the archive's offline scan uses. Fonts are
# local, so no network reference may appear in ANY asset — not even a namespace
# URL, which is why the icons are parsed from markup and the select chevron is
# drawn in CSS.
_FORBIDDEN = ("https://", "http://", "//cdn", 'src="//', "fonts.googleapis", "cdnjs")


def _read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def test_all_assets_exist() -> None:
    for name in ASSETS:
        assert (WEB / name).is_file(), f"missing GUI asset {name}"


def test_the_gui_is_exactly_one_document() -> None:
    """Five pages became four views inside one document (§7).

    The legacy pages are DELETED, not merely unlinked: a stray wizard.html would
    still be reachable, would re-race the bridge, and would re-parse everything
    the single document exists to parse once.
    """
    pages = sorted(path.name for path in WEB.glob("*.html"))
    assert pages == ["index.html"], f"the GUI must ship exactly one document, found {pages}"


@pytest.mark.parametrize("name", ASSETS)
def test_asset_has_no_network_reference(name: str) -> None:
    text = _read(name)
    for needle in _FORBIDDEN:
        # The CSP meta legitimately names schemes inside http-equiv content;
        # those are policy directives ('self'/'none'), not fetched URLs. Guard
        # only the URL-shaped forbidden needles.
        assert needle not in text, f"{name} references {needle!r}"


@pytest.mark.parametrize("name", ("tokens.css", "app.css"))
def test_css_braces_balanced(name: str) -> None:
    text = _read(name)
    assert text.count("{") == text.count("}"), f"{name} has unbalanced braces"
    assert text.count("{") > 0, f"{name} defined no rules"


# --- the design system (docs/design/DESIGN_LANGUAGE.md §1-§6) --------------


def test_tokens_carry_the_porcelain_and_oxblood_system() -> None:
    """tokens.css IS §1-§6, spot-pinned so a restyle cannot drift it silently."""
    text = _read("tokens.css")
    # §1 ground and ink: warm, never navy.
    for token in (
        "--ground:        oklch(0.16 0.013 50)",
        "--ground-deep:   oklch(0.125 0.012 50)",
        "--ink:           oklch(0.96 0.010 80)",
    ):
        assert token in text, f"tokens.css lost {token!r}"
    # The two values that carry a measured WCAG floor rather than a taste call.
    # --ink-muted below 0.72 puts every 12px caption on a panel under 4.5:1;
    # --brand-bright above 0.55 puts the porcelain label on the primary button
    # under it. Both were measured, both ways, on the shipped surfaces.
    assert "--ink-muted:     oklch(0.72 0.012 80)" in text
    assert "--brand-bright: oklch(0.55 0.15 30)" in text
    # §1 clinical signals are their own family.
    for signal in ("--ok:", "--attention:", "--stop:"):
        assert signal in text, f"tokens.css missing the {signal!r} signal"
    # Content surfaces are opaque steps of a luminance ladder, not glass.
    for surface in ("--surface:", "--field:", "--field-focus:"):
        assert surface in text, f"tokens.css missing the {surface!r} content surface"
    # §3 glass is the chrome tiers only, near-opaque above ambient.
    assert "--glass-blur:      saturate(150%) blur(24px)" in text
    assert "--glass-modal-blur:  saturate(170%) blur(48px)" in text
    assert "--glass-modal-bg:    rgba(24, 20, 16, 0.94)" in text, "the mud fix is gone"
    assert "--glass-card-bg" not in text, "the content glass tier came back"
    # §5 radius BY ROLE, not one token.
    for radius in ("--radius-panel:   14px", "--radius-control:  8px", "--radius-chip:     6px"):
        assert radius in text, f"tokens.css lost {radius!r}"
    # §6 four durations, one curve.
    assert "--ease-quart: cubic-bezier(0.32, 0.72, 0, 1)" in text
    for duration in ("--t-snap: 120ms", "--t-move: 240ms", "--t-soft: 480ms", "--t-fill: 800ms"):
        assert duration in text, f"tokens.css lost {duration!r}"
    # The dark form-control palette, so the OS-drawn select popup is not white.
    assert "color-scheme: dark" in text


def test_three_local_font_faces_never_block_text() -> None:
    """All three families are local, and none of them can cause a text flash."""
    text = _read("tokens.css")
    for family, file in (
        ("Mona Sans", "MonaSansVF.woff2"),
        ("JetBrains Mono", "JetBrainsMonoVF.woff2"),
        ("Fraunces", "FrauncesVF.woff2"),
    ):
        assert f'font-family: "{family}";' in text
        assert f'src: url("fonts/{file}") format("woff2-variations");' in text
    assert text.count("@font-face {") == 3
    assert text.count("font-display: fallback;") == 3, "a blocking face is a flash of no text"


def test_type_never_goes_below_twelve_px() -> None:
    """§4: nothing below 12px, ever — the smallest text carries audit facts."""
    small = [
        (name, size)
        for name in ("tokens.css", "app.css")
        for size in re.findall(r"font-size:\s*(\d+)px", _read(name))
        if int(size) < 12
    ]
    assert not small, f"type below the 12px floor: {small}"


def test_app_css_carries_the_components() -> None:
    text = _read("app.css")
    for cls in (
        ".panel",
        ".segment-toggle",
        ".segment-goo",
        ".segment-indicator",
        ".calendar-cell",
        ".log-strip",
        ".progress-bar-fill",
        ".value-n",
        ".empty-state",
        ".view--leaving",
        ".btn-primary",
        ".btn-secondary",
        ".btn-quiet",
    ):
        assert cls in text, f"app.css is missing {cls}"
    # The gooey filter is referenced by the segment's isolated goo layer.
    assert "filter: url(#gooey);" in text
    # Reduced motion zeroes every animation (§6).
    assert "prefers-reduced-motion: reduce" in text
    # The one-of-N picker is ours, so its popup is in the page and styled here
    # rather than drawn by the OS in colours we could only ask nicely for.
    for cls in (".chooser-trigger", ".chooser-list", ".chooser-row", ".chooser-note"):
        assert cls in text, f"app.css is missing {cls}"
    # A textarea is styled with the inputs, not left as a native white box.
    assert ".field textarea" in text


def test_the_design_language_names_every_token_and_no_others() -> None:
    """The doc and the sheet describe the same system, checked both ways.

    This test exists because the document went stale in exactly the way a
    document does: its §1 table still gave `--ground` as 0.18 and
    `--brand-bright` as 0.56 long after both moved, and its whole §2 rested on
    a backdrop layer that had been deleted. A prose file nothing checks becomes
    a second, wrong source of truth — and this one carries measured accessibility
    floors, so being wrong about it is not cosmetic.

    Both directions matter. A token the document does not mention is a
    decision nobody wrote down; a token the document names that the sheet does
    not define is advice for a system that no longer exists.
    """
    sheet = _read("tokens.css")
    doc = (Path(__file__).resolve().parents[2] / "docs/design/DESIGN_LANGUAGE.md").read_text(
        encoding="utf-8"
    )
    # A letter has to follow the dashes, or markdown's own `---` table rules
    # read as a token name.
    defined = set(re.findall(r"^\s+(--[a-z][a-z0-9-]*):", sheet, re.M))
    # The removal ledger's whole job is naming things that no longer exist, so
    # the "stale" direction stops where it starts.
    live = doc.split("## 12.", 1)[0]
    named = set(re.findall(r"(--[a-z][a-z0-9-]*)", live))

    # Groups the document describes by their rule rather than one row each —
    # naming six spacing steps and six type sizes twice would make the document
    # worse, and both groups ARE described (the 4px scale table, the type scale).
    by_rule = {token for token in defined if re.match(r"--(space|text|t-|radius|ease)", token)}
    # Values a component owns rather than the system: they appear in app.css at
    # the one place that uses them and are covered by the tier they belong to.
    owned = {
        "--fill",
        "--fill-soft",
        "--fill-strong",
        "--hairline",
        "--font-body",
        "--font-mono",
        "--font-editorial",
        "--glass-highlight",
        "--glass-shadow",
        "--shadow-float",
        "--measure-prose",
        "--glass-border",
        "--glass-bg",
        "--glass-blur",
        "--glass-modal-border",
    }

    unwritten = sorted(defined - named - by_rule - owned)
    assert not unwritten, f"tokens.css defines what the design language never mentions: {unwritten}"

    stale = sorted(named - defined)
    assert not stale, f"the design language names tokens that no longer exist: {stale}"

    # And the two measured floors are quoted correctly, since being wrong about
    # THESE is a shipped accessibility failure rather than a stale sentence.
    assert "--ink-muted` | `oklch(0.72 0.012 80)" in doc
    assert "--brand-bright` | `oklch(0.55 0.15 30)" in doc


def test_the_anti_slop_ledger_stays_removed() -> None:
    """§12: what was deleted must stay deleted."""
    css = _read("app.css")
    gone = {
        ".wallpaper {": "the gradient wallpaper layer",
        ".wallpaper-veil": "the second full-viewport gradient",
        "--accent-fill": "the dichroic progress gradient",
        "@keyframes shimmer": "the 8s progress shimmer",
        ".pill {": "the dead filter-bar component",
        ".cmd-palette": "the command palette",
        ".watermark": "the full-viewport decorative mark",
        ".status-badge": "the second pill, which nothing rendered",
        ".chip:focus-visible": "a focus ring for a component that does not exist",
        ".select-wrap": "the native dropdown's chevron wrapper",
        "background-color: #241d18": "the colours we asked the OS to paint its popup",
    }
    for needle, what in gone.items():
        assert needle not in css, f"{what} came back ({needle!r})"
    # The glyph icon constants are gone from the shipped markup (§8).
    html = _read("index.html")
    for glyph in ("✓", "⚠", "✗"):
        assert glyph not in html, f"the {glyph!r} glyph icon is back in the markup"
    assert "const GLYPH" not in _read("shell.js"), "the glyph table is back"


def _css_rules(css: str) -> list[tuple[str, str]]:
    """Every ``selector { body }`` pair in a stylesheet, comments stripped.

    Enough of a parser for these checks: the sheet has no nesting and no at-rule
    bodies containing braces other than the rules inside them, which this simply
    reads as rules of their own.
    """
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return [
        (selector.strip().split("{")[-1].strip(), body)
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", bare)
    ]


def test_only_the_view_nav_wears_the_pill() -> None:
    """A sliding pill means "peer destinations", and there is one such control.

    A binary setting is a switch and one-of-N is a chooser, so the pill radius
    is allowed on the nav (and its two internal layers), on the switch track and
    the progress bar — round-ended shapes, not pills — and nowhere else. The
    check this replaces gathered the same lines and then asserted they contained
    the string it had just filtered them by, so it passed on any selector.
    """
    allowed = {
        ".segment-toggle",
        ".segment-goo",
        ".segment-indicator",
        ".toggle .track",
        # The nav's own focus ring, shaped to the lozenge it hugs.
        ".navpill .segment-option",
        ".progress-bar",
        ".progress-bar-fill",
        ".log-strip",
    }
    users = {
        part.split(":")[0]
        for selector, body in _css_rules(_read("app.css"))
        if "var(--radius-pill)" in body
        for part in selector.split(",")
    }
    assert users, "the pill radius vanished entirely"
    assert users <= allowed, f"a second control wears the pill: {sorted(users - allowed)}"


def test_every_control_clears_the_hit_target_floor() -> None:
    """44px on the short axis, declared — not left to whatever the padding gives.

    Every one of these was measured under the floor in a real browser, nine of
    them under WCAG 2.5.8's 24px AA minimum. The floors are asserted here so a
    later padding change cannot quietly walk them back; the browser sweep in
    tests/gui_e2e proves they actually render.
    """
    css = _read("app.css")
    rules = dict(_css_rules(css))
    for selector in (
        ".btn",
        ".toggle",
        ".advanced > summary",
        ".route-detail summary",
        ".mode-tab",
        '.field :where(input[type="text"], input[type="search"], select, textarea)',
    ):
        assert selector in rules, f"{selector} is gone"
        assert "min-height: 44px;" in rules[selector], f"{selector} has no hit-target floor"
    assert "width: 44px; height: 44px;" in rules[".cal-nav"], "the calendar arrows shrank again"


def test_machine_shaped_fields_opt_in_to_the_mono_face() -> None:
    """Mono is for strings read character by character, and it is opt-in.

    The default is the body face, so a new field is safe by omission; a path or
    an identifier says so with a class. The base rule is wrapped in :where() on
    purpose — its attribute selectors would otherwise outrank a single class and
    the opt-in would be silently ignored, which is exactly what happened first.
    """
    css = _read("app.css")
    assert '.field :where(input[type="text"]' in css, "the base rule lost its :where()"
    assert ".field .is-path, .field .is-id, .field .is-endpoint {" in css
    html = _read("index.html")
    for field, face in (
        ("uploads-results-dir", "is-path"),
        ("uploads-assistant-folder", "is-path"),
        ("uploads-record", "is-path"),
        ("layout-samples", "is-path"),
        ("format-example", "is-path"),
        ("uploads-assistant", "is-id"),
        ("uploads-skiplist", "is-id"),
        ("uploads-search", "is-id"),
        ("layout-name", "is-id"),
        ("format-name", "is-id"),
        ("uploads-browser", "is-endpoint"),
    ):
        tag = re.search(rf'<(?:input|textarea)[^>]*\bid="{field}"', html)
        assert tag is not None, f"{field} is gone from the markup"
        assert f'class="{face}"' in tag.group(0), f"{field} is not wearing {face}"
    # A name a person composes stays in the body face.
    for field in ("layout-display", "format-display"):
        tag = re.search(rf'<input[^>]*\bid="{field}"', html)
        assert tag is not None and "class=" not in tag.group(0), f"{field} took a mono face"


def test_a_count_is_coloured_only_when_it_asks_for_something() -> None:
    """Colour is earned; success and zero are not states worth shouting about.

    This replaces a check on the counter grid's template areas, which existed
    because two tiles once shared a cell. There is no grid now — a value display
    is a number over its name with no container — so the collision it guarded
    against cannot happen, and what is worth guarding is the colour rule.
    """
    css = _read("app.css")
    coloured = dict(
        re.findall(r'\.value\[data-signal="(\w+)"\] \.value-n \{ color: var\((--\w+)\);', css)
    )
    assert coloured == {"attention": "--stop", "progress": "--attention"}, (
        f"a signal outside the attention family is colouring a number: {coloured}"
    )
    assert "--ok" not in css.split(".values {", 1)[1].split(".empty-state", 1)[0], (
        "success is being coloured — success is the ABSENCE of a coloured number"
    )
    zero = '.value[data-zero="true"] .value-n'
    assert f"{zero} {{ color: var(--ink-secondary); font-weight: 300; }}" in css

    # The uppercase carve-out is bounded to this one label and nothing else.
    upper = [selector for selector, body in _css_rules(css) if "text-transform: uppercase" in body]
    assert upper == [".value-k"], f"uppercase escaped the value label: {upper}"


# --- the single document ---------------------------------------------------


def test_index_has_strict_csp_with_font_src() -> None:
    text = _read("index.html")
    assert "Content-Security-Policy" in text
    for directive in (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "connect-src 'none'",
        "font-src 'self'",
    ):
        assert directive in text, f"index.html lost the {directive!r} directive"


def test_index_references_only_local_files_that_ship() -> None:
    text = _read("index.html")
    refs = re.findall(r'(?:href|src)="([^"]+)"', text)
    assert refs, "index.html referenced no assets"
    for ref in refs:
        assert not ref.startswith(("http://", "https://", "//")), f"non-local ref {ref!r}"
        assert (WEB / ref).is_file(), f"index.html references a missing local file {ref!r}"
    assert 'href="tokens.css"' in text and 'href="app.css"' in text


def test_index_ships_the_four_views_and_their_nav() -> None:
    text = _read("index.html")
    for view in VIEWS:
        assert f'data-view="{view}"' in text, f"the {view!r} view section is missing"
        assert f'data-view-target="{view}"' in text, f"the {view!r} nav button is missing"
    # Only the first view paints on load; the rest ship hidden, so the first
    # frame is the Charts skeleton rather than four stacked views.
    for view in VIEWS:
        tag = re.search(rf'<section class="view"[^>]*data-view="{view}"[^>]*>', text)
        assert tag is not None, f"the {view!r} section is not a plain view section"
        is_hidden = "hidden" in tag.group(0)
        assert is_hidden is (view != "charts"), (
            f"the {view!r} view ships {'hidden' if is_hidden else 'visible'}"
        )
    # Every script the document loads is loaded exactly once.
    for script in ("shell.js", *[name for name, _flow in VIEW_SCRIPTS]):
        assert text.count(f'src="{script}"') == 1, f"{script} is not loaded exactly once"


def test_the_gooey_filter_is_declared_once_for_the_whole_app() -> None:
    """The filter defs used to be re-declared (and re-registered) per page."""
    text = _read("index.html")
    assert text.count('filter id="gooey"') == 1
    assert "feColorMatrix" in text and "feGaussianBlur" in text


def test_the_app_name_appears_once_in_the_chrome() -> None:
    """§10.7: the product name is the window's, and the version lives in About."""
    text = _read("index.html")
    assert "<title>Anastomosis</title>" in text
    # The nav pill names the current view in the chrome layer, and each view's
    # own h1 names it in the content layer. The band that printed it a third
    # time is gone.
    assert 'class="title-bar"' not in text and 'id="view-band"' not in text
    assert 'class="segment-toggle navpill"' in text
    assert 'id="about-version"' in text and "AGPL-3.0" in text
    # No <h1> repeats the product name.
    assert not re.search(r"<h1>\s*Anastomosis\s*</h1>", text)


def test_nothing_navigates_the_document() -> None:
    """A view switch is a class toggle, never a document load (§7)."""
    for name in ASSETS:
        text = _read(name)
        assert "window.location" not in text, f"{name} navigates the document"
    html = _read("index.html")
    assert not re.search(r'href="[^"]*\.html"', html), "index.html links to a page"


# --- the JS seam -----------------------------------------------------------


def test_one_event_dispatcher_lives_in_the_shell() -> None:
    """One `window.anastEvent`, in shell.js, routing by flow (§7).

    Each page used to define its own, filtered to its own flow — which is what
    orphaned a run's event stream the moment the operator changed page.
    """
    shell = _read("shell.js")
    assert "window.anastEvent = function anastEvent" in shell
    assert "BY_FLOW[event.flow]" in shell, "the dispatcher no longer routes by flow"
    for script, _flow in VIEW_SCRIPTS:
        # An assignment, not a mention: the view headers may point at the shell's.
        assert "window.anastEvent =" not in _read(script), (
            f"{script} defines a second dispatcher — the shell owns the only one"
        )


@pytest.mark.parametrize(("script", "flow"), VIEW_SCRIPTS)
def test_each_view_registers_the_flow_it_owns(script: str, flow: str) -> None:
    """Flow scoping: the dispatcher hands an event to the ONE view that owns it.

    Two views emit identical stage/progress/done/error kinds (a Charts rebuild
    and a migration), so the flow registration is what stops one finishing the
    other's run.
    """
    text = _read(script)
    assert f'"{flow}"' in text, f"{script} does not name its {flow!r} flow"
    assert "registerView" in text or "registerFlow" in text, f"{script} registers nothing"


@pytest.mark.parametrize("script", [name for name, _flow in VIEW_SCRIPTS])
def test_view_script_uses_the_bridge_through_the_shell_guard(script: str) -> None:
    text = _read(script)
    assert "pywebview" in text and "hasApi" in text
    # The bridge bootstrap happens ONCE, in the shell: a view waits on it.
    assert "Shell.onReady" in text or "Shell.onInfo" in text


def test_the_run_form_is_built_once_and_composed_twice() -> None:
    """Charts and Migrate share one form component, not two implementations."""
    shell = _read("shell.js")
    assert "function buildRunForm(" in shell
    charts, migrate = _read("app.js"), _read("wizard.js")
    assert "Shell.buildRunForm(" in charts and "Shell.buildRunForm(" in migrate
    assert 'mode: "charts"' in charts and 'mode: "migrate"' in migrate
    # The field labels exist in exactly one place across every shipped asset.
    for label in ("Export folder", "Where results go", "Double-check results"):
        hits = sum(_read(name).count(label) for name in ASSETS)
        assert hits == 1, f"{label!r} is written {hits} times — the form was duplicated"


def test_uploads_drives_only_through_the_controller() -> None:
    """Filing is wired live, but ONLY through the controller seam.

    Driving is SAFE because the JS goes only through the controller — it must
    never reach into the upload record's write surface, so the controller stays
    the single owner of every write.
    """
    js = _read("console.js")
    html = _read("index.html")
    assert "upload_start" in js and "upload_stop" in js
    assert "upload_safety_notice" in js, "console.js must fetch the safety warning"
    assert 'id="uploads-safety"' in html, "the safety warning has nowhere to land"
    for call in ("run_pipeline", "transition", "begin_run", "recover"):
        assert call not in js, f"console.js must not invoke {call!r} (the controller owns writes)"


def test_uploads_double_check_ships_on_and_reaches_the_drive_call() -> None:
    """The verification ladder is ON by default; unchecking is the opt-out."""
    html = _read("index.html")
    checkbox = re.search(r'<input[^>]*id="uploads-verify"[^>]*>', html)
    assert checkbox is not None and "checked" in checkbox.group(0)
    js = _read("console.js")
    assert "uploads-verify" in js and "verify" in js


def test_uploads_replaced_the_palette_with_a_visible_search() -> None:
    """The one real use of the ⌘K palette became a field an operator can see."""
    html = _read("index.html")
    js = _read("console.js")
    assert 'id="uploads-search"' in html
    assert "upload_item_keys" in js, "the search is fed by the controller's id list"
    # No accessor that would surface a patient name exists.
    assert "patient_name" not in js and "patient_name" not in html


def test_uploads_states_render_as_plain_english_with_the_id_on_the_tooltip() -> None:
    """Raw snake_case state ids were unreadable as prose to a clinician."""
    js = _read("console.js")
    for state, label in (
        ("pre_verify_failed", "Stopped before filing"),
        ("duplicate_at_destination", "Already in the destination"),
        ("skipped_skiplist", "Skipped at your request"),
    ):
        assert state in js and label in js, f"{state!r} lost its plain label"
    assert "row.title = state" in js, "the technical id must stay available on the tooltip"


def test_teach_hosts_both_modes_behind_a_required_confirmation() -> None:
    """Two mirrored wizards became two modes of one view, gate intact."""
    html = _read("index.html")
    assert 'data-mode="layout"' in html and 'data-mode="format"' in html
    assert 'id="layout-confirm"' in html and 'id="format-confirm"' in html
    # Both write buttons ship disabled; only the confirmation arms them.
    for button in ("layout-write", "format-save"):
        markup = re.search(rf'<button[^>]*id="{button}"[^>]*>', html)
        assert markup is not None and "disabled" in markup.group(0)
    layout, fmt = _read("packgen.js"), _read("source.js")
    assert "api.pack_init_async(" in layout and "api.last_pack_result(" in layout
    assert "api.source_init_async(" in fmt and "api.last_source_result(" in fmt
    # The synchronous variants would block the bridge thread; they are not used.
    assert "api.source_init(" not in fmt and "api.pack_init(" not in layout
    for call in ("run_pipeline", "begin_run", "transition", "recover"):
        assert call not in fmt and call not in layout


def test_charts_keeps_its_freshness_notice_and_section_matrix() -> None:
    html = _read("index.html")
    js = _read("app.js")
    assert 'id="freshness-toast"' in html
    assert "pack_freshness" in js  # the vendor-change probe
    assert "renderSectionMatrix" in _read("shell.js")
    assert "setSections" in js, "the layout's sections must repaint when it changes"


def test_the_handoff_carries_context_from_migrate_to_uploads() -> None:
    """The wizard used to end by telling the operator to retype what it knew."""
    html = _read("index.html")
    migrate, uploads = _read("wizard.js"), _read("console.js")
    assert 'id="migrate-continue"' in html
    assert 'Shell.showView("uploads"' in migrate
    assert "uploads-assistant" in uploads
    # The handoff travels ONLY as showView's context. It used to be written to a
    # shell-global as well, which Uploads read on every arrival — so the offer
    # never expired and kept reverting fields the operator had retyped since.
    for source, name in ((migrate, "wizard.js"), (uploads, "console.js")):
        assert "Shell.store(" not in source, f"{name} brought the handoff global back"


# --- the bundled OFL fonts -------------------------------------------------


@pytest.mark.parametrize("name", FONT_FILES)
def test_font_present_and_is_woff2(name: str) -> None:
    path = FONTS / name
    assert path.is_file(), f"missing bundled font {name}"
    # WOFF2 magic number is 'wOF2'.
    assert path.read_bytes()[:4] == b"wOF2", f"{name} is not a WOFF2 file"


def test_fonts_ship_ofl_attribution_readme() -> None:
    readme = FONTS / "README.md"
    assert readme.is_file(), "fonts/README.md (OFL attribution) is missing"
    text = readme.read_text(encoding="utf-8")
    assert "OFL" in text and "Open Font License" in text
    # Every bundled upstream is cited.
    assert "github/mona-sans" in text
    assert "JetBrains/JetBrainsMono" in text
    assert "Fraunces" in text
    assert "Mona Sans" in text and "JetBrains Mono" in text


# --- packaging -------------------------------------------------------------


def test_wheel_contains_gui_web_and_fonts(tmp_path: Path) -> None:
    """Build a wheel and confirm gui/web/* AND the fonts ship (registry.yaml check)."""
    repo_root = Path(__file__).resolve().parents[2]
    pytest.importorskip("build", reason="wheel build needs the 'build' package")
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path), str(repo_root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    wheels = list(tmp_path.glob("*.whl"))
    assert wheels, "no wheel was built"
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
    for asset in ASSETS:
        assert any(n.endswith(f"gui/web/{asset}") for n in names), (
            f"wheel is missing gui/web/{asset}"
        )
    for font in FONT_FILES:
        assert any(n.endswith(f"gui/web/fonts/{font}") for n in names), (
            f"wheel is missing gui/web/fonts/{font}"
        )
