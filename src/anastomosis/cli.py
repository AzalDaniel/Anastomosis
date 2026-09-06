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

# This module's docstring is the top-level `anast --help` text (`help=__doc__`
# below); the Typer apps, top-level commands, and shared pipeline/presentation
# helpers live here, while command bodies live in `cli_commands` and import at
# the BOTTOM so their decorators register against the apps defined above.

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

    from anastomosis.core.commands import DeliveryOutcome, PipelineCommand, SourceInfo
    from anastomosis.pipeline import StageEvent


# A real function (not a bare import alias) so
# `monkeypatch.setattr("anastomosis.cli._make_destination", ...)` keeps
# working and `anast upload` resolves it late through this module. Lazy
# import: `deliver.browser` eagerly pulls in the whole upload-engine package.
def _make_destination(cdp_url: str, loaded: object) -> object:
    from anastomosis.deliver.browser.attach import attach_destination

    return attach_destination(cdp_url, loaded)


# The FHIR twin of the seam above: `anast upload --fhir URL` resolves its
# destination late through `_cli._make_fhir_destination`, so a monkeypatch
# drives the flow with no live FHIR server; the import is lazy for the same
# reason as `deliver.browser`.
def _make_fhir_destination(
    base_url: str,
    *,
    bearer_token: str | None = None,
    create_missing_patients: bool = False,
    search_by_ssn: bool = False,
) -> object:
    from anastomosis.deliver.fhir_api.attach import attach_fhir_destination

    return attach_fhir_destination(
        base_url,
        bearer_token=bearer_token,
        create_missing_patients=create_missing_patients,
        search_by_ssn=search_by_ssn,
    )


app = typer.Typer(
    name="anast",
    help=__doc__,
    # OFF so bare `anast` reaches the callback below
    # (`invoke_without_command`), which offers a terminal session or, for any
    # non-interactive caller, this exact help page and exit code.
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
    """Status glyphs safe for the live console (ASCII on a non-UTF-8
    stream), resolved per call against ``console.file`` so a CP-1252 Windows
    console gets ASCII instead of a :class:`UnicodeEncodeError` mid-output.
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
    """Lossless medical-records migration — platform-agnostic and physician-friendly."""
    # The redacting log handler installs here, at the entry point (never at
    # import time); the call is idempotent.
    configure_logging(logging.WARNING)
    if ctx.invoked_subcommand is not None:
        return
    # A terminal gets the guided session; a pipe/script/CI gets the help page
    # and exit code, so nothing hangs on a prompt it cannot answer. The guide
    # imports only on this path so no command pays for it.
    from anastomosis.cli_commands.guide import is_interactive_terminal, run_guide

    if not is_interactive_terminal(console):
        ctx.get_help()  # Typer's Rich formatter prints the page as a side effect
        raise typer.Exit(code=2)
    raise typer.Exit(code=run_guide(console))


#: What each optional install lets a person do, named by the CAPABILITY not
#: the Python extra — the extra's own name still prints beside it for support.
CAPABILITY_NAMES = {
    "render": "Building and double-checking charts",
    "deliver-browser": "Filing charts through a browser",
    "fhir": "Sending charts over FHIR",
    "gui": "The desktop app",
}


def _print_source(source: SourceInfo) -> None:
    """One export format and the visits it would exclude, naming each rule
    by the word ``--include`` accepts — a flag whose vocabulary is written
    nowhere is a flag nobody can type. No export has been read yet.
    """
    ident = f" [dim]({source.name})[/dim]" if source.display != source.name else ""
    console.print(f"  export format [cyan]{source.display}[/cyan]{ident}: {source.description}")
    for rule, detail in sorted(source.selection.items()):
        console.print(f"    [dim]--include {rule}[/dim]: {detail.get('label', rule)}")


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
            # "installed", not "ready": `render` can read installed while
            # Playwright's browser never downloaded. `anast doctor` verifies.
            console.print(f"  [green]{what}[/green]: installed [dim]({extra})[/dim]")
        else:
            console.print(f"  [dim]{named}[/dim]: not available on this computer")
    # The readable name leads; the id typed at `--from`/`--pack` is the dim
    # caption (#164).
    for source in toolkit.sources:
        _print_source(source)
    for pack in toolkit.packs:
        origin = "built in" if pack.origin == "builtin" else pack.origin
        ident = f" [dim]({pack.name})[/dim]" if pack.display != pack.name else ""
        if pack.available:
            console.print(f"  chart layout [cyan]{pack.display}[/cyan]{ident}: ready ({origin})")
        else:
            console.print(f"  chart layout [red]{pack.display}[/red]{ident}: {pack.diagnosis}")
    console.print("[dim]  anast doctor checks that what is installed actually works.[/dim]")


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
    except Exception as exc:
        # Not RuntimeError: the shell raises that when pywebview itself is
        # absent, but pywebview's own WebViewException (bases Exception,
        # BaseException, object) is what it raises with no GTK/Qt backend to
        # draw with — the likelier failure. Escaped so Rich renders literal
        # "anastomosis[gui]" rather than reading [gui] as a style tag.
        console.print(f"[red]{escape(str(exc))}[/red]")
        console.print(f"Install the desktop parts with: {escape('pip install anastomosis[gui]')}")
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
# Pipeline mechanics live in :mod:`anastomosis.pipeline` (frontend-free); this
# wrapper keeps console output and exit codes byte-identical to before the
# extraction. `cli_commands` reaches these helpers late so test monkeypatch
# seams keep working.


def _make_event_printer(*, source: str | None, charts_dir: Path) -> Callable[[StageEvent], None]:
    """Contract: translates :class:`~anastomosis.pipeline.StageEvent`\\ s
    into the exact lines the CLI prints, parameterized by ``source`` (typed
    ``--source``/``--from`` suppresses "Detected source") and ``charts_dir``.
    Shared by ``anast migrate`` for byte-aligned output.
    """
    from anastomosis.pipeline import (
        STAGE_DETECT,
        STAGE_INGEST,
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
            # "skipped" means "already on disk"; excluded-by-selection gets
            # its own number, shown only when nonzero.
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
            # not_carried rides the counts dict only when nonzero (settle_qa);
            # a run that abbreviates nothing gets the line it always had (#297).
            not_carried = event.counts.get("not_carried", 0)
            console.print(
                f"QA: [green]{event.counts['pass']} pass[/green], "
                f"{event.counts['warn']} warn, "
                f"{'[red]' if fail else ''}{fail} fail"
                f"{'[/red]' if fail else ''}"
                + (
                    f" — [yellow]{not_carried} fact(s) carried by the record "
                    "summary, not the visit charts[/yellow]"
                    if not_carried
                    else ""
                )
                + f" {arrow} qa_report.json"
            )
        elif event.stage == STAGE_MANIFEST:
            # Additive line — emitted only when an upload manifest was requested.
            console.print(
                f"manifest: [green]{event.counts['items']} item(s)[/green] "
                f"{arrow} upload_manifest.json"
            )
        elif event.stage == STAGE_INGEST:
            # The ordinary ingest line stays silent; a quarantined row is the
            # run's honesty and must not read as green until reviewed.
            if quarantined := event.counts.get("quarantined", 0):
                console.print(
                    f"[yellow]{quarantined} row(s) quarantined — no patient to "
                    f"own them[/yellow] {arrow} quarantine.json"
                )

    return _print_event


def _run_command(cmd: PipelineCommand) -> None:
    """Run a :class:`PipelineCommand`, rendering its :class:`StageEvent`\\ s
    as the exact Rich lines the CLI printed before the pipeline extraction,
    then delivery outcomes in archive->bundle->ccda order;
    :class:`PipelineError` becomes the same ``console.print`` + ``typer.Exit``.
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
    _print_source_reading(result.pipeline.source_reading)


def _print_source_reading(reading: tuple[str, ...]) -> None:
    """Print the source ledger's account, when the source kept one — the
    sentences arrive PHI-free and pre-composed; this only frames them with a
    heading and a pointer to ``loss_ledger.json``. Silent for sources with no
    ledger.
    """
    if not reading:
        return
    console.print("[bold]What the source offered, and what arrived:[/bold]")
    for line in reading:
        console.print(f"  {line}")
    console.print(f"  {_glyphs().arrow} loss_ledger.json, beside the charts, for the full account")


def _print_delivery(outcome: DeliveryOutcome) -> None:
    """Print one deliverer's outcome, then anything it could not deliver."""
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
        # The document count rides beside the patient count because its
        # ABSENCE is what #373 looked like; zero prints nothing (most exports
        # carry no attachments).
        docs = counts.get("documents", 0)
        attached = f", {_plural(docs, 'document', 'documents')}" if docs else ""
        console.print(
            f"C-CDA: [green]{counts['patients']} patients[/green]{attached} "
            f"{arrow} {outcome.out_dir}"
        )
    _print_shortfall(outcome)


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _print_shortfall(outcome: DeliveryOutcome) -> None:
    """Say what this delivery could not file, only when there is something —
    under the success line, since folding it in is how a missing chart gets
    read as a statistic instead of a problem. Silent on an ordinary run.
    """
    counts = outcome.counts
    where = {"archive": "the archive", "bundle": "the bundles"}.get(outcome.kind, "it")
    missing = counts.get("missing", 0)
    unattributed = counts.get("unattributed", 0)
    if missing and outcome.kind == "ccda":
        # A different shortfall from the archive's: nothing was misfiled, a
        # patient simply has no document. Said as that, not as a chart count.
        console.print(
            f"  [yellow]{_plural(missing, 'patient', 'patients')} "
            f"{'has' if missing == 1 else 'have'} no C-CDA document[/yellow]; "
            f"the export is incomplete."
        )
    elif missing:
        console.print(
            f"  [yellow]{_plural(missing, 'chart', 'charts')} this run rendered "
            f"{'is' if missing == 1 else 'are'} missing from {where}.[/yellow] "
            f"Check the charts folder."
        )
    if unattributed:
        console.print(
            f"  [yellow]{_plural(unattributed, 'chart', 'charts')} could not be matched "
            f"to a patient[/yellow]; {'it is' if unattributed == 1 else 'they are'} "
            f"in unattributed/."
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


def _includes_or_exit(
    include: list[str] | None, *, source: str | None, pack: str
) -> tuple[str, ...]:
    """Parse ``--include`` rule names, converting a blank one to a clean exit 2
    rather than a traceback. Rule-NAME validation happens later, against the
    resolved source's own rules — the same two-step the section flags take."""
    from anastomosis.pipeline import PipelineError, parse_selection_includes

    try:
        return tuple(sorted(parse_selection_includes(include)))
    except PipelineError as exc:
        _report_pipeline_error(exc, source=source, pack=pack)
        raise typer.Exit(code=exc.exit_code) from None


def _report_pipeline_error(exc: object, *, source: str | None, pack: str) -> None:
    """Render a :class:`PipelineError` as the CLI's original lines, switching
    on the structured ``kind`` (not message prose). ``bad_output``/
    ``bad_section``/``bad_selection``/``bad_source``/``bad_destination``
    print the PHI-safe message; ``qa_failed`` prints nothing extra.
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
        # The reconstruct summary line already printed (the RECONSTRUCT
        # event); these are the per-encounter (id, type) detail lines.
        for encounter_id, exc_type in exc.failed:
            console.print(f"  [red]failed[/red] encounter {encounter_id} ({exc_type})")
    elif exc.kind == "qa_failed":
        # A QA failure printed only its summary line (the QA event) before
        # exiting; no extra error line here.
        return
    else:
        # bad_source/bad_output/bad_section/bad_selection/generic: print the
        # PHI-safe message at exit 2 (operator input) — except
        # conservation_failed, which lands here too at exit 1: lost work is
        # the run's failure, not the operator's.
        console.print(f"[red]{_escape(message)}[/red]")


# --- retained live seam: the selector validator for `destination init --validate` --
# `_make_validator` stays defined here (like `_make_destination`) even though
# its only caller moved to `cli_commands.destination`: wizard tests
# monkeypatch `cli._make_validator`, resolved late through this module.
# Playwright imports only here, lazily.


def _make_validator(cdp_url: str) -> object:
    """Build the live selector validator for ``--validate`` (the seam tests
    mock). Attaches over CDP (loopback-only) to the operator's browser,
    wraps its first page in :class:`PlaywrightPageAdapter`, and returns a
    :class:`~anastomosis.destinations.wizard.CdpSelectorValidator`.
    """
    from anastomosis.deliver.browser.cdp import CdpEndpoint, connect_over_cdp
    from anastomosis.destinations.browserpack import PlaywrightPageAdapter
    from anastomosis.destinations.wizard import CdpSelectorValidator

    # Drives the operator's existing EHR context/page; this one-shot leaves
    # teardown to process exit (the upload path owns explicit release()).
    _playwright, browser = connect_over_cdp(CdpEndpoint(cdp_url))
    context = browser.contexts[0]
    page = context.pages[0]
    return CdpSelectorValidator(PlaywrightPageAdapter(page))


# --- command registration (the facade split) --------------------------------
# Command bodies live under ``cli_commands``; importing them HERE (after the
# Typer apps and shared helpers above) runs their ``@<app>.command(...)``
# decorators against the already-defined app. Import ORDER fixes help order
# (info/gui/doctor above; migrate/upload/archive/bundle below), so
# ``# isort: off`` pins it against the alphabetiser; F401/E402 mark the
# side-effect-only import.
# isort: off
from anastomosis.cli_commands import pipeline  # noqa: E402, F401
from anastomosis.cli_commands import migrate  # noqa: E402, F401
from anastomosis.cli_commands import upload  # noqa: E402, F401
from anastomosis.cli_commands import delivery  # noqa: E402, F401
from anastomosis.cli_commands import destination  # noqa: E402, F401
from anastomosis.cli_commands import packsrc  # noqa: E402, F401

# isort: on


if __name__ == "__main__":
    # Under ``python -m anastomosis.cli`` this file runs as ``__main__``, a
    # second instance the command modules never see; delegate to the canonical one.
    from anastomosis.cli import app as _canonical_app

    _canonical_app()
