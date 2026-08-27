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
        "--brand:        oklch(0.44 0.13 30)",
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
    for surface in ("--surface:", "--surface-row:", "--field:", "--field-focus:"):
        assert surface in text, f"tokens.css missing the {surface!r} content surface"
    # §3 glass is the chrome tiers only, near-opaque above ambient.
    assert "--glass-veil-blur:   saturate(130%) blur(24px)" in text
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
        ".counter-tile",
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
    # The open native dropdown is styled dark, not left to the OS default.
    assert ".field select option" in text
    assert "background-color: #241d18" in text
    # A textarea is styled with the inputs, not left as a native white box.
    assert ".field textarea" in text


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
    }
    for needle, what in gone.items():
        assert needle not in css, f"{what} came back ({needle!r})"
    # The pill radius survives in exactly two places: the segment toggle and the
    # status badge (§5).
    pill_users = [
        line.strip()
        for line in css.splitlines()
        if "var(--radius-pill)" in line and "--radius-pill:" not in line
    ]
    assert pill_users, "the pill radius vanished entirely"
    for line in pill_users:
        assert "radius-pill" in line
    # The glyph icon constants are gone from the shipped markup (§8).
    html = _read("index.html")
    for glyph in ("✓", "⚠", "✗"):
        assert glyph not in html, f"the {glyph!r} glyph icon is back in the markup"
    assert "const GLYPH" not in _read("shell.js"), "the glyph table is back"


def test_the_four_status_buckets_have_their_own_grid_cells() -> None:
    """The counter tiles no longer collide, and only Filed is green (§10.5)."""
    css = _read("app.css")
    areas = re.search(r"grid-template-areas:(.+?);", css, re.DOTALL)
    assert areas is not None, "the counter grid lost its template areas"
    named = set(re.findall(r"[a-z]+", areas.group(1)))
    assert named == {"filed", "attention", "progress", "waiting"}
    placed = dict(re.findall(r'\[data-bucket="(\w+)"\]\s*\{\s*grid-area:\s*(\w+);', css))
    assert placed == {
        "filed": "filed",
        "attention": "attention",
        "progress": "progress",
        "waiting": "waiting",
    }, f"two tiles share a cell: {placed}"
    assert '[data-bucket="filed"] .counter-value     { color: var(--ok); }' in css
    assert '[data-bucket="attention"] .counter-value { color: var(--stop); }' in css


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
        tag = re.search(rf'<section class="view" data-view="{view}"[^>]*>', text)
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
    # The in-page band carries the CURRENT VIEW, not the app name.
    assert '<span class="title-text" id="view-band">Charts</span>' in text
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
    assert "cell.title = state" in js, "the technical id must stay available on the tooltip"


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
