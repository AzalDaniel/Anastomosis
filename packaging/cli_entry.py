# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""Frozen CLI entry point for the Nuitka Windows build.

A top-level entry (not the in-package ``anastomosis.cli`` module) so Nuitka
compiles a plain main script; it just runs the Typer ``app``.
"""

from anastomosis.cli import app

if __name__ == "__main__":
    app()
