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


def test_tebra_check_targets_the_bundled_pack_not_a_user_override(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The tebra check must reflect the BUNDLED pack, not a user
    override: pointing the bundled-pack anchor at a directory with NO
    ``pack.yaml`` (a hidden / un-bundled built-in), while a valid USER
    pack of the same name exists, must FAIL — a precedence-respecting
    lookup that let the user pack mask the missing built-in would not."""
    from importlib.resources import files as real_files

    # A USER-dir tebra pack that WOULD satisfy the loader by precedence.
    user_pack = tmp_path / "user" / "destinations" / "tebra"
    user_pack.mkdir(parents=True)
    (user_pack / "pack.yaml").write_text("name: tebra\n", encoding="utf-8")
    monkeypatch.setattr(
        "anastomosis.destinations.loader.user_destinations_dir",
        lambda: tmp_path / "user" / "destinations",
    )

    # A bundled-pack root that ships NO ``tebra/`` (the built-in was not shipped).
    empty_builtin_root = tmp_path / "builtin-empty"
    empty_builtin_root.mkdir()

    def fake_files(pkg: str):  # type: ignore[no-untyped-def]
        if pkg == "anastomosis.destinations":
            return empty_builtin_root
        return real_files(pkg)

    monkeypatch.setattr("importlib.resources.files", fake_files)
    check = selfcheck._check_tebra_pack()
    assert check.ok is False, check.detail
    assert "missing" in check.detail


def test_tebra_check_passes_against_the_real_bundled_pack() -> None:
    """Sanity: against the shipped tree the bundled tebra pack resolves and loads."""
    check = selfcheck._check_tebra_pack()
    assert check.ok is True, check.detail
