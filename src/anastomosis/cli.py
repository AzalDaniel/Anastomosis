"""Anastomosis command-line interface.

Installed as both ``anast`` (everyday) and ``anastomosis`` (formal).
Sub-commands appear as their pipeline stages are implemented:

    anast ingest        EHI export / C-CDA  ->  canonical records
    anast reconstruct   canonical records   ->  rendered documents
    anast qa            rendered documents  ->  QA report
    anast archive       full pipeline       ->  searchable offline archive
    anast bundle        full pipeline       ->  per-patient bundles
    anast pipeline run  one command, whole pipeline (charts + optional archive/bundle)
    anast pack init     sample PDFs         ->  a draft template pack
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console

import anastomosis
import anastomosis.sources.ccda
import anastomosis.sources.oracle_ehi
import anastomosis.sources.pf_tebra

if TYPE_CHECKING:
    from anastomosis.core.commands import DeliveryOutcome, PipelineCommand
    from anastomosis.core.model import PatientRecord

app = typer.Typer(
    name="anast",
    help=__doc__,
    no_args_is_help=True,
    rich_markup_mode="rich",
)
pipeline_app = typer.Typer(help="Run pipeline stages end to end.")
app.add_typer(pipeline_app, name="pipeline")
destination_app = typer.Typer(help="Inspect destinations and plan delivery routes.")
app.add_typer(destination_app, name="destination")
pack_app = typer.Typer(help="Build and inspect template packs.")
app.add_typer(pack_app, name="pack")
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"anastomosis {anastomosis.__version__}")
        raise typer.Exit()


@app.callback()
def main(
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


@app.command()
def info() -> None:
    """Show toolkit status: installed extras, sources, packs, environment."""
    from anastomosis.core.commands import get_toolkit_info

    toolkit = get_toolkit_info()
    console.print(f"[bold]anastomosis[/bold] {toolkit.version}")
    for extra, available in toolkit.extras.items():
        if available:
            console.print(f"  extra [green]{extra}[/green]: available")
        else:
            console.print(f"  extra [dim]{extra}[/dim]: not installed")
    for name, description in toolkit.sources:
        console.print(f"  source [cyan]{name}[/cyan]: {description}")
    for pack in toolkit.packs:
        if pack.available:
            console.print(f"  pack [cyan]{pack.name}[/cyan]: available ({pack.origin})")
        else:
            console.print(f"  pack [red]{pack.name}[/red]: {pack.diagnosis}")


@app.command("gui")
def gui_cmd(
    debug: Annotated[
        bool, typer.Option("--debug", help="Open the webview with developer tools.")
    ] = False,
) -> None:
    """Launch the desktop GUI (liquid-glass dashboard). Needs the gui extra."""
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


# --- shared pipeline machinery ---------------------------------------------
#
# The pipeline mechanics live in :mod:`anastomosis.pipeline` (frontend-free, so
# the GUI drives the same code). This CLI wrapper consumes that core and keeps
# every console message and exit code byte-identical to before the extraction.


def _run_command(cmd: PipelineCommand) -> None:
    """Run a :class:`PipelineCommand`, rendering its events as the CLI always has.

    The structured :class:`~anastomosis.pipeline.StageEvent`\\ s are translated
    back into the exact Rich lines the CLI printed before the
    :mod:`anastomosis.pipeline` extraction, then the delivery outcomes are
    printed in archive→bundle→ccda order; :class:`PipelineError` becomes the
    same ``console.print`` + ``typer.Exit`` it raised inline.
    """
    from anastomosis.core.commands import run_pipeline_command
    from anastomosis.pipeline import (
        STAGE_DETECT,
        STAGE_QA,
        STAGE_RECONSTRUCT,
        PipelineError,
        StageEvent,
    )

    def _print_event(event: StageEvent) -> None:
        if event.stage == STAGE_DETECT:
            # Announce only genuine auto-detection (the original behavior):
            # an operator who typed --source already knows the source.
            if cmd.source is None:
                console.print(f"Detected source: [cyan]{event.detail}[/cyan]")
        elif event.stage == STAGE_RECONSTRUCT:
            failed = event.counts["failed"]
            console.print(
                f"[green]{event.counts['rendered']} rendered[/green], "
                f"{event.counts['skipped']} skipped, "
                f"{'[red]' if failed else ''}{failed} failed"
                f"{'[/red]' if failed else ''} → {cmd.charts_dir}"
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
                f"{'[/red]' if fail else ''} → qa_report.json"
            )
        # The ingest stage prints no CLI line of its own (the original printed none).

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
    if outcome.kind == "archive":
        console.print(
            f"Archive: [green]{counts['patients']} patients[/green], "
            f"{counts['encounters']} encounters, {counts['pdfs']} pdfs → {outcome.out_dir}"
        )
    elif outcome.kind == "bundle":
        console.print(f"Bundles: [green]{counts['patients']} patients[/green] → {outcome.out_dir}")
    elif outcome.kind == "ccda":
        console.print(f"C-CDA: [green]{counts['patients']} patients[/green] → {outcome.out_dir}")


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
    ``bad_section``, ``bad_source``) print their PHI-safe message; ``qa_failed``
    prints nothing extra (its summary already rode the QA event).
    """
    from rich.markup import escape as _escape

    from anastomosis.pipeline import PipelineError

    assert isinstance(exc, PipelineError)
    message = str(exc)
    if exc.kind == "no_source":
        suffix = message[len("Could not identify the export format.") :]
        console.print(f"[red]Could not identify the export format.[/red]{suffix}")
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


# --- pipeline run -----------------------------------------------------------


@pipeline_app.command("run")
def pipeline_run(
    export_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)],
    out: Annotated[Path, typer.Option("--out", "-o", help="Output directory (created 0700).")],
    source: Annotated[
        str | None,
        typer.Option("--source", "-s", help="Source adapter name (default: auto-detect)."),
    ] = None,
    pack: Annotated[str, typer.Option("--pack", "-p", help="Template pack name.")] = "generic_soap",
    pack_dir: Annotated[
        list[Path] | None,
        typer.Option("--pack-dir", help="Extra pack directories (implies trusting their code)."),
    ] = None,
    trust_pack: Annotated[
        bool,
        typer.Option(
            "--trust-pack",
            help="Trust the --pack-dir packs at their current code hash (records the hash; "
            "required the first time, and again after their code changes).",
        ),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Re-render documents that exist.")] = False,
    section: Annotated[
        list[str] | None,
        typer.Option(
            "--section",
            help="Override a section flag, e.g. --section insurance=on --section addenda=off.",
        ),
    ] = None,
    qa: Annotated[
        bool, typer.Option("--qa/--no-qa", help="Verify every rendered document (default on).")
    ] = True,
    archive: Annotated[
        Path | None,
        typer.Option("--archive", help="Also emit an offline browsable archive in this directory."),
    ] = None,
    bundle: Annotated[
        Path | None,
        typer.Option(
            "--bundle", help="Also emit one per-patient bundle subdirectory in this directory."
        ),
    ] = None,
    ccda: Annotated[
        Path | None,
        typer.Option("--ccda", help="Also emit one C-CDA / CCD XML per patient in this directory."),
    ] = None,
) -> None:
    """Ingest an export and reconstruct every encounter into chart PDFs."""
    from anastomosis.core.commands import DeliveryCommand, PipelineCommand

    sections = _sections_or_exit(section, source=source, pack=pack)
    deliveries: list[DeliveryCommand] = []
    if archive is not None:
        deliveries.append(DeliveryCommand("archive", archive))
    if bundle is not None:
        deliveries.append(DeliveryCommand("bundle", bundle))
    if ccda is not None:
        deliveries.append(DeliveryCommand("ccda", ccda))
    _run_command(
        PipelineCommand(
            export_dir=export_dir,
            charts_dir=out,
            source=source,
            pack=pack,
            pack_dirs=tuple(pack_dir or ()),
            force=force,
            trust_new=trust_pack,
            sections=sections,
            qa=qa,
            deliveries=tuple(deliveries),
        )
    )


# --- anast archive ----------------------------------------------------------


@app.command("archive")
def archive_cmd(
    export_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)],
    out: Annotated[Path, typer.Option("--out", "-o", help="Archive output directory (0700).")],
    source: Annotated[
        str | None,
        typer.Option("--source", "-s", help="Source adapter name (default: auto-detect)."),
    ] = None,
    pack: Annotated[str, typer.Option("--pack", "-p", help="Template pack name.")] = "generic_soap",
    pack_dir: Annotated[
        list[Path] | None,
        typer.Option("--pack-dir", help="Extra pack directories (implies trusting their code)."),
    ] = None,
    trust_pack: Annotated[
        bool,
        typer.Option(
            "--trust-pack",
            help="Trust the --pack-dir packs at their current code hash (records the hash; "
            "required the first time, and again after their code changes).",
        ),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Re-render documents that exist.")] = False,
    section: Annotated[
        list[str] | None,
        typer.Option("--section", help="Override a section flag."),
    ] = None,
    qa: Annotated[
        bool, typer.Option("--qa/--no-qa", help="Verify every rendered document (default on).")
    ] = True,
    charts_dir: Annotated[
        Path | None,
        typer.Option(
            "--charts-dir",
            help="Where chart PDFs land before being copied into the archive "
            "(default: <out>/_charts).",
        ),
    ] = None,
) -> None:
    """Run the full pipeline and write a searchable offline archive."""
    from anastomosis.core.commands import DeliveryCommand, PipelineCommand

    sections = _sections_or_exit(section, source=source, pack=pack)
    charts = charts_dir or (out / "_charts")
    _run_command(
        PipelineCommand(
            export_dir=export_dir,
            charts_dir=charts,
            source=source,
            pack=pack,
            pack_dirs=tuple(pack_dir or ()),
            force=force,
            trust_new=trust_pack,
            sections=sections,
            qa=qa,
            deliveries=(DeliveryCommand("archive", out),),
        )
    )


# --- anast bundle -----------------------------------------------------------


@app.command("bundle")
def bundle_cmd(
    export_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)],
    out: Annotated[Path, typer.Option("--out", "-o", help="Bundles output directory (0700).")],
    source: Annotated[
        str | None,
        typer.Option("--source", "-s", help="Source adapter name (default: auto-detect)."),
    ] = None,
    pack: Annotated[str, typer.Option("--pack", "-p", help="Template pack name.")] = "generic_soap",
    pack_dir: Annotated[
        list[Path] | None,
        typer.Option("--pack-dir", help="Extra pack directories (implies trusting their code)."),
    ] = None,
    trust_pack: Annotated[
        bool,
        typer.Option(
            "--trust-pack",
            help="Trust the --pack-dir packs at their current code hash (records the hash; "
            "required the first time, and again after their code changes).",
        ),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Re-render documents that exist.")] = False,
    section: Annotated[
        list[str] | None,
        typer.Option("--section", help="Override a section flag."),
    ] = None,
    qa: Annotated[
        bool, typer.Option("--qa/--no-qa", help="Verify every rendered document (default on).")
    ] = True,
    charts_dir: Annotated[
        Path | None,
        typer.Option(
            "--charts-dir",
            help="Where chart PDFs land before being copied into the bundles "
            "(default: <out>/_charts).",
        ),
    ] = None,
) -> None:
    """Run the full pipeline and write one per-patient bundle directory each."""
    from anastomosis.core.commands import DeliveryCommand, PipelineCommand

    sections = _sections_or_exit(section, source=source, pack=pack)
    charts = charts_dir or (out / "_charts")
    _run_command(
        PipelineCommand(
            export_dir=export_dir,
            charts_dir=charts,
            source=source,
            pack=pack,
            pack_dirs=tuple(pack_dir or ()),
            force=force,
            trust_new=trust_pack,
            sections=sections,
            qa=qa,
            deliveries=(DeliveryCommand("bundle", out),),
        )
    )


# --- anast destination ------------------------------------------------------


def _load_registry(registry: Path | None) -> object:
    """Load the destination registry (overlay if given).

    A malformed overlay (bad YAML, a schema violation, or a missing file) is an
    operator-input error: print a clean message and exit 2, never a pydantic or
    PyYAML traceback. The packaged registry is validated in CI, so a failure
    there would be a genuine bug — but the same clean exit still beats a
    traceback.
    """
    from pydantic import ValidationError
    from rich.markup import escape as _escape
    from yaml import YAMLError

    from anastomosis.destinations.registry import DestinationRegistry

    try:
        if registry is not None:
            return DestinationRegistry.merged(registry)
        return DestinationRegistry.load()
    except (ValidationError, YAMLError, OSError) as exc:
        where = f" {registry}" if registry is not None else ""
        console.print(
            f"[red]Invalid destination registry{where}[/red] "
            f"({_escape(type(exc).__name__)}) — check the file's YAML and schema."
        )
        raise typer.Exit(code=2) from None


def _oldest_evidence(entry: object) -> str:
    from anastomosis.destinations.registry import DestinationEntry

    assert isinstance(entry, DestinationEntry)
    dates = [
        cap.evidence.verified
        for cap in (entry.doc_write_api, entry.ccda_import, entry.browser)
        if cap.evidence is not None
    ]
    return min(dates).isoformat() if dates else "—"


def _local_pack_status(name: str) -> str:
    """Describe whether a discovered browser pack exists locally for ``name``.

    Surfaced in `destination list`/`route` so the operator can see a pack is
    present (and whether the wizard has been run) without it ever auto-affecting
    routing — the registry overlay stays the single routing truth.
    """
    from anastomosis.destinations.loader import BrowserPackError, load_destination_pack

    try:
        loaded = load_destination_pack(name)
    except BrowserPackError:
        return "—"
    return "ready" if loaded.ready else "needs-discovery"


@destination_app.command("list")
def destination_list(
    registry: Annotated[
        Path | None,
        typer.Option("--registry", help="Overlay registry file (replaces packaged entries)."),
    ] = None,
) -> None:
    """List registered destinations and their declared capabilities."""
    from rich.console import Console
    from rich.table import Table

    from anastomosis.destinations.registry import DestinationEntry, DestinationRegistry

    reg = _load_registry(registry)
    assert isinstance(reg, DestinationRegistry)
    table = Table(title="destinations")
    table.add_column("name", style="cyan")
    table.add_column("display")
    table.add_column("doc_write_api")
    table.add_column("ccda_import")
    table.add_column("browser")
    table.add_column("pack")
    table.add_column("oldest evidence")
    for name in sorted(reg.entries):
        entry: DestinationEntry = reg.entries[name]
        table.add_row(
            entry.name,
            entry.display,
            entry.doc_write_api.kind,
            entry.ccda_import.kind,
            entry.browser.kind,
            _local_pack_status(entry.name),
            _oldest_evidence(entry),
        )
    # A wide, non-truncating console so the seven columns (and their cell text)
    # survive intact regardless of the calling terminal width — the table is a
    # data dump the operator scrolls, not a width-fit layout.
    Console(width=200).print(table)


@destination_app.command("route")
def destination_route(
    name: Annotated[str, typer.Argument(help="Destination name (see `anast destination list`).")],
    registry: Annotated[
        Path | None,
        typer.Option("--registry", help="Overlay registry file (replaces packaged entries)."),
    ] = None,
) -> None:
    """Print the shortest-path transit map; exit 1 if no viable route exists."""
    from anastomosis.deliver.router import plan_route
    from anastomosis.destinations.registry import DestinationRegistry

    reg = _load_registry(registry)
    assert isinstance(reg, DestinationRegistry)
    try:
        transit = plan_route(name, reg)
    except KeyError as exc:
        # KeyError carries the known-names list (no PHI) — show it, not a traceback.
        console.print(f"[red]{exc.args[0] if exc.args else exc}[/red]")
        # Exit-code contract: 2 = unknown destination NAME (operator typo),
        # 1 = known destination with NO viable route (capability gap). Tests
        # pin both; scripts branch on them.
        raise typer.Exit(code=2) from None
    console.print(transit.render())
    # Surface a locally present browser pack WITHOUT auto-flipping routing: the
    # registry overlay remains the single routing truth, so we only note that a
    # pack exists and how the operator declares it.
    pack_status = _local_pack_status(name)
    if pack_status != "—" and transit.options[-1].kind.value == "browser":
        if not transit.options[-1].viable:
            console.print(
                f"note: browser pack present locally ({pack_status}) — declare it in your "
                "registry overlay (kind: pack) to route through it"
            )
        else:
            console.print(f"note: browser pack present locally ({pack_status})")
    if transit.chosen is None:
        raise typer.Exit(code=1)


# --- anast destination init (the selector-discovery wizard) -----------------

# The maximum re-entry attempts for a not-found selector under --validate before
# the operator must either accept it unvalidated or give up.
_VALIDATE_MAX_TRIES = 3


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

    browser = connect_over_cdp(CdpEndpoint(cdp_url))
    # The operator has their EHR open; drive its existing context/page.
    context = browser.contexts[0]
    page = context.pages[0]
    return CdpSelectorValidator(PlaywrightPageAdapter(page))


def _prompt_slot(
    slot: str,
    *,
    required: bool,
    guidance: str,
    validator: object | None,
) -> str:
    """Prompt for one selector slot, optionally validating it against the page.

    Optional slots accept an empty entry (= skip). With a ``validator``, a
    selector matching zero elements may be re-entered up to
    :data:`_VALIDATE_MAX_TRIES` times or accepted with an explicit confirmation
    (the ``--allow-unvalidated`` consent at the slot level). Without one, the
    selector is accepted as-is. PHI: prompts and prints carry slot names and
    selectors only — never patient data.
    """
    from anastomosis.destinations.wizard import SelectorValidator

    label = "required" if required else "optional, blank to skip"
    for attempt in range(1, _VALIDATE_MAX_TRIES + 1):
        raw: str = typer.prompt(f"  {slot} ({label}) — {guidance}", default="")
        value = raw.strip()
        if not value:
            if not required:
                return ""
            console.print("    [yellow]a value is required[/yellow]")
            continue
        if validator is None:
            return value
        assert isinstance(validator, SelectorValidator)
        count = validator.count(value)
        if count >= 1:
            console.print(f"    [green]found {count} element(s)[/green]")
            return value
        console.print(f"    [yellow]selector matched 0 elements[/yellow] (try {attempt})")
        if attempt < _VALIDATE_MAX_TRIES:
            continue
        if typer.confirm("    accept this unvalidated selector anyway?", default=False):
            return value
    # Exhausted tries without acceptance: re-raise as an explicit operator abort.
    console.print(f"[red]gave up discovering {slot!r} (no matching selector)[/red]")
    raise typer.Exit(code=1)


@destination_app.command("init")
def destination_init(
    name: Annotated[str, typer.Argument(help="Destination pack name, e.g. tebra.")],
    out_dir: Annotated[
        Path | None,
        typer.Option("--out-dir", help="Where to write selectors.yaml (default: user dir)."),
    ] = None,
    validate: Annotated[
        bool,
        typer.Option("--validate", help="Check each selector against a live page (needs --cdp)."),
    ] = False,
    cdp: Annotated[
        str | None,
        typer.Option("--cdp", help="Loopback CDP endpoint, e.g. http://127.0.0.1:9222."),
    ] = None,
    pack_dir: Annotated[
        list[Path] | None,
        typer.Option("--pack-dir", help="Extra directories to find the pack scaffold in."),
    ] = None,
) -> None:
    """Discover a browser pack's CSS selectors against your live EHR session.

    Loads the pack scaffold, prompts for each selector slot (required first),
    optionally validates each against your attached browser (``--validate
    --cdp``), then writes ``selectors.yaml`` into your user directory. The
    packaged registry is never modified — a paste-able overlay snippet is printed
    so you declare the now-discovered pack in your own routing overlay.
    """
    from anastomosis.deliver.browser.cdp import SHARED_MACHINE_WARNING
    from anastomosis.destinations.browserpack import SelectorMap
    from anastomosis.destinations.loader import (
        BrowserPackError,
        load_destination_pack,
        user_destinations_dir,
    )
    from anastomosis.destinations.wizard import (
        SLOT_GUIDANCE,
        registry_overlay_snippet,
        write_selectors,
    )

    try:
        loaded = load_destination_pack(name, list(pack_dir or []))
    except BrowserPackError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from None

    validator: object | None = None
    if validate:
        if cdp is None:
            console.print("[red]--validate requires --cdp (a loopback CDP endpoint)[/red]")
            raise typer.Exit(code=2)
        console.print(SHARED_MACHINE_WARNING)
        try:
            validator = _make_validator(cdp)
        except Exception as exc:  # attach/launch failure — name the type, no PHI
            console.print(f"[red]could not attach for validation ({type(exc).__name__})[/red]")
            raise typer.Exit(code=2) from None
    elif cdp is not None:
        # --cdp without --validate: still warn (a debug port was named).
        console.print(SHARED_MACHINE_WARNING)
    else:
        console.print(
            "[yellow]selectors accepted as-is[/yellow] — preflight validates them at run time"
        )

    console.print(f"Discovering selectors for [cyan]{loaded.name}[/cyan]:")
    discovered: dict[str, str] = {}
    for slot in SelectorMap.required_slots():
        discovered[slot] = _prompt_slot(
            slot, required=True, guidance=SLOT_GUIDANCE.get(slot, ""), validator=validator
        )
    for slot in SelectorMap.optional_slots():
        discovered[slot] = _prompt_slot(
            slot, required=False, guidance=SLOT_GUIDANCE.get(slot, ""), validator=validator
        )

    target_root = out_dir or user_destinations_dir()
    written = write_selectors(loaded.name, discovered, target_root)
    console.print(f"[green]wrote[/green] {written}")
    console.print(
        "\nNext steps — declare this pack in your registry overlay (NOT the packaged one):"
    )
    console.print(registry_overlay_snippet(loaded.name))
    console.print(
        f"Then route it:  anast destination route {loaded.name} --registry <your-overlay>.yaml"
    )


# --- anast pack init (the pack-from-samples wizard) -------------------------

# A pack name must be a safe directory + manifest identifier (it becomes the
# pack's directory name and YAML `name:`). Mirrors the loader's expectations.
_PACK_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# Below this many samples the static/per-patient split is statistically weak;
# the wizard warns loudly (the learner still runs).
_LOW_SAMPLE_FLOOR = 3


def _collect_sample_pdfs(patterns: list[str]) -> list[Path]:
    """Resolve dir-or-glob arguments into a sorted, de-duplicated PDF list.

    A bare directory contributes its ``*.pdf`` children; a glob is expanded;
    a direct path is taken as-is. Sorted for deterministic sample indices.
    """
    import glob as _glob

    found: set[Path] = set()
    for raw in patterns:
        candidate = Path(raw)
        if candidate.is_dir():
            found.update(p for p in candidate.glob("*.pdf"))
            continue
        if candidate.is_file():
            found.add(candidate)
            continue
        # Treat as a glob (supports ``./samples/*.pdf``).
        found.update(Path(match) for match in _glob.glob(raw) if Path(match).is_file())
    return sorted(found)


def _synthetic_preview_record() -> PatientRecord:
    """A tiny, fully-synthetic record for the side-by-side preview render.

    feedface- ids, a 555-exchange phone, an example.com-free facility, and the
    canonical synthetic patient (Testpatient, Synthia). Carries one signed SOAP
    encounter with vitals so every template branch the draft renders has data.
    """
    import datetime

    from anastomosis.core.model import (
        Encounter,
        Facility,
        NoteSection,
        Observation,
        ObservationCategory,
        Patient,
        PatientRecord,
        Practitioner,
        SectionKind,
    )

    patient = Patient(
        id="feedface-0000-0000-0000-0000000000aa",
        given_name="Synthia",
        family_name="Testpatient",
        birth_date=datetime.date(1985, 3, 14),
        sex="F",
    )
    facility = Facility(
        id="feedface-fac0-0000-0000-0000000000aa",
        name="Example Synthetic Clinic",
        address_line1="100 Placeholder Way",
        city="Springfield",
        state="WA",
        postal_code="98101",
        phone="(206) 555-0100",
    )
    provider = Practitioner(
        id="feedface-d0c0-0000-0000-0000000000aa",
        given_name="Pat",
        family_name="Provider",
        display_name="Dr. Pat Provider",
        credential="MD",
    )
    encounter = Encounter(
        id="feedface-e000-0000-0000-0000000000aa",
        patient_id=patient.id,
        facility_id=facility.id,
        provider_id=provider.id,
        signed_by_id=provider.id,
        date_of_service=datetime.date(2024, 1, 2),
        note_type="Progress Note",
        chief_complaint="Cough and congestion",
        signed_at=datetime.datetime(2024, 1, 2, 17, 30, tzinfo=datetime.UTC),
        sections=[
            NoteSection(
                kind=SectionKind.SUBJECTIVE,
                title="Subjective",
                text="Patient reports a productive cough for five days.",
            ),
            NoteSection(
                kind=SectionKind.OBJECTIVE,
                title="Objective",
                text="Lungs clear to auscultation bilaterally.",
            ),
            NoteSection(
                kind=SectionKind.ASSESSMENT,
                title="Assessment",
                text="Acute viral bronchitis.",
            ),
            NoteSection(
                kind=SectionKind.PLAN,
                title="Plan",
                text="Supportive care; return if symptoms persist.",
            ),
        ],
    )
    vitals = [
        Observation(
            id="feedface-0b50-0000-0000-0000000000a1",
            patient_id=patient.id,
            encounter_id=encounter.id,
            category=ObservationCategory.VITAL_SIGNS,
            code="8867-4",
            display="Heart rate",
            value="72",
            unit="bpm",
        ),
    ]
    return PatientRecord(
        id=patient.id,
        patient=patient,
        encounters=[encounter],
        facilities=[facility],
        practitioners=[provider],
        observations=vitals,
    )


def _render_preview(pack_dir: Path) -> Path | None:
    """Render one synthetic preview record through the draft pack.

    Returns the preview PDF path on success, or ``None`` when the Chromium
    renderer is unavailable (a draft still emitted — the operator can render
    later). PHI-safe by construction: only the synthetic preview record is used.
    """
    from anastomosis.reconstruct import discover_packs
    from anastomosis.reconstruct.engine import ReconstructionEngine

    try:
        from anastomosis.reconstruct.chromium import ChromiumRenderer
    except ImportError:
        console.print("[yellow]preview skipped[/yellow]: install anastomosis[render] for Chromium")
        return None
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            pw.chromium.launch().close()
    except Exception as exc:  # browser not fetched / cannot launch
        console.print(
            f"[yellow]preview skipped[/yellow]: Chromium unavailable ({type(exc).__name__}); "
            "run 'playwright install chromium'"
        )
        return None

    status = discover_packs([pack_dir.parent], allow_external=True).get(pack_dir.name)
    if status is None or status.pack is None:
        diagnosis = status.diagnosis if status else "draft pack not discovered"
        console.print(f"[red]preview failed:[/red] {diagnosis}")
        raise typer.Exit(code=1)
    manifest = status.pack.manifest
    margins = {
        "top": manifest.page.margin_top,
        "right": manifest.page.margin_right,
        "bottom": manifest.page.margin_bottom,
        "left": manifest.page.margin_left,
    }
    engine = ReconstructionEngine(
        status.pack,
        lambda: ChromiumRenderer(page_size=manifest.page.size, margins=margins),
    )
    preview_dir = pack_dir / "preview"
    result = engine.run([_synthetic_preview_record()], preview_dir)
    if result.failed or not result.documents:
        console.print(f"[red]preview render failed[/red] ({len(result.failed)} error(s))")
        raise typer.Exit(code=1)
    return result.documents[0].path


@pack_app.command("init")
def pack_init(
    samples: Annotated[
        list[str],
        typer.Option(
            "--from-samples",
            help="Sample PDFs: a directory, a glob (./samples/*.pdf), or files.",
        ),
    ],
    name: Annotated[
        str, typer.Option("--name", help="Pack name (lowercase identifier, e.g. acme_soap).")
    ],
    out_dir: Annotated[
        Path,
        typer.Option("--out-dir", help="Directory to write the pack into (default: ./packs)."),
    ] = Path("packs"),
    render_preview: Annotated[
        bool,
        typer.Option(
            "--render-preview/--no-render-preview",
            help="Render one synthetic preview record through the draft (needs Chromium).",
        ),
    ] = False,
    display: Annotated[
        str | None,
        typer.Option("--display", help="Human label for the source format (default: the name)."),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the interactive same-patient confirmation."),
    ] = False,
) -> None:
    """Learn a draft template pack from sample PDFs of an EHR's note format.

    Collects the samples, harvests + analyzes them (PHI-safe — only static
    template text is summarized), echoes the same-patient caveat for explicit
    confirmation, then writes a loadable DRAFT pack. The draft is a STARTING
    POINT: review the rendered preview against an original sample, edit the
    template, and re-render. Fidelity is not claimed.
    """
    from anastomosis.core.logutil import exc_tag
    from anastomosis.packgen import analyze, extract_samples
    from anastomosis.packgen.emit import SAME_PATIENT_CAVEAT, emit_draft_pack

    if not _PACK_NAME_RE.match(name):
        console.print(
            f"[red]invalid pack name {name!r}[/red] — use a lowercase identifier "
            "(letters, digits, underscores; starting with a letter)"
        )
        raise typer.Exit(code=2)

    pdfs = _collect_sample_pdfs(samples)
    if not pdfs:
        console.print(
            "[red]no sample PDFs found[/red] — pass --from-samples <dir>, a glob, or files"
        )
        raise typer.Exit(code=2)
    # PHI: log the COUNT only, never the sample paths (they may be named after
    # patients — the extract module's contract).
    console.print(f"Found [cyan]{len(pdfs)}[/cyan] sample PDF(s).")
    if len(pdfs) < _LOW_SAMPLE_FLOOR:
        console.print(
            f"[yellow]warning: only {len(pdfs)} sample(s)[/yellow] — confidence is LOW. "
            f"The static/per-patient text split needs >= {_LOW_SAMPLE_FLOOR} DISTINCT-patient "
            "samples to be reliable."
        )

    try:
        analysis = analyze(extract_samples(pdfs))
    except Exception as exc:  # unreadable/encrypted sample — type only, no path/PHI
        console.print(f"[red]analysis failed[/red] ({exc_tag(exc)})")
        raise typer.Exit(code=1) from None

    console.print("\n[bold]Inferred design[/bold] (PHI-safe summary):")
    for line in analysis.summary_lines():
        console.print(f"  {line}")

    console.print(f"\n[yellow]Same-patient caveat:[/yellow] {SAME_PATIENT_CAVEAT}")
    if not yes and not typer.confirm("Are these samples from DIFFERENT patients?", default=False):
        console.print("Aborting — gather samples from distinct patients and re-run.")
        raise typer.Exit(code=0)

    try:
        pack_dir = emit_draft_pack(analysis, name=name, display=display or name, out_dir=out_dir)
    except Exception as exc:
        console.print(f"[red]emit failed[/red] ({exc_tag(exc)})")
        raise typer.Exit(code=1) from None
    console.print(f"\n[green]wrote draft pack[/green] → {pack_dir}")

    preview_path: Path | None = None
    if render_preview:
        preview_path = _render_preview(pack_dir)
        if preview_path is not None:
            console.print(f"[green]preview[/green] → {preview_path}")

    console.print("\n[bold]Next steps[/bold] (see DRAFT.md):")
    if preview_path is not None:
        console.print(f"  1. Review {preview_path} against an original sample.")
    else:
        console.print("  1. Render a preview (--render-preview) and compare to an original sample.")
    console.print(
        f"  2. Edit {pack_dir / 'template.html'} (reposition unplaced static text, tokens)."
    )
    console.print(
        f"  3. Re-render:  anast pipeline run <export> -o out --pack {name} --pack-dir {out_dir}"
    )


if __name__ == "__main__":
    app()
