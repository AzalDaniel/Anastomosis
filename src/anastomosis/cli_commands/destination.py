# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""``anast destination list`` / ``route`` / ``init`` — inspect routes, discover packs.

The three command bodies and their helpers (registry load, oldest-evidence and
local-pack-status columns, the selector-slot prompt) split out of
:mod:`anastomosis.cli`. They register against the ``destination_app`` defined
there; ``console`` / ``_glyphs`` resolve late through the ``cli`` module, and the
live selector-validator seam ``_make_validator`` is resolved LATE through it too
(``_cli._make_validator``) so the wizard tests keep mocking it at ``cli._make_validator``.
See :mod:`anastomosis.cli_commands` for the facade rationale.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from anastomosis.cli import destination_app


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
        raw: str = typer.prompt(f"  {slot} ({label}) — {guidance}", default="")
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
    _cli.console.print(f"[green]wrote[/green] {written}")
    _cli.console.print(
        "\nNext steps — declare this pack in your registry overlay (NOT the packaged one):"
    )
    _cli.console.print(registry_overlay_snippet(loaded.name))
    _cli.console.print(
        f"Then route it:  anast destination route {loaded.name} --registry <your-overlay>.yaml"
    )
