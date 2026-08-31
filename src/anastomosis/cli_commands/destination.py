"""``anast destination list`` / ``route`` / ``init`` — inspect routes, discover packs.

See :mod:`anastomosis.cli_commands` for the split/registration rationale. One
module-specific seam: the live selector-validator ``_make_validator`` is
resolved LATE through the ``cli`` module (``_cli._make_validator``) so the
wizard tests keep mocking it at ``cli._make_validator``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from anastomosis.cli import destination_app
from anastomosis.cli_commands._paths import in_file, out_dir
from anastomosis.core.presentation import as_typed


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

    from anastomosis import cli as _cli
    from anastomosis.destinations.registry import DestinationRegistry

    try:
        if registry is not None:
            return DestinationRegistry.merged(registry)
        return DestinationRegistry.load()
    except (ValidationError, YAMLError, OSError) as exc:
        where = f" {registry}" if registry is not None else ""
        _cli.console.print(
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


def _route_status(kind: str, *, verbose: bool) -> str:
    """Whether a route works, in the words the Migrate screen uses.

    The registry's own names for a route — ``vendor_rest``,
    ``fhir_documentreference``, ``in_product`` — answer "how", and the table was
    printing them as if that were the question. What a person choosing a
    destination needs first is whether the route is available at all;
    ``--verbose`` keeps the registry's name for anyone who needs to know which
    interface is involved.
    """
    if verbose:
        return kind
    # Three states, not two. `unverified` means nobody has checked this route
    # yet; `none` means it is not there. `destination route` has always drawn
    # that line ("not confirmed yet — this opens once it has been checked" vs
    # "not available") and the plain table erased it — on a registry whose
    # contract makes `unverified` a first-class, load-bearing state.
    if kind == "unverified":
        return "not checked"
    return "not available" if kind == "none" else "available"


@destination_app.command("list")
def destination_list(
    registry: Annotated[
        Path | None,
        typer.Option(
            "--registry",
            parser=in_file,
            help="Overlay registry file (entries override packaged ones of the same name).",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Name the interface behind each route."),
    ] = False,
) -> None:
    """List the systems charts can be filed into, and how each one accepts them."""
    from rich.console import Console
    from rich.table import Table

    from anastomosis.destinations.registry import DestinationEntry, DestinationRegistry

    reg = _load_registry(registry)
    assert isinstance(reg, DestinationRegistry)
    table = Table(title="Destination systems")
    table.add_column("name", style="cyan")
    table.add_column("System")
    table.add_column("Send directly")
    table.add_column("Import a transfer document")
    table.add_column("Through a browser")
    table.add_column("Filing assistant")
    table.add_column("Last checked")
    for name in sorted(reg.entries):
        entry: DestinationEntry = reg.entries[name]
        table.add_row(
            entry.name,
            entry.display,
            _route_status(entry.doc_write_api.kind, verbose=verbose),
            _route_status(entry.ccda_import.kind, verbose=verbose),
            _route_status(entry.browser.kind, verbose=verbose),
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
        typer.Option(
            "--registry",
            parser=in_file,
            help="Overlay registry file (entries override packaged ones of the same name).",
        ),
    ] = None,
) -> None:
    """Show how charts can reach this system; exit 1 if no route is available."""
    from anastomosis import cli as _cli
    from anastomosis.deliver.router import plan_route
    from anastomosis.destinations.registry import DestinationRegistry

    reg = _load_registry(registry)
    assert isinstance(reg, DestinationRegistry)
    try:
        transit = plan_route(name, reg)
    except KeyError as exc:
        # KeyError carries the known-names list (no PHI) — show it, not a traceback.
        _cli.console.print(f"[red]{exc.args[0] if exc.args else exc}[/red]")
        # Exit-code contract: 2 = unknown destination NAME (operator typo),
        # 1 = known destination with NO viable route (capability gap). Tests
        # pin both; scripts branch on them.
        raise typer.Exit(code=2) from None
    _cli.console.print(transit.render(_cli._glyphs()))
    # Surface a locally present browser pack WITHOUT auto-flipping routing: the
    # registry overlay remains the single routing truth, so we only note that a
    # pack exists and how the operator declares it.
    pack_status = _local_pack_status(name)
    if pack_status != "—" and transit.options[-1].kind.value == "browser":
        if not transit.options[-1].viable:
            _cli.console.print(
                f"note: browser pack present locally ({pack_status}) — declare it in your "
                "registry overlay (kind: pack) to route through it"
            )
        else:
            _cli.console.print(f"note: browser pack present locally ({pack_status})")
    if transit.chosen is None:
        raise typer.Exit(code=1)


# The maximum re-entry attempts for a not-found selector under --validate before
# the operator must either accept it unvalidated or give up.
_VALIDATE_MAX_TRIES = 3


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
    from anastomosis import cli as _cli
    from anastomosis.destinations.wizard import SelectorValidator

    label = "required" if required else "optional, blank to skip"
    for attempt in range(1, _VALIDATE_MAX_TRIES + 1):
        raw: str = as_typed(typer.prompt(f"  {slot} ({label}) — {guidance}", default=""))
        value = raw.strip()
        if not value:
            if not required:
                return ""
            _cli.console.print("    [yellow]a value is required[/yellow]")
            continue
        if validator is None:
            return value
        assert isinstance(validator, SelectorValidator)
        count = validator.count(value)
        if count >= 1:
            _cli.console.print(f"    [green]found {count} element(s)[/green]")
            return value
        _cli.console.print(f"    [yellow]selector matched 0 elements[/yellow] (try {attempt})")
        if attempt < _VALIDATE_MAX_TRIES:
            continue
        if typer.confirm("    accept this unvalidated selector anyway?", default=False):
            return value
    # Exhausted tries without acceptance: re-raise as an explicit operator abort.
    _cli.console.print(f"[red]gave up discovering {slot!r} (no matching selector)[/red]")
    raise typer.Exit(code=1)


@destination_app.command("init")
def destination_init(
    name: Annotated[str, typer.Argument(help="Which filing assistant to set up, e.g. tebra.")],
    out_dir: Annotated[
        Path | None,
        typer.Option(
            "--out-dir",
            help="Where to save what it learns (default: your user folder).",
            parser=out_dir,
        ),
    ] = None,
    validate: Annotated[
        bool,
        typer.Option("--validate", help="Check each selector against a live page (needs --cdp)."),
    ] = False,
    cdp: Annotated[
        str | None,
        typer.Option(
            "--cdp",
            help="The browser on this computer to work through, e.g. http://127.0.0.1:9222.",
        ),
    ] = None,
    pack_dir: Annotated[
        list[Path] | None,
        typer.Option("--pack-dir", help="Another folder to look for the starting point in."),
    ] = None,
) -> None:
    """Teach Anastomosis where things are on your destination's pages.

    Asks you to point out each thing it needs to find — the search box, the
    patient name, the upload button — taking the required ones first. With
    ``--validate --cdp`` it checks each answer against the browser you have
    open, so a wrong answer is caught now rather than mid-run. What it learns
    is saved in your own user folder.

    What ships with Anastomosis is never changed. It prints a short block of
    text for you to paste into your own settings, which is what tells
    Anastomosis this system is now ready to file into.
    """
    from anastomosis import cli as _cli
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
        _cli.console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from None

    validator: object | None = None
    if validate:
        if cdp is None:
            _cli.console.print("[red]--validate requires --cdp (a loopback CDP endpoint)[/red]")
            raise typer.Exit(code=2)
        _cli.console.print(SHARED_MACHINE_WARNING)
        try:
            validator = _cli._make_validator(cdp)
        except Exception as exc:  # attach/launch failure — name the type, no PHI
            _cli.console.print(f"[red]could not attach for validation ({type(exc).__name__})[/red]")
            raise typer.Exit(code=2) from None
    elif cdp is not None:
        # --cdp without --validate: still warn (a debug port was named).
        _cli.console.print(SHARED_MACHINE_WARNING)
    else:
        _cli.console.print(
            "[yellow]selectors accepted as-is[/yellow] — preflight validates them at run time"
        )

    _cli.console.print(f"Discovering selectors for [cyan]{loaded.name}[/cyan]:")
    discovered: dict[str, str] = {}
    _cli.console.print("[bold]Required[/bold] — filing cannot run without these:")
    for slot in SelectorMap.required_slots():
        discovered[slot] = _prompt_slot(
            slot, required=True, guidance=SLOT_GUIDANCE.get(slot, ""), validator=validator
        )
    # The optional block is the longer of the two, and most of it is the upload
    # dialog's own fields — which plenty of systems simply do not show. Saying
    # so once, up front, is what keeps a run of blank answers from reading as
    # something having gone wrong.
    _cli.console.print("[bold]Optional[/bold] — press Enter to skip any you do not see:")
    for slot in SelectorMap.optional_slots():
        discovered[slot] = _prompt_slot(
            slot, required=False, guidance=SLOT_GUIDANCE.get(slot, ""), validator=validator
        )

    target_root = out_dir or user_destinations_dir()
    written = write_selectors(loaded.name, discovered, target_root)
    _cli.console.print(f"[green]wrote[/green] {written}")
    _cli.console.print(
        "\nNext steps — declare this pack in your registry overlay (NOT the packaged one):"
    )
    _cli.console.print(registry_overlay_snippet(loaded.name))
    _cli.console.print(
        f"Then route it:  anast destination route {loaded.name} --registry <your-overlay>.yaml"
    )
