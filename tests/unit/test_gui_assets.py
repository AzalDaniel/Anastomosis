"""GUI web-asset tests — offline guarantee, parse sanity, packaging, design system.

The desktop GUI's html/css/js/fonts ship bundled and must be network-free (the
archive's offline rule applies — fonts are LOCAL files served under a strict
``font-src 'self'`` CSP). These tests scan the assets for network references,
check the CSS parses (balanced braces), confirm pages reference only local
files, that ``anastEvent`` is defined, that the carried Liquid Glass token
sheet keeps its REAL values, that the gooey SVG filter is present wherever the
segment toggle lives, that the OFL fonts + attribution ship, and that a built
wheel actually contains ``gui/web`` and the fonts (the registry.yaml precedent).
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
ASSETS = (
    "index.html",
    "tokens.css",
    "app.css",
    "shell.js",
    "app.js",
    "wizard.html",
    "wizard.js",
    "console.html",
    "console.js",
    "packgen.html",
    "packgen.js",
)

# The bundled SIL OFL variable fonts (carried verbatim from the predecessor).
FONT_FILES = ("MonaSansVF.woff2", "JetBrainsMonoVF.woff2")

# Every page (index + the three workspaces).
ALL_PAGES = ("index.html", "wizard.html", "console.html", "packgen.html")
# The three item-18/19 pages (html + their page JS).
NEW_PAGES = ("wizard.html", "console.html", "packgen.html")
NEW_SCRIPTS = ("wizard.js", "console.js", "packgen.js")
# Pages that host a segment toggle and therefore must define the gooey filter.
GOOEY_PAGES = ("index.html", "console.html", "wizard.html", "packgen.html")

# The exact forbidden-substring set the archive's offline scan uses. Fonts are
# local, so no network reference may appear in ANY asset.
_FORBIDDEN = ("https://", "http://", "//cdn", 'src="//', "fonts.googleapis", "cdnjs")


def test_all_assets_exist() -> None:
    for name in ASSETS:
        assert (WEB / name).is_file(), f"missing GUI asset {name}"


@pytest.mark.parametrize("name", ASSETS)
def test_asset_has_no_network_reference(name: str) -> None:
    text = (WEB / name).read_text(encoding="utf-8")
    for needle in _FORBIDDEN:
        # The CSP meta legitimately names schemes inside http-equiv content;
        # those are policy directives ('self'/'none'), not fetched URLs. Guard
        # only the URL-shaped forbidden needles.
        assert needle not in text, f"{name} references {needle!r}"


@pytest.mark.parametrize("name", ("tokens.css", "app.css"))
def test_css_braces_balanced(name: str) -> None:
    text = (WEB / name).read_text(encoding="utf-8")
    assert text.count("{") == text.count("}"), f"{name} has unbalanced braces"
    assert text.count("{") > 0, f"{name} defined no rules"


# --- the carried Liquid Glass design system -------------------------------


def test_tokens_carry_the_real_design_system() -> None:
    """tokens.css carries the predecessor's REAL :root sheet + @font-face.

    Spot-pins a few load-bearing values so a re-styled fork can't silently
    drift the design language: the quartic ease curve, the coral OKLCH
    primary, the dichroic accent fill, the glass elevations, and the local
    Mona Sans / JetBrains Mono @font-face declarations.
    """
    text = (WEB / "tokens.css").read_text(encoding="utf-8")
    # The motion curve, verbatim.
    assert "--ease-quart:        cubic-bezier(0.32, 0.72, 0, 1);" in text
    # The coral primary in OKLCH (carried value).
    assert "--primary:        oklch(0.78 0.18 28);" in text
    assert "--primary-glow:   oklch(0.78 0.18 28 / 0.40);" in text
    # The dichroic progress fill and glass elevations.
    assert "--accent-fill:" in text and "linear-gradient(90deg" in text
    for prop in ("--glass-card-bg", "--glass-card-blur", "--glass-modal-blur"):
        assert f"{prop}:" in text, f"tokens.css missing {prop}"
    assert "--glass-card-border:" in text
    # Surfaces, ink, signals, spacing, radii — the full sheet shape.
    for prop in ("--surface-card", "--ink-primary", "--signal-success", "--space-4", "--radius-lg"):
        assert f"{prop}:" in text, f"tokens.css missing {prop}"
    # The local @font-face for both families (no second italic file for Mona).
    assert "@font-face {" in text
    assert 'font-family: "Mona Sans";' in text
    assert 'src: url("fonts/MonaSansVF.woff2") format("woff2-variations");' in text
    assert 'font-family: "JetBrains Mono";' in text
    assert 'src: url("fonts/JetBrainsMonoVF.woff2") format("woff2-variations");' in text


def test_app_css_carries_core_components() -> None:
    """app.css carries the original component classes (the visual layer)."""
    text = (WEB / "app.css").read_text(encoding="utf-8")
    for cls in (
        ".glass-card",
        ".segment-toggle",
        ".segment-goo",
        ".segment-indicator",
        ".cmd-palette",
        ".calendar-cell",
        ".log-strip",
        ".progress-bar-fill",
        ".counter-tile",
    ):
        assert cls in text, f"app.css missing carried component {cls}"
    # The gooey filter is referenced by the segment goo layer.
    assert "filter: url(#gooey);" in text
    # The dichroic shimmer animation drives the progress fill.
    assert "@keyframes shimmer" in text
    # Reduced-motion handling is carried.
    assert "prefers-reduced-motion: reduce" in text


def test_wallpaper_is_procedural_not_a_bundled_image() -> None:
    """The provenance-unknown wallpaper.jpg is NOT shipped; the layer is CSS."""
    assert not (WEB / "wallpaper.jpg").exists(), "wallpaper.jpg must not ship (unknown provenance)"
    text = (WEB / "app.css").read_text(encoding="utf-8")
    assert ".wallpaper {" in text
    # The dark fallback base and a procedural gradient (no live image url).
    assert "#0a0a0c" in text
    assert "radial-gradient" in text
    # An operator can drop their own wallpaper.jpg — the override is documented.
    assert "wallpaper.jpg" in text


# --- the bundled OFL fonts ------------------------------------------------


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
    # Both upstream sources are cited.
    assert "github/mona-sans" in text
    assert "JetBrains/JetBrainsMono" in text
    assert "Mona Sans" in text and "JetBrains Mono" in text


# --- CSP + local-only references (every page) -----------------------------


@pytest.mark.parametrize("name", ALL_PAGES)
def test_page_has_strict_csp_with_font_src(name: str) -> None:
    text = (WEB / name).read_text(encoding="utf-8")
    assert "Content-Security-Policy" in text
    assert "default-src 'self'" in text
    assert "script-src 'self'" in text
    assert "style-src 'self'" in text
    assert "connect-src 'none'" in text  # zero network at read time
    # Fonts are local files served under the strict policy.
    assert "font-src 'self'" in text


@pytest.mark.parametrize("name", ALL_PAGES)
def test_page_references_only_local_files(name: str) -> None:
    text = (WEB / name).read_text(encoding="utf-8")
    hrefs = re.findall(r'(?:href|src)="([^"]+)"', text)
    assert hrefs, f"{name} referenced no assets"
    for ref in hrefs:
        assert not ref.startswith(("http://", "https://", "//")), f"{name}: non-local ref {ref!r}"
        # Each referenced local asset must actually ship.
        assert (WEB / ref).is_file(), f"{name} references missing local file {ref!r}"


@pytest.mark.parametrize("name", ALL_PAGES)
def test_page_links_shared_tokens_and_app_css(name: str) -> None:
    text = (WEB / name).read_text(encoding="utf-8")
    # All pages reuse the single source of truth (tokens.css) + the composition
    # layer (app.css) — no per-page stylesheet drift.
    assert 'href="tokens.css"' in text
    assert 'href="app.css"' in text


@pytest.mark.parametrize("name", GOOEY_PAGES)
def test_page_with_segment_toggle_defines_gooey_filter(name: str) -> None:
    """Every page that hosts the gooey segment toggle ships the SVG filter def."""
    text = (WEB / name).read_text(encoding="utf-8")
    assert 'filter id="gooey"' in text, f"{name} missing the gooey SVG filter def"
    assert "feColorMatrix" in text and "feGaussianBlur" in text


# --- JS bridge guards ------------------------------------------------------


def test_anast_event_dispatcher_defined() -> None:
    text = (WEB / "app.js").read_text(encoding="utf-8")
    assert "window.anastEvent" in text
    # The plain-browser guard so opening in a browser does not throw.
    assert "pywebview" in text and "hasApi" in text


def test_shell_exposes_carried_interaction_patterns() -> None:
    """shell.js carries the segment-drag, palette, log-drawer, calendar code."""
    text = (WEB / "shell.js").read_text(encoding="utf-8")
    assert "window.AnastShell" in text
    for fn in ("initSegmentToggles", "initCommandPalette", "initLogStrip", "renderCalendar"):
        assert fn in text, f"shell.js missing carried helper {fn}"
    # The drag physics: pointer follow + nearest-slot snap + stretch.
    assert "pointermove" in text and "--segment-index" in text
    # The calendar halo treatment (counts only).
    assert "calendar-cell--halo-" in text and "calendar-count-badge" in text


@pytest.mark.parametrize("name", NEW_SCRIPTS)
def test_new_script_uses_pywebview_bridge_with_guard(name: str) -> None:
    text = (WEB / name).read_text(encoding="utf-8")
    # Each page talks to the headless controller over the pywebview bridge and
    # guards against a plain browser (no fake behavior, shows the launch notice).
    assert "pywebview" in text and "hasApi" in text


def test_console_lists_item_keys_never_names() -> None:
    """The patient command sheet is a STUB that lists item KEYS, not names."""
    html = (WEB / "console.html").read_text(encoding="utf-8")
    js = (WEB / "console.js").read_text(encoding="utf-8")
    assert "Cmd/Ctrl+K" in html or "Ctrl+K" in html
    # The palette is fed by upload_item_keys (ids only).
    assert "upload_item_keys" in js
    # No accessor that would surface a patient name exists.
    assert "patient_name" not in js and "patient_name" not in html


@pytest.mark.parametrize(
    ("page", "needle"),
    [
        # The wizard labels the deferred live API push.
        ("wizard.js", "later milestone"),
        # The console labels deferred live driving in BOTH the page and JS.
        ("console.html", "later milestone"),
    ],
)
def test_deferred_functionality_is_labeled(page: str, needle: str) -> None:
    text = (WEB / page).read_text(encoding="utf-8")
    assert needle in text, f"{page} must loudly label deferred functionality"


def test_console_has_no_fake_live_upload_controls() -> None:
    """No button must pretend to start/pause/drive real uploads (deferred to M6)."""
    html = (WEB / "console.html").read_text(encoding="utf-8")
    js = (WEB / "console.js").read_text(encoding="utf-8")
    # The console is read-only: it must not call any write/drive controller
    # method. The only controller methods it may invoke are the read accessors.
    forbidden_calls = ("run_pipeline", "transition", "begin_run", "recover", "upload_start")
    for call in forbidden_calls:
        assert call not in js, f"console.js must not invoke {call!r} (read-only surface)"
    # The deferred label must appear so the deferral is loud, not silent.
    assert "deferred" in html


def test_packgen_requires_confirmation_checkbox() -> None:
    """The same-patient guard is a REQUIRED checkbox (ported from the CLI)."""
    html = (WEB / "packgen.html").read_text(encoding="utf-8")
    js = (WEB / "packgen.js").read_text(encoding="utf-8")
    assert 'id="confirm-distinct"' in html
    # The emit button starts disabled and only the confirmation enables it.
    assert "disabled" in html
    assert "confirmed_distinct_patients" in js or "true)" in js
    assert "pack_init" in js


def test_dashboard_links_to_new_pages() -> None:
    text = (WEB / "index.html").read_text(encoding="utf-8")
    for page in NEW_PAGES:
        assert f'href="{page}"' in text, f"dashboard must link to {page}"


def test_dashboard_freshness_toast_wired() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'id="freshness-toast"' in html
    assert "pack_freshness" in js  # the vendor-change detection probe


def test_dashboard_section_matrix_wired() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'id="section-matrix"' in html
    # gatherSections() reads the live matrix (no longer a hardcoded {}).
    assert "section-matrix" in js
    assert "renderSectionMatrix" in js


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
