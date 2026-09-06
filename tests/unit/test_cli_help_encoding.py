"""Typer-rendered help must survive a legacy Windows console (cp1252):
Typer renders COMMAND DOCSTRINGS and option ``help=`` strings itself,
so a character cp1252 cannot encode crashes ``anast --help`` with a
``UnicodeEncodeError`` before any command runs. Two layers of defense:

* a STATIC sweep over every Typer-visible string in the app tree;
* an end-to-end reproducer: render ``--help`` and ``migrate --help``
  in a subprocess whose stdio is pinned to ``cp1252:strict``.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys

import pytest
import typer

from anastomosis.cli import app


def _visible_strings() -> list[tuple[str, str]]:
    """Every (where, text) pair Typer can render into help output:
    walks the Typer app natively (no click layer), collecting each
    command's help/docstring, sub-app group help, and every
    ``typer.Option``/``typer.Argument`` ``help=`` string."""
    pairs: list[tuple[str, str]] = []

    def _callback_strings(path: str, callback: object) -> None:
        doc = inspect.getdoc(callback)
        if doc:
            pairs.append((path, doc))
        try:
            # eval_str: the CLI module uses `from __future__ import annotations`,
            # so without evaluation the Annotated metadata (typer.Option help=)
            # is invisible — the sweep would silently skip every option string.
            sig = inspect.signature(callback, eval_str=True)  # type: ignore[arg-type]
        except (NameError, TypeError, ValueError):
            return
        for param in sig.parameters.values():
            for meta in getattr(param.annotation, "__metadata__", ()):
                help_text = getattr(meta, "help", None)
                if isinstance(help_text, str) and help_text:
                    pairs.append((f"{path} --{param.name}", help_text))

    def _walk(instance: typer.Typer, path: str) -> None:
        info_help = getattr(instance.info, "help", None)
        if isinstance(info_help, str) and info_help:
            pairs.append((path, info_help))
        for cmd in instance.registered_commands:
            cmd_path = f"{path} {cmd.name or getattr(cmd.callback, '__name__', '?')}"
            if isinstance(cmd.help, str) and cmd.help:
                pairs.append((cmd_path, cmd.help))
            if cmd.callback is not None:
                _callback_strings(cmd_path, cmd.callback)
        for group in instance.registered_groups:
            sub = group.typer_instance
            group_path = f"{path} {group.name or '?'}"
            if isinstance(group.help, str) and group.help:
                pairs.append((group_path, group.help))
            if sub is not None:
                _walk(sub, group_path)

    _walk(app, "anast")
    return pairs


def test_every_typer_visible_string_is_cp1252_encodable() -> None:
    pairs = _visible_strings()
    # The walker must actually see the app tree (guards against a silent
    # introspection break making this test vacuous).
    assert len(pairs) > 20, f"suspiciously few Typer-visible strings found: {len(pairs)}"
    offenders: list[str] = []
    for where, text in pairs:
        try:
            text.encode("cp1252")
        except UnicodeEncodeError as exc:
            snippet = text[max(0, exc.start - 10) : exc.end + 10]
            offenders.append(f"{where}: ...{snippet!r}... ({exc.reason})")
    assert not offenders, (
        "Typer-visible help text contains characters a cp1252 Windows console "
        "cannot encode — `anast --help` would crash there:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("args", [["--help"], ["migrate", "--help"]])
def test_cli_help_survives_cp1252_console(args: list[str]) -> None:
    """The end-to-end reproducer: rendering help with stdio pinned to
    strict cp1252 must exit 0 with no traceback. ``migrate --help``'s
    docstring carried the U+2192 arrow that cp1252 cannot encode."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "cp1252:strict"
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.argv = ['anast'] + sys.argv[1:]; "
            "from anastomosis.cli import app; app()",
            *args,
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    combined = proc.stderr + proc.stdout
    assert proc.returncode == 0, combined
    assert "Traceback" not in combined
    assert "UnicodeEncodeError" not in combined
