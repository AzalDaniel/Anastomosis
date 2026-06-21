"""The bundled-asset self-check (`anast doctor` / GUI doctor / the packaging CI hook).

These pin that every shipped data asset is discovered through the app's real
accessors, that a missing asset is DETECTED (not silently passed), and that the
bundled-Chromium check is REQUIRED in a frozen build but optional otherwise.
"""

from __future__ import annotations

import sys

from anastomosis.core import selfcheck
from anastomosis.core.selfcheck import check_bundled_assets, is_frozen

# Asset checks that never depend on an optional extra — all must pass in any
# healthy checkout/wheel/frozen build.
_REQUIRED = (
    "destinations registry",
    "tebra destination pack",
    "built-in packs",
    "HL7 C-CDA stylesheet",
    "GUI web assets",
    "GUI fonts",
    "learned-source synonyms",
    "archive web assets",
)


def test_is_frozen_false_from_source() -> None:
    assert is_frozen() is False


def test_all_bundled_assets_present_in_source_tree() -> None:
    result = check_bundled_assets()
    by_name = {c.name: c for c in result.checks}
    for name in _REQUIRED:
        assert name in by_name, f"missing check: {name}"
        assert by_name[name].ok, f"{name}: {by_name[name].detail}"
    # Chromium is OK whether present (found) or skipped (no render extra) — never
    # failed — in a healthy tree; the non-frozen run makes it optional.
    assert by_name["bundled Chromium"].ok
    assert result.ok


def test_check_detects_a_missing_asset(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Repoint one asset accessor at a nonexistent path → that check (and the
    aggregate) must FAIL (proves the self-check has teeth, not vacuous)."""
    import anastomosis.reconstruct.ccda_standard.renderer as renderer

    monkeypatch.setattr(renderer, "CDA_XSL", tmp_path / "nope" / "CDA.xsl")
    result = check_bundled_assets()
    cda = next(c for c in result.checks if c.name == "HL7 C-CDA stylesheet")
    assert cda.ok is False
    assert "missing" in cda.detail
    assert result.ok is False


def test_chromium_required_when_frozen_optional_otherwise(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Simulate the render extra being absent: skipped (ok) when not frozen,
    failed (required) when frozen — so a frozen build that didn't bundle
    Chromium fails the doctor instead of silently passing."""
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)  # force the import to fail
    assert selfcheck._check_chromium(frozen=False).ok is True
    assert selfcheck._check_chromium(frozen=True).ok is False
