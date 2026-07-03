"""Enforce package import boundaries that the architecture relies on.

These tests are quick property checks against ``sys.modules``: they import
a frontend package in isolation (subprocess, so the test runner's already-
loaded modules don't bias the result) and assert that no forbidden module
ended up loaded.

Today's only boundary: the GUI must not depend on the CLI. The two are
peer frontends over a shared core; a GUI-to-CLI dependency is a one-way
ratchet toward a CLI-shaped GUI, and the lazy CLI import that used to live
at ``gui/controller.py:_attach_destination`` was the symptom Codex Finding
#2 flagged. The shared browser-attach code now lives in
``anastomosis.deliver.browser.attach``; both frontends import it directly.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


def _modules_after_import(target: str) -> set[str]:
    """Import ``target`` in a clean subprocess and return ``sys.modules``."""
    script = textwrap.dedent(f"""
        import sys
        import {target}  # noqa: F401
        # Emit one module name per line on stdout; the parent splits and sets it.
        for name in sorted(sys.modules):
            print(name)
    """)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return set(proc.stdout.split())


def test_gui_does_not_import_cli() -> None:
    """``anastomosis.gui`` must not pull ``anastomosis.cli`` (or its private
    helpers) into ``sys.modules`` at import time. The bridge for the
    upload-attach seam is :mod:`anastomosis.deliver.browser.attach` —
    importing the GUI must NEVER transitively load the CLI.
    """
    loaded = _modules_after_import("anastomosis.gui")
    forbidden = {"anastomosis.cli"}
    leaked = forbidden & loaded
    assert not leaked, (
        f"anastomosis.gui leaked CLI dependency into sys.modules: {sorted(leaked)}. "
        "The GUI must use anastomosis.deliver.browser.attach.attach_destination "
        "instead of any CLI-private helper."
    )


def test_gui_does_not_import_cli_commands() -> None:
    """``anastomosis.gui`` must not pull any ``anastomosis.cli_commands`` module
    into ``sys.modules`` either. Each command group (the 0.4.0 CLI facade split)
    imports ``anastomosis.cli`` at its top for the Typer app objects, so a
    GUI-to-cli_commands edge would drag the whole CLI in — the same peer-frontend
    boundary :func:`test_gui_does_not_import_cli` guards, one layer down.
    """
    loaded = _modules_after_import("anastomosis.gui")
    leaked = {name for name in loaded if name.startswith("anastomosis.cli_commands")}
    assert not leaked, (
        f"anastomosis.gui leaked cli_commands modules into sys.modules: {sorted(leaked)}. "
        "The GUI must not import any CLI command group (they import anastomosis.cli)."
    )


def test_browser_attach_module_loads_without_playwright_extra() -> None:
    """The attach module is a thin shell — importing the module must not
    require the optional ``deliver-browser`` extra. The Playwright imports
    live INSIDE :func:`attach_destination` so installing without the extra
    keeps the CLI and GUI loadable.
    """
    loaded = _modules_after_import("anastomosis.deliver.browser.attach")
    # Importing the module alone must not pull playwright in.
    assert "playwright" not in loaded
    assert "playwright.sync_api" not in loaded


def test_cli_make_destination_aliases_attach_destination() -> None:
    """Long-standing tests monkeypatch ``anastomosis.cli._make_destination``;
    PR-P kept the alias so that contract still holds. The alias must point
    at the canonical :func:`attach_destination` (not a stale re-implementation).
    """
    from anastomosis.cli import _make_destination
    from anastomosis.deliver.browser.attach import attach_destination

    assert _make_destination is attach_destination


# --- public verification imports (Codex re-audit P1 regression) ------------
#
# A circular import between ``deliver.verify.composite`` and
# ``deliver.browser.reports`` snuck past PR-Q's full-suite run because the
# test runner's import order happened to load the modules in a safe sequence
# first. A FRESH process can hit the cycle and fail; the subprocess tests
# below run each public import in its own interpreter so the cycle cannot
# be masked by prior imports of the test suite.


def test_layered_verifier_public_import_in_fresh_process() -> None:
    """``from anastomosis.deliver.verify import LayeredVerifier`` must succeed
    in a clean interpreter — no circular import between
    :mod:`.verify.composite` and :mod:`.browser.reports`.
    """
    loaded = _modules_after_import("anastomosis.deliver.verify")
    assert "anastomosis.deliver.verify.composite" in loaded
    # Best-effort sanity: the leaf types module is loaded too.
    assert "anastomosis.deliver.verify.types" in loaded


def test_level_coverage_imports_from_both_sites() -> None:
    """:class:`LevelCoverage` is published in two places for back-compat:
    its canonical home (:mod:`.verify.types` — a leaf module) and re-export
    from :mod:`.verify.composite`. Both imports must succeed in a fresh
    process and resolve to the SAME class object (the alias-identity contract
    that downstream consumers like the upload reporter rely on).
    """
    script = textwrap.dedent("""
        from anastomosis.deliver.verify.composite import LevelCoverage as A
        from anastomosis.deliver.verify.types import LevelCoverage as B
        assert A is B, f"LevelCoverage drift: composite={A!r} types={B!r}"
        print("ok")
    """)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert proc.stdout.strip() == "ok"


def test_browser_reports_does_not_directly_import_verify_composite() -> None:
    """The fix-the-cycle constraint: ``browser.reports`` MUST NOT *directly*
    import from :mod:`.verify.composite`. Codex's re-audit caught the cycle
    that emerged when this rule was broken — ``verify.composite`` imports
    ``browser.errors`` which (via ``browser/__init__.py``) re-enters
    ``.reports``, and a direct import of ``LevelCoverage`` from
    ``verify.composite`` then resolves against a partially-initialized
    module.

    Static check on the source text: a module-level
    ``from anastomosis.deliver.verify.composite import ...`` line in
    ``reports.py`` is the regression signal. The canonical home of
    :class:`LevelCoverage` is :mod:`.verify.types` (a leaf module) and the
    report writer must reach it that way.
    """
    from pathlib import Path

    reports_src = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "anastomosis"
        / "deliver"
        / "browser"
        / "reports.py"
    ).read_text(encoding="utf-8")
    assert "from anastomosis.deliver.verify.composite" not in reports_src, (
        "browser/reports.py reintroduced a direct import from verify.composite. "
        "That's the cycle Codex's re-audit caught. Import LevelCoverage from "
        "anastomosis.deliver.verify.types instead (the leaf module)."
    )
    # The leaf import is what the fix expects to see.
    assert "from anastomosis.deliver.verify.types import LevelCoverage" in reports_src, (
        "browser/reports.py must import LevelCoverage from the leaf .verify.types "
        "module (the break-the-cycle fix)."
    )
