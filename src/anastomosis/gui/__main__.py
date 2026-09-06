"""Desktop GUI entry point (``python -m anastomosis.gui`` and the frozen exe).

``webview`` is imported lazily inside :func:`anastomosis.gui.shell.launch`,
so importing this module never requires the ``gui`` extra; a missing runtime
surfaces as a clean message + non-zero exit, never a traceback.
"""

from __future__ import annotations

import sys


def _self_check() -> int:
    """Run the bundled-asset self-check like ``anast doctor``; return 0/1.

    ``detail`` stays count/enumerated-code/exception-type only (RULES.md 2).
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

    ``--self-check`` runs the bundled-asset check instead, without a window.
    """
    if "--self-check" in sys.argv[1:]:
        raise SystemExit(_self_check())

    # Must run before launch: the root logger's default lastResort handler
    # leaks unredacted otherwise. logutil is stdlib-only, so import stays lazy.
    import logging

    from anastomosis.core.logutil import configure_logging

    configure_logging(logging.WARNING)

    from anastomosis.gui.shell import launch

    try:
        launch()
    except Exception as exc:  # top-level entry: a user must never see a raw traceback
        # Type name only, never exc's message: it may embed input (RULES.md 2).
        print(f"GUI failed to start ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
