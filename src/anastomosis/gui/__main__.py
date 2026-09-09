"""Desktop GUI entry point (``python -m anastomosis.gui`` and the frozen exe).

``webview`` is imported lazily inside :func:`anastomosis.gui.shell.launch`,
so importing this module never requires the ``gui`` extra; a missing runtime
surfaces as a clean message + non-zero exit, never a traceback.
"""

from __future__ import annotations

import sys
import traceback


def _self_check() -> int:
    """Run the bundled-asset self-check like ``anast doctor``; return 0/1.

    ``detail`` stays count/enumerated-code/exception-type only (RULES.md 2).
    """
    from anastomosis.commands.selfcheck import check_bundled_assets
    from anastomosis.core.presentation import terminal_glyphs

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
    return _self_check_info(glyphs)


def _self_check_info(glyphs: object) -> int:
    """Prove ``info()`` answers — the dashboard paints nothing until it does,
    and it reaches every source adapter and pack context, which the asset
    checks do not. Printed, not logged: RULES.md 2 bars a traceback from a
    log, and this call has no record in scope to leak.
    """
    from anastomosis.commands.run import get_toolkit_info, toolkit_payload

    ok_mark = getattr(glyphs, "ok", "+")
    fail_mark = getattr(glyphs, "fail", "x")
    try:
        payload = toolkit_payload(get_toolkit_info())
    except Exception:
        print(f"  {fail_mark} toolkit info: raised")
        traceback.print_exc()
        return 1
    sources = payload["sources"]
    packs = payload["packs"]
    assert isinstance(sources, list) and isinstance(packs, list)
    print(
        f"  {ok_mark} toolkit info: {payload['version']}, "
        f"{len(sources)} source(s), {len(packs)} pack(s)"
    )
    if not sources or not packs:
        print("toolkit info answered with no sources or no packs")
        return 1
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

    from anastomosis.commands.run import get_toolkit_info
    from anastomosis.gui.shell import launch

    try:
        get_toolkit_info()  # before launch: the bridge must not first-import pydantic
        launch()
    except Exception as exc:  # top-level entry: a user must never see a raw traceback
        # Type name only, never exc's message: it may embed input (RULES.md 2).
        print(f"GUI failed to start ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
