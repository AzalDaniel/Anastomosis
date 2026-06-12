"""GUI web-asset tests — offline guarantee, parse sanity, packaging.

The dashboard's html/css/js ship bundled and must be network-free (the
archive's offline rule applies). These tests scan the assets for network
references, check the CSS parses enough (balanced braces), confirm
``index.html`` references only local files, that ``anastEvent`` is defined, and
that a built wheel actually contains ``gui/web`` (the registry.yaml precedent).
"""

from __future__ import annotations

import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "src" / "anastomosis" / "gui" / "web"
ASSETS = (
    "index.html",
    "tokens.css",
    "app.css",
    "app.js",
    "wizard.html",
    "wizard.js",
    "console.html",
    "console.js",
    "packgen.html",
    "packgen.js",
)

# The three new item-18/19 pages (html + their page JS).
NEW_PAGES = ("wizard.html", "console.html", "packgen.html")
NEW_SCRIPTS = ("wizard.js", "console.js", "packgen.js")

# The exact forbidden-substring set the archive's offline scan uses.
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


def test_tokens_defines_liquid_glass_custom_properties() -> None:
    text = (WEB / "tokens.css").read_text(encoding="utf-8")
    for prop in ("--glass-bg", "--glass-border", "--blur", "--accent", "--halo", "--ease"):
        assert f"{prop}:" in text, f"tokens.css missing {prop}"
    for radius in ("--radius-sm", "--radius-md", "--radius-lg"):
        assert f"{radius}:" in text, f"tokens.css missing {radius}"
    # Dark-first with a light fallback via prefers-color-scheme.
    assert "prefers-color-scheme: light" in text


def test_index_references_only_local_files() -> None:
    text = (WEB / "index.html").read_text(encoding="utf-8")
    hrefs = re.findall(r'(?:href|src)="([^"]+)"', text)
    assert hrefs, "index.html referenced no assets"
    for ref in hrefs:
        assert not ref.startswith(("http://", "https://", "//")), f"non-local ref {ref!r}"
        # Each referenced local asset must actually ship.
        assert (WEB / ref).is_file(), f"index.html references missing local file {ref!r}"


def test_anast_event_dispatcher_defined() -> None:
    text = (WEB / "app.js").read_text(encoding="utf-8")
    assert "window.anastEvent" in text
    # The plain-browser guard so opening in a browser does not throw.
    assert "pywebview" in text and "hasApi" in text


def test_index_has_strict_csp_meta() -> None:
    text = (WEB / "index.html").read_text(encoding="utf-8")
    assert "Content-Security-Policy" in text
    assert "default-src 'self'" in text
    assert "connect-src 'none'" in text  # zero network at read time


# --- the three new item-18/19 pages ---------------------------------------


@pytest.mark.parametrize("name", NEW_PAGES)
def test_new_page_has_strict_csp_meta(name: str) -> None:
    text = (WEB / name).read_text(encoding="utf-8")
    assert "Content-Security-Policy" in text
    assert "default-src 'self'" in text
    assert "script-src 'self'" in text
    assert "connect-src 'none'" in text  # zero network at read time


@pytest.mark.parametrize("name", NEW_PAGES)
def test_new_page_references_only_local_files(name: str) -> None:
    text = (WEB / name).read_text(encoding="utf-8")
    hrefs = re.findall(r'(?:href|src)="([^"]+)"', text)
    assert hrefs, f"{name} referenced no assets"
    for ref in hrefs:
        assert not ref.startswith(("http://", "https://", "//")), f"{name}: non-local ref {ref!r}"
        assert (WEB / ref).is_file(), f"{name} references missing local file {ref!r}"


@pytest.mark.parametrize("name", NEW_PAGES)
def test_new_page_links_shared_tokens_and_app_css(name: str) -> None:
    text = (WEB / name).read_text(encoding="utf-8")
    # All pages reuse the single source of truth (tokens.css) + the composition
    # layer (app.css) — no per-page stylesheet drift.
    assert 'href="tokens.css"' in text
    assert 'href="app.css"' in text


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


def test_wheel_contains_gui_web(tmp_path: Path) -> None:
    """Build a wheel and confirm gui/web/* ships (the registry.yaml check)."""
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
