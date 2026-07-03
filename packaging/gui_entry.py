# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""Frozen GUI entry point for the Nuitka Windows build.

Nuitka warns against compiling a package's ``__main__.py`` AS the main module, so
the build (``build_windows.py``) compiles THIS top-level script instead. It just
delegates to the tested :func:`anastomosis.gui.__main__.main` (which launches the
pywebview shell and reports a startup failure cleanly).
"""

from anastomosis.gui.__main__ import main

if __name__ == "__main__":
    main()
