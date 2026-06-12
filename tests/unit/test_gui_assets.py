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
ASSETS = ("index.html", "tokens.css", "app.css", "app.js")

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
