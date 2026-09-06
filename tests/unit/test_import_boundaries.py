"""Enforce package import boundaries that the architecture relies on.

Quick property checks against ``sys.modules``: import a frontend package
in isolation (subprocess, so the test runner's already-loaded modules
don't bias the result) and assert no forbidden module ended up loaded.

Today's only boundary: the GUI must not depend on the CLI (peer frontends
over a shared core). The shared browser-attach code both import directly
lives in ``anastomosis.deliver.browser.attach``.
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
    """``anastomosis.gui`` must not pull any ``anastomosis.cli_commands``
    module into ``sys.modules`` either: each command group imports
    ``anastomosis.cli`` at its top, so a GUI-to-cli_commands edge would
    drag the whole CLI in — the same peer-frontend boundary, one layer
    down."""
    loaded = _modules_after_import("anastomosis.gui")
    leaked = {name for name in loaded if name.startswith("anastomosis.cli_commands")}
    assert not leaked, (
        f"anastomosis.gui leaked cli_commands modules into sys.modules: {sorted(leaked)}. "
        "The GUI must not import any CLI command group (they import anastomosis.cli)."
    )


def test_cli_does_not_eagerly_import_source_adapters_or_destinations() -> None:
    """``anastomosis.cli`` backing `--help`/`doctor`/`gui` must not
    eagerly import any source adapter or destination client (rule 75),
    each lazy per-command. ``anastomosis.deliver.fhir_api`` itself is
    exempt (a near-empty package init `cli_commands.upload` needs at
    module load); its heavy children are not."""
    loaded = _modules_after_import("anastomosis.cli")
    forbidden = {
        "anastomosis.sources.ccda",
        "anastomosis.sources.fhir_r4",
        "anastomosis.sources.oracle_ehi",
        "anastomosis.sources.pf_tebra",
        "anastomosis.deliver.browser",
        "anastomosis.deliver.fhir_api.client",
        "anastomosis.deliver.fhir_api.destination",
    }
    leaked = forbidden & loaded
    assert not leaked, f"anastomosis.cli eagerly imported: {sorted(leaked)}"


def test_cli_does_not_eagerly_import_the_greeting_mark() -> None:
    """The vessel mark is drawn only for a person at a terminal (rule 75):
    no named command may pay for the sampled grid, the density ramp, or
    Rich's live display just by importing the CLI."""
    loaded = _modules_after_import("anastomosis.cli")
    forbidden = {
        "anastomosis.core.vesselmark",
        "anastomosis.core.vesselmark_data",
        "rich.live",
    }
    leaked = forbidden & loaded
    assert not leaked, f"anastomosis.cli eagerly imported: {sorted(leaked)}"


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


def test_cli_make_destination_delegates_to_attach_destination() -> None:
    """Long-standing tests monkeypatch ``anastomosis.cli._make_destination``,
    so this stays a thin lazy-import wrapper — never a plain module-level
    assignment, which would force ``anastomosis.cli`` to eagerly import the
    whole upload-engine package — and must delegate straight through to
    the canonical :func:`attach_destination`, not a stale re-implementation."""
    from unittest.mock import patch

    from anastomosis.cli import _make_destination

    sentinel = object()
    with patch(
        "anastomosis.deliver.browser.attach.attach_destination", return_value=sentinel
    ) as mock_attach:
        result = _make_destination("http://127.0.0.1:9222", "loaded")

    mock_attach.assert_called_once_with("http://127.0.0.1:9222", "loaded")
    assert result is sentinel


# --- public verification imports (circular-import regression) --------------
#
# A circular import between ``deliver.verify.composite`` and
# ``deliver.browser.reports`` can hide from a full-suite run because the
# test runner's import order happens to load the modules in a safe sequence
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
    its canonical home (:mod:`.verify.types`) and a re-export from
    :mod:`.verify.composite`. Both imports must succeed in a fresh process
    and resolve to the SAME class object — the alias-identity contract
    downstream consumers rely on."""
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
    """``browser.reports`` must NOT *directly* import from
    :mod:`.verify.composite`: that import cycles back through
    ``browser.errors`` and ``browser/__init__.py`` into ``.reports``
    itself, resolving ``LevelCoverage`` against a partially-initialized
    module. Its canonical home is the leaf module :mod:`.verify.types`."""
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
        "That's the cycle this test guards against. Import LevelCoverage from "
        "anastomosis.deliver.verify.types instead (the leaf module)."
    )
    # The leaf import is what the fix expects to see.
    assert "from anastomosis.deliver.verify.types import LevelCoverage" in reports_src, (
        "browser/reports.py must import LevelCoverage from the leaf .verify.types "
        "module (the break-the-cycle fix)."
    )
