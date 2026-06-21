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
"""

from __future__ import annotations

import sys


def main() -> None:
    """Launch the desktop GUI; report a startup failure cleanly (no traceback)."""
    from anastomosis.gui.shell import launch

    try:
        launch()
    except Exception as exc:  # top-level entry: a user must never see a raw traceback
        print(f"GUI failed to start ({type(exc).__name__}): {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
