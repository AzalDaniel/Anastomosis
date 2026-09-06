"""Bundled-asset self-check, one source of truth for the CLI, the GUI and
CI (``anast doctor``): every shipped data asset is present, non-empty and
readable, resolved through the SAME accessor the app uses at runtime so a
path the freezer failed to bundle is caught. PHI: a check's ``detail`` is
a count/"ok"/"skipped"/exception TYPE name, never patient-derived. The
bundled-Chromium check is required in a frozen build, optional otherwise.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = ["AssetCheck", "SelfCheckResult", "check_bundled_assets", "is_frozen"]


def is_frozen() -> bool:
    """True when running from a frozen build (Nuitka ``__compiled__`` or ``sys.frozen``)."""
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


@dataclass(frozen=True)
class AssetCheck:
    """One asset's verdict: present-and-readable, or not, with a PHI-free detail."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class SelfCheckResult:
    """The full self-check: a list of per-asset checks; ``ok`` iff all passed."""

    checks: list[AssetCheck]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


def _readable(path: Path) -> bool:
    """A path is a non-empty, readable regular file."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _check_registry() -> AssetCheck:
    try:
        from anastomosis.destinations.registry import DestinationRegistry

        DestinationRegistry.load()  # reads the bundled registry.yaml (importlib.resources)
    except Exception as exc:
        return AssetCheck("destinations registry", False, type(exc).__name__)
    return AssetCheck("destinations registry", True, "loaded")


def _check_tebra_pack() -> AssetCheck:
    """The BUNDLED tebra browser-pack scaffold, specifically: resolved by
    package path and loaded directly, NOT through user-pack-override
    precedence, so a user-supplied ``tebra`` pack can never mask a missing
    built-in."""
    try:
        from importlib.resources import files

        from anastomosis.destinations.loader import _PACK_FILE, load_destination_pack

        builtin_dir = Path(str(files("anastomosis.destinations") / "tebra"))
        if not _readable(builtin_dir / _PACK_FILE):
            return AssetCheck("tebra destination pack", False, "bundled pack.yaml missing")
        # Point the loader straight at the bundled directory so its defensive
        # parsing still validates the manifest, but precedence cannot let a user
        # pack stand in for the built-in one.
        load_destination_pack("tebra", [builtin_dir])
    except Exception as exc:
        return AssetCheck("tebra destination pack", False, type(exc).__name__)
    return AssetCheck("tebra destination pack", True, "loaded")


def _check_jinja_packs() -> AssetCheck:
    try:
        from anastomosis.reconstruct import discover_packs

        # Bundled-asset health, so the shipped directory is the only one asked.
        # An operator's own taught layout may legitimately shadow a built-in
        # name, and that is not this check's business — nor may it turn a
        # healthy install into a reported missing asset.
        packs = discover_packs(include_user=False)
        missing = []
        for name in ("generic_soap", "practice_fusion_soap"):
            status = packs.get(name)
            if status is None or not status.available or status.pack is None:
                missing.append(name)
            elif not _readable(status.pack.template_path):
                missing.append(f"{name}/template")
    except Exception as exc:
        return AssetCheck("built-in packs", False, type(exc).__name__)
    if missing:
        return AssetCheck("built-in packs", False, f"missing: {', '.join(missing)}")
    return AssetCheck("built-in packs", True, "generic_soap, practice_fusion_soap")


def _check_ccda_stylesheet() -> AssetCheck:
    try:
        from anastomosis.reconstruct.ccda_standard.renderer import CDA_XSL

        vendor = CDA_XSL.parent
        files = (CDA_XSL, vendor / "cda_l10n.xml", vendor / "cda_narrativeblock.xml")
        missing = [p.name for p in files if not _readable(p)]
    except Exception as exc:
        return AssetCheck("HL7 C-CDA stylesheet", False, type(exc).__name__)
    if missing:
        return AssetCheck("HL7 C-CDA stylesheet", False, f"missing: {', '.join(missing)}")
    return AssetCheck("HL7 C-CDA stylesheet", True, "CDA.xsl + l10n + narrativeblock")


def _check_gui_web() -> AssetCheck:
    try:
        from anastomosis.gui.shell import _WEB_DIR

        pages = (
            # The single-document shell plus every view script it loads once —
            # a freezer that drops one script leaves that view dead inside a
            # window that otherwise opens, so each is checked by name.
            "index.html",
            "app.js",
            "shell.js",
            "wizard.js",
            "console.js",
            "packgen.js",
            "source.js",
            "app.css",
            "tokens.css",
        )
        missing = [name for name in pages if not _readable(_WEB_DIR / name)]
    except Exception as exc:
        return AssetCheck("GUI web assets", False, type(exc).__name__)
    if missing:
        return AssetCheck("GUI web assets", False, f"missing: {', '.join(missing)}")
    return AssetCheck("GUI web assets", True, f"{len(pages)} files")


def _check_fonts() -> AssetCheck:
    try:
        from anastomosis.gui.shell import _WEB_DIR

        # Every face the GUI ships; a face missing from this tuple never
        # fails doctor and shows up only as a silent fallback in the app.
        fonts = ("FrauncesVF.woff2", "MonaSansVF.woff2", "JetBrainsMonoVF.woff2")
        bad = []
        for name in fonts:
            path = _WEB_DIR / "fonts" / name
            if not _readable(path) or path.read_bytes()[:4] != b"wOF2":
                bad.append(name)
    except Exception as exc:
        return AssetCheck("GUI fonts", False, type(exc).__name__)
    if bad:
        return AssetCheck("GUI fonts", False, f"missing/invalid: {', '.join(bad)}")
    return AssetCheck("GUI fonts", True, "Fraunces + Mona Sans + JetBrains Mono (woff2)")


def _check_synonyms() -> AssetCheck:
    try:
        import json

        from anastomosis.core.sourcelearn import _SYNONYMS_PATH

        if not _readable(_SYNONYMS_PATH):
            return AssetCheck("learned-source synonyms", False, "missing")
        data = json.loads(_SYNONYMS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data:
            return AssetCheck("learned-source synonyms", False, "empty/invalid json")
    except Exception as exc:
        return AssetCheck("learned-source synonyms", False, type(exc).__name__)
    return AssetCheck("learned-source synonyms", True, f"{len(data)} field group(s)")


def _check_archive_assets() -> AssetCheck:
    try:
        from anastomosis.deliver.archive.archive import _ASSETS_DIR

        files = ("anast-index.js", "anast.css")
        missing = [name for name in files if not _readable(_ASSETS_DIR / name)]
    except Exception as exc:
        return AssetCheck("archive web assets", False, type(exc).__name__)
    if missing:
        return AssetCheck("archive web assets", False, f"missing: {', '.join(missing)}")
    return AssetCheck("archive web assets", True, "anast-index.js + anast.css")


def _check_chromium(*, frozen: bool) -> AssetCheck:
    """The bundled Playwright Chromium: resolves ``executable_path``
    without launching the browser. Required in a frozen build; skipped
    cleanly in a minimal install lacking the render extra."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        if frozen:
            return AssetCheck(
                "bundled Chromium", False, f"playwright import failed: {type(exc).__name__}"
            )
        return AssetCheck("bundled Chromium", True, "skipped (render extra not installed)")
    try:
        pw = sync_playwright().start()
        try:
            exe = Path(pw.chromium.executable_path)
        finally:
            pw.stop()
    except Exception as exc:
        return AssetCheck("bundled Chromium", False, type(exc).__name__)
    if not exe.is_file():
        return AssetCheck("bundled Chromium", False, "executable not found at resolved path")
    return AssetCheck("bundled Chromium", True, "found")


def check_bundled_assets() -> SelfCheckResult:
    """Run every bundled-asset check and return the aggregate verdict."""
    frozen = is_frozen()
    return SelfCheckResult(
        checks=[
            _check_registry(),
            _check_tebra_pack(),
            _check_jinja_packs(),
            _check_ccda_stylesheet(),
            _check_gui_web(),
            _check_fonts(),
            _check_synonyms(),
            _check_archive_assets(),
            _check_chromium(frozen=frozen),
        ]
    )
