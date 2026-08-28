"""Anastomosis command-line interface.

Installed as both ``anast`` (everyday) and ``anastomosis`` (formal).

    anast pipeline run  an EHR export      ->  finished, double-checked charts
    anast migrate       an EHR export      ->  what a move to another system needs
    anast upload        finished charts    ->  filed into another system
    anast archive       an EHR export      ->  a searchable offline copy
    anast bundle        an EHR export      ->  one folder per patient, for requests
    anast destination   which systems charts can go to, and how to set one up
    anast pack init     some sample PDFs   ->  a draft chart layout
    anast source init   one example export ->  Anastomosis learns that format
    anast info          what this copy can do on this computer
    anast doctor        check that nothing Anastomosis ships is missing
    anast gui           open the desktop app
"""

# NB: this module's docstring is the top-level ``anast --help`` text
# (``help=__doc__`` below), so it stays byte-identical to the command listing —
# the facade note lives here in a comment instead. This module keeps the Typer
# app objects, the top-level ``info``/``gui``/``doctor`` commands, and the
# pipeline machinery + presentation helpers the command groups share; the
# command bodies live in :mod:`anastomosis.cli_commands` and are imported at the
# BOTTOM of this file so their ``@<app>.command(...)`` decorators register
# against the already-defined apps here (the late-import facade split).

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console

import anastomosis
from anastomosis.core.logutil import configure_logging
from anastomosis.core.presentation import Glyphs, terminal_glyphs

if TYPE_CHECKING:
    from collections.abc import Callable

    from anastomosis.core.commands import DeliveryOutcome, PipelineCommand
    from anastomosis.pipeline import StageEvent


# NB: ``_make_destination`` is re-published from THIS module as a wrapper around
# :func:`anastomosis.deliver.browser.attach.attach_destination` — a genuine,
# mypy-visible module attribute (a real ``def``, NOT a bare import alias) so
# long-standing tests that
# ``monkeypatch.setattr("anastomosis.cli._make_destination", ...)`` keep working,
# and the moved ``anast upload`` command resolves it LATE through this module
# (``_cli._make_destination``) so that patch is honored. The implementation lives
# in ``deliver.browser.attach`` so the GUI never needs to import the CLI to reach
# it. The import is INSIDE the body (not at module top) because
# ``anastomosis.deliver.browser`` eagerly pulls in the whole upload-engine
# package on import; every other ``anast`` command pays nothing for it.
def _make_destination(cdp_url: str, loaded: object) -> object:
    from anastomosis.deliver.browser.attach import attach_destination

    return attach_destination(cdp_url, loaded)


# The API route's twin of the seam above: ``anast upload --fhir URL`` builds its
# destination through ``_cli._make_fhir_destination`` (resolved LATE, same as the
# browser seam), so ``monkeypatch.setattr(cli, "_make_fhir_destination", ...)``
# drives the whole upload flow with no FHIR server. The implementation lives in
# ``deliver.fhir_api.attach`` for the same reason as its browser counterpart, and
# the import is lazy for the same reason too (``deliver.fhir_api`` pulls in the
# client + destination modules at package import).
def _make_fhir_destination(
    base_url: str,
    *,
    bearer_token: str | None = None,
    create_missing_patients: bool = False,
) -> object:
    from anastomosis.deliver.fhir_api.attach import attach_fhir_destination

    return attach_fhir_destination(
        base_url,
        bearer_token=bearer_token,
        create_missing_patients=create_missing_patients,
    )


app = typer.Typer(
    name="anast",
    help=__doc__,
    # Typer's built-in no-args help short-circuits inside argument parsing, before
    # any callback runs. Turning it OFF lets bare ``anast`` reach the callback
    # below (``invoke_without_command``), which offers a person at a terminal the
    # guided session — and prints this exact help page, with the same exit code,
    # for every non-interactive caller.
    no_args_is_help=False,
    rich_markup_mode="rich",
)
pipeline_app = typer.Typer(help="Rebuild charts from an export, end to end.")
app.add_typer(pipeline_app, name="pipeline")
destination_app = typer.Typer(help="Inspect destinations and plan delivery routes.")
app.add_typer(destination_app, name="destination")
pack_app = typer.Typer(help="Make and inspect chart layouts.")
app.add_typer(pack_app, name="pack")
source_app = typer.Typer(help="Teach the toolkit a new structured export format.")
app.add_typer(source_app, name="source")
console = Console()


def _glyphs() -> Glyphs:
    """Status glyphs safe for the live console (ASCII on a non-UTF-8 stream).

    Resolved per call against ``console.file`` (lazily ``sys.stdout``) so it
    reflects the actual terminal — a CP-1252 Windows console gets ASCII instead
    of a :class:`UnicodeEncodeError` mid-output.
    """
    return terminal_glyphs(console.file)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"anastomosis {anastomosis.__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Reconstruct, verify, and re-home clinical records."""
    # Install the redacting log handler for every ``anast`` command (the root
    # logger otherwise falls back to an unredacted lastResort handler). Wired
    # here at the entry point, never at import time; the call is idempotent.
    configure_logging(logging.WARNING)
    if ctx.invoked_subcommand is not None:
        return
    # Bare ``anast``. A person at a terminal gets the guided session; every other
    # caller — a pipe, a script, CI — gets exactly the help page and exit code
    # this has always printed, so nothing can hang on a prompt it cannot answer.
    # The whole guide lives in cli_commands.guide, imported only on this path so
    # no command (and no ``--help``) pays for it.
    from anastomosis.cli_commands.guide import is_interactive_terminal, run_guide

    if not is_interactive_terminal(console):
        ctx.get_help()  # Typer's Rich formatter prints the page as a side effect
        raise typer.Exit(code=2)
    raise typer.Exit(code=run_guide(console))


#: What each optional install actually lets a person do. `anast info` answers
#: "can this computer do the thing I want", so it names the thing, not the
#: Python extra that carries it — the extra's own name still prints beside it
#: for a support request.
CAPABILITY_NAMES = {
    "render": "Building charts",
    "render-qa": "Double-checking charts",
    "fhir": "Sending charts over FHIR",
    "gui": "The desktop app",
}


@app.command()
def info() -> None:
    """Show what this copy of Anastomosis can do on this computer."""
    from anastomosis.core.commands import get_toolkit_info

    toolkit = get_toolkit_info()
    console.print(f"[bold]anastomosis[/bold] {toolkit.version}")
    for extra, available in toolkit.extras.items():
        # The packaging name is what a support request needs; what an operator
        # needs is whether the thing it enables works. Both, in that order.
        what = CAPABILITY_NAMES.get(extra, extra)
        named = f"{what} [dim]({extra})[/dim]" if what != extra else what
        if available:
            console.print(f"  [green]{what}[/green]: ready [dim]({extra})[/dim]")
        else:
            console.print(f"  [dim]{named}[/dim]: not installed on this computer")
    for name, description in toolkit.sources:
        console.print(f"  export format [cyan]{name}[/cyan]: {description}")
    for pack in toolkit.packs:
        origin = "built in" if pack.origin == "builtin" else pack.origin
        if pack.available:
            console.print(f"  chart layout [cyan]{pack.name}[/cyan]: ready ({origin})")
        else:
            console.print(f"  chart layout [red]{pack.name}[/red]: {pack.diagnosis}")


@app.command("gui")
def gui_cmd(
    debug: Annotated[
        bool, typer.Option("--debug", help="Open the webview with developer tools.")
    ] = False,
) -> None:
    """Open the desktop app. Needs the desktop parts to be installed."""
    from rich.markup import escape

    try:
        from anastomosis.gui.shell import launch
    except ImportError as exc:  # the shell module itself failed to import
        console.print(
            f"[red]GUI unavailable[/red] ({type(exc).__name__}) — "
            f"install {escape('anastomosis[gui]')}"
        )
        raise typer.Exit(code=1) from None
    try:
        launch(debug=debug)
    except RuntimeError as exc:
        # The shell raises RuntimeError naming the extra when pywebview is absent.
        # Escape so Rich renders the literal "anastomosis[gui]" (the [gui] is not
        # a style tag) rather than swallowing the bracketed extra name.
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None


@app.command("doctor")
def doctor_cmd() -> None:
    """Verify every bundled data asset is present and readable (install health).

    Resolves the destinations registry, the built-in template packs, the vendored
    HL7 C-CDA stylesheet, the GUI web tree + fonts, the learned-source synonyms,
    the archive assets, and — in a packaged build — the bundled Chromium, each
    through the same accessor the app uses at runtime. Exits non-zero if any
    required asset is missing, so the Windows packaging CI can run it against the
    frozen executable to prove the installer bundle is complete.
    """
    from anastomosis.core.selfcheck import check_bundled_assets

    glyphs = _glyphs()
    result = check_bundled_assets()
    for check in result.checks:
        mark = glyphs.ok if check.ok else glyphs.fail
        console.print(f"  {mark} {check.name}: {check.detail}")
    if not result.ok:
        failed = sum(1 for c in result.checks if not c.ok)
        console.print(f"[red]{failed} asset check(s) failed[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]all {len(result.checks)} asset checks passed[/green]")


# --- shared pipeline machinery ---------------------------------------------
#
# The pipeline mechanics live in :mod:`anastomosis.pipeline` (frontend-free, so
# the GUI drives the same code). This CLI wrapper consumes that core and keeps
# every console message and exit code byte-identical to before the extraction.
# The command groups in :mod:`anastomosis.cli_commands` reach these helpers (and
# ``console``/``_glyphs``) late through this module so the monkeypatch seams the
# tests rely on keep working.


def _make_event_printer(*, source: str | None, charts_dir: Path) -> Callable[[StageEvent], None]:
    """Build the stage-event → Rich-line translator both run paths share.

    Translates the structured :class:`~anastomosis.pipeline.StageEvent`\\ s back
    into the exact lines the CLI prints — the detect/reconstruct/QA lines —
    parameterized by the operator's ``source`` (a typed ``--source`` /
    ``--from`` suppresses the "Detected source" announcement) and the
    ``charts_dir`` the reconstruct line names. ``anast migrate`` reuses this so
    its rendering stays byte-aligned with ``anast pipeline run``.
    """
    from anastomosis.pipeline import (
        STAGE_DETECT,
        STAGE_MANIFEST,
        STAGE_QA,
        STAGE_RECONSTRUCT,
    )

    arrow = _glyphs().arrow

    def _print_event(event: StageEvent) -> None:
        if event.stage == STAGE_DETECT:
            # Announce only genuine auto-detection (the original behavior):
            # an operator who typed --source already knows the source.
            if source is None:
                console.print(f"Detected source: [cyan]{event.detail}[/cyan]")
        elif event.stage == STAGE_RECONSTRUCT:
            failed = event.counts["failed"]
            # "skipped" here means "already on disk", so on a first run it reads
            # as "nothing was left out" — and encounters the source's own
            # selection rules excluded were invisible. They get their own
            # number, and only when there are any: a run that left nothing out
            # should not have to say so on every line.
            excluded = event.counts.get("excluded", 0)
            console.print(
                f"[green]{event.counts['rendered']} rendered[/green], "
                f"{event.counts['skipped']} skipped, "
                + (f"[yellow]{excluded} excluded by selection rules[/yellow], " if excluded else "")
                + f"{'[red]' if failed else ''}{failed} failed"
                f"{'[/red]' if failed else ''} {arrow} {charts_dir}"
            )
        elif event.stage == STAGE_QA:
            if event.detail:  # QA downgraded (no PyMuPDF)
                console.print(f"[yellow]QA skipped[/yellow]: {event.detail.split(': ', 1)[-1]}")
                return
            fail = event.counts["fail"]
            console.print(
                f"QA: [green]{event.counts['pass']} pass[/green], "
                f"{event.counts['warn']} warn, "
                f"{'[red]' if fail else ''}{fail} fail"
                f"{'[/red]' if fail else ''} {arrow} qa_report.json"
            )
        elif event.stage == STAGE_MANIFEST:
            # Additive line — emitted only when an upload manifest was requested.
            console.print(
                f"manifest: [green]{event.counts['items']} item(s)[/green] "
                f"{arrow} upload_manifest.json"
            )
        # The ingest stage prints no CLI line of its own (the original printed none).

    return _print_event


def _run_command(cmd: PipelineCommand) -> None:
    """Run a :class:`PipelineCommand`, rendering its events as the CLI always has.

    The structured :class:`~anastomosis.pipeline.StageEvent`\\ s are translated
    back into the exact Rich lines the CLI printed before the
    :mod:`anastomosis.pipeline` extraction, then the delivery outcomes are
    printed in archive→bundle→ccda order; :class:`PipelineError` becomes the
    same ``console.print`` + ``typer.Exit`` it raised inline.
    """
    from anastomosis.core.commands import run_pipeline_command
    from anastomosis.pipeline import PipelineError

    _print_event = _make_event_printer(source=cmd.source, charts_dir=cmd.charts_dir)

    try:
        result = run_pipeline_command(cmd, on_event=_print_event)
    except PipelineError as exc:
        _report_pipeline_error(exc, source=cmd.source, pack=cmd.pack)
        raise typer.Exit(code=exc.exit_code) from None
    for kind in ("archive", "bundle", "ccda"):
        outcome = result.deliveries.get(kind)
        if outcome is not None:
            _print_delivery(outcome)


def _print_delivery(outcome: DeliveryOutcome) -> None:
    """Print one deliverer's outcome, byte-identical to the pre-extraction lines."""
    counts = outcome.counts
    arrow = _glyphs().arrow
    if outcome.kind == "archive":
        console.print(
            f"Archive: [green]{counts['patients']} patients[/green], "
            f"{counts['encounters']} encounters, {counts['pdfs']} pdfs {arrow} {outcome.out_dir}"
        )
    elif outcome.kind == "bundle":
        console.print(
            f"Bundles: [green]{counts['patients']} patients[/green] {arrow} {outcome.out_dir}"
        )
    elif outcome.kind == "ccda":
        console.print(
            f"C-CDA: [green]{counts['patients']} patients[/green] {arrow} {outcome.out_dir}"
        )


def _sections_or_exit(
    section: list[str] | None, *, source: str | None, pack: str
) -> dict[str, bool]:
    """Parse ``--section`` overrides, converting a strict-parse failure (a bad
    value or a missing ``=value``) to a clean exit 2 rather than a traceback.
    Section-NAME validation happens later, against the resolved pack."""
    from anastomosis.pipeline import PipelineError, parse_section_overrides

    try:
        return parse_section_overrides(section)
    except PipelineError as exc:
        _report_pipeline_error(exc, source=source, pack=pack)
        raise typer.Exit(code=exc.exit_code) from None


def _report_pipeline_error(exc: object, *, source: str | None, pack: str) -> None:
    """Render a :class:`PipelineError` as the exact lines the CLI used to print.

    Switches on the error's structured ``kind`` (not message prose) and
    reproduces the original line per kind byte-for-byte, so the existing CLI
    tests ("Could not identify", "unavailable", per-encounter failure lines)
    keep passing unchanged. The newer operator-input kinds (``bad_output``,
    ``bad_section``, ``bad_source``, ``bad_destination``) print their PHI-safe
    message; ``qa_failed`` prints nothing extra (its summary already rode the
    QA event).
    """
    from rich.markup import escape as _escape

    from anastomosis.pipeline import PipelineError

    assert isinstance(exc, PipelineError)
    message = str(exc)
    if exc.kind == "no_source":
        suffix = message[len("Could not identify the export format.") :]
        console.print(f"[red]Could not identify the export format.[/red]{suffix}")
        # Additive guidance (the failure and exit code are unchanged): a format
        # the toolkit has never seen can be taught once from an example.
        console.print(
            "If this is a new format, teach it once: "
            "[cyan]anast source init <example-file> --name <label>[/cyan]"
        )
    elif exc.kind == "bad_pack":
        diagnosis = message.split(": ", 1)[1]
        console.print(f"[red]Pack {pack!r} unavailable:[/red] {diagnosis}")
    elif exc.kind == "render_failed":
        # The reconstruct summary line already printed (the RECONSTRUCT event);
        # now the per-encounter (id, type) detail lines, exactly as before.
        for encounter_id, exc_type in exc.failed:
            console.print(f"  [red]failed[/red] encounter {encounter_id} ({exc_type})")
    elif exc.kind == "qa_failed":
        # A QA failure printed only its summary line (the QA event) before
        # exiting; no extra error line is emitted here — matching the original.
        return
    else:
        # bad_source / bad_output / bad_section / generic: print the PHI-safe
        # message. Exit code 2 (operator input), per the CLI's exit-code contract.
        console.print(f"[red]{_escape(message)}[/red]")


# --- retained live seam: the selector validator for `destination init --validate` --
#
# ``_make_validator`` stays DEFINED here (like ``_make_destination`` above) even
# though its only caller — ``destination init`` — moved to
# :mod:`anastomosis.cli_commands.destination`. Long-standing wizard tests do
# ``monkeypatch.setattr(cli, "_make_validator", ...)``; the moved command
# resolves it LATE through this module (``_cli._make_validator``) so the patch is
# honored. Playwright is imported only here (lazily, via ``connect_over_cdp``).


def _make_validator(cdp_url: str) -> object:
    """Build the live selector validator for ``--validate`` (the SEAM tests mock).

    Attaches over CDP (loopback-only, validated) to the browser the operator
    launched and logged into, wraps its first page in the
    :class:`PlaywrightPageAdapter`, and returns a
    :class:`~anastomosis.destinations.wizard.CdpSelectorValidator`. Tests
    monkeypatch this whole function so the validation flow needs no browser.
    Playwright is imported only here (lazily, via ``connect_over_cdp``).
    """
    from anastomosis.deliver.browser.cdp import CdpEndpoint, connect_over_cdp
    from anastomosis.destinations.browserpack import PlaywrightPageAdapter
    from anastomosis.destinations.wizard import CdpSelectorValidator

    # The operator has their EHR open; drive its existing context/page. This is
    # the interactive `destination init --validate` one-shot — the Playwright
    # driver is reaped at process exit (the upload path owns explicit teardown via
    # run_upload_command's release()). We never close the operator's context/page.
    _playwright, browser = connect_over_cdp(CdpEndpoint(cdp_url))
    context = browser.contexts[0]
    page = context.pages[0]
    return CdpSelectorValidator(PlaywrightPageAdapter(page))


# --- command registration (the facade split) --------------------------------
#
# The command bodies live in focused modules under ``cli_commands``. Importing
# them HERE — after the Typer apps and the shared helpers above are defined —
# runs their module-level ``@<app>.command(...)`` decorators, registering each
# command against the already-defined app (the standard late-import pattern).
# The import ORDER fixes the help order of the direct ``app`` commands
# (info/gui/doctor are defined above; migrate/upload then archive/bundle
# register below in that order), so ``# isort: off`` pins it against the
# alphabetiser. The modules are imported for this side effect only (hence F401)
# and sit below the app definitions (hence E402).
# isort: off
from anastomosis.cli_commands import pipeline  # noqa: E402, F401
from anastomosis.cli_commands import migrate  # noqa: E402, F401
from anastomosis.cli_commands import upload  # noqa: E402, F401
from anastomosis.cli_commands import delivery  # noqa: E402, F401
from anastomosis.cli_commands import destination  # noqa: E402, F401
from anastomosis.cli_commands import packsrc  # noqa: E402, F401

# isort: on


if __name__ == "__main__":
    # Under ``python -m anastomosis.cli`` this file runs as ``__main__`` — a
    # second module instance whose Typer apps the command modules never see
    # (they register on ``anastomosis.cli``). Delegate to the canonical
    # instance so both invocation forms expose the same command set.
    from anastomosis.cli import app as _canonical_app

    _canonical_app()
