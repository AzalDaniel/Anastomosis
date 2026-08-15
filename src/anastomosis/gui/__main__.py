"""Desktop GUI entry point.

``python -m anastomosis.gui`` and the frozen GUI executable produced by the
Windows packaging build both launch the liquid-glass dashboard through the
pywebview shell — the same code path the CLI's ``anast gui`` command runs. The
packaged app's Start-menu shortcut targets this entry (and the
``anastomosis-gui`` GUI script declared in ``pyproject.toml``).

Kept deliberately thin: it only resolves the shell and reports a startup failure
cleanly. ``webview`` is imported lazily inside :func:`anastomosis.gui.shell.launch`,
so importing this module never requires the ``gui`` extra; a missing runtime
surfaces as a clean message + non-zero exit, never a traceback.

A ``--self-check`` flag runs the SAME bundled-asset self-check the CLI's
``anast doctor`` runs — but against the GUI bundle (its own copy of every data
asset + Chromium), WITHOUT launching the pywebview window — and exits 0/1. The
Windows packaging CI runs the GUI exe with this flag so a mis-bundled GUI build
fails the job instead of shipping a broken Start-menu app. It deliberately uses
plain ``print`` + the shared :func:`~anastomosis.core.presentation.terminal_glyphs`
(not Rich/typer) so it stays as dependency-light as the launch path.
"""

from __future__ import annotations

import sys


def _self_check() -> int:
    """Run the bundled-asset self-check and print it like ``anast doctor``; return an exit code.

    Mirrors :func:`anastomosis.cli.doctor_cmd`: one line per check (glyph, name,
    detail), then an aggregate verdict; ``0`` when every check passed, ``1``
    otherwise. PHI-free by construction — a check's detail is a count / "ok" /
    "skipped" / an exception TYPE name (see :mod:`anastomosis.core.selfcheck`).
    """
    from anastomosis.core.presentation import terminal_glyphs
    from anastomosis.core.selfcheck import check_bundled_assets

    glyphs = terminal_glyphs(sys.stdout)
    result = check_bundled_assets()
    for check in result.checks:
        mark = glyphs.ok if check.ok else glyphs.fail
        print(f"  {mark} {check.name}: {check.detail}")
    if not result.ok:
        failed = sum(1 for c in result.checks if not c.ok)
        print(f"{failed} asset check(s) failed")
        return 1
    print(f"all {len(result.checks)} asset checks passed")
    return 0


def main() -> None:
    """Launch the desktop GUI; report a startup failure cleanly (no traceback).

    With ``--self-check`` on the command line, runs the bundled-asset self-check
    (the GUI bundle's own assets + Chromium) and exits 0/1 WITHOUT launching the
    window — the CI gate for the GUI bundle.
    """
    if "--self-check" in sys.argv[1:]:
        raise SystemExit(_self_check())

    # Install the redacting log handler before launching (the root logger
    # otherwise falls back to an unredacted lastResort handler). logutil is
    # stdlib-only, so a plain lazy import keeps this entry dependency-light.
    import logging

    from anastomosis.core.logutil import configure_logging

    configure_logging(logging.WARNING)

    from anastomosis.gui.shell import launch

    try:
        launch()
    except Exception as exc:  # top-level entry: a user must never see a raw traceback
        # Broad catch: print the exception TYPE only (its message may embed
        # input); the type name is always safe to surface.
        print(f"GUI failed to start ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
