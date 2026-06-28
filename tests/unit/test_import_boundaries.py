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
