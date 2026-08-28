"""``anast migrate`` — EHR-to-EHR migration (PF->Tebra is one instance).

See :mod:`anastomosis.cli_commands` for the split/registration rationale.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import typer

from anastomosis.cli import app
from anastomosis.cli_commands._options import ExportDir, Force, OutDir, PackDirs, TrustPack

if TYPE_CHECKING:
    from anastomosis.core.migrate import MigrationCommand


def _resolve_migration_profile(
    profile_name: str | None,
    *,
    source: str | None,
    destination: str | None,
    render: str | None,
    section: list[str] | None,
    qa: bool | None,
) -> tuple[str, str, str, dict[str, bool], bool]:
    """Resolve the migration config from a saved profile + explicit overrides.

    A ``--profile`` supplies defaults for source/destination/render/sections/qa;
    any explicitly-typed flag overrides it. Loud, PHI-safe failures (a missing
    profile, a profile lacking the required fields) become a clean exit 2 rather
    than a traceback. Returns the resolved ``(source, destination, render,
    sections, qa)``.
    """
    from anastomosis import cli as _cli
    from anastomosis.core.migrate import RENDER_NEUTRAL, default_migration_profiles

    saved: dict[str, object] = {}
    if profile_name is not None:
        store = default_migration_profiles()
        loaded = store.get(profile_name)
        if loaded is None:
            _cli.console.print(
                f"[red]no saved migration profile {profile_name!r}[/red] "
                f"(have: {', '.join(store.names()) or 'none'})"
            )
            raise typer.Exit(code=2)
        saved = loaded

    # Explicit flags win over the profile; the profile fills the rest.
    resolved_source = source if source is not None else saved.get("source")
    resolved_destination = destination if destination is not None else saved.get("destination")
    if not isinstance(resolved_source, str) or not resolved_source:
        _cli.console.print("[red]--from is required[/red] (or supply it via --profile).")
        raise typer.Exit(code=2)
    if not isinstance(resolved_destination, str) or not resolved_destination:
        _cli.console.print("[red]--to is required[/red] (or supply it via --profile).")
        raise typer.Exit(code=2)

    if render is not None:
        resolved_render = render
    elif isinstance(saved.get("render"), str):
        resolved_render = str(saved["render"])
    else:
        resolved_render = RENDER_NEUTRAL

    saved_sections = saved.get("sections")
    if section is not None:
        resolved_sections = _cli._sections_or_exit(
            section, source=resolved_source, pack=resolved_render
        )
    elif isinstance(saved_sections, dict):
        resolved_sections = {str(k): bool(v) for k, v in saved_sections.items()}
    else:
        resolved_sections = {}

    if qa is not None:
        resolved_qa = qa
    elif isinstance(saved.get("qa"), bool):
        resolved_qa = bool(saved["qa"])
    else:
        resolved_qa = True

    return resolved_source, resolved_destination, resolved_render, resolved_sections, resolved_qa


def _run_migration(cmd: MigrationCommand, save_profile: str | None) -> None:
    """Run a :class:`MigrationCommand`, presenting it as the CLI presents a pipeline.

    Prints the transit map FIRST (the route is the headline of a migration),
    then runs :func:`run_migration` translating its events with the SAME printer
    ``anast pipeline run`` uses, then the chart + C-CDA outcome lines. On
    :class:`PipelineError` it reproduces ``_report_pipeline_error`` +
    ``typer.Exit``; on success, ``--save-profile`` persists the resolved config.
    Ends on the shared verdict: the prepared notice (route resolved, delivery not
    executed — exit 0) or the manual-import notice (no viable route — exit 1).
    """
    from anastomosis import cli as _cli
    from anastomosis.core.migrate import default_migration_profiles, run_migration
    from anastomosis.core.migration_status import (
        classify_migration,
        manual_import_notice,
        prepared_notice,
    )
    from anastomosis.pipeline import PipelineError

    # Resolve and SURFACE the route before running — a migration is a route move.
    try:
        from anastomosis.deliver.router import plan_route
        from anastomosis.destinations.registry import DestinationRegistry

        transit = plan_route(cmd.destination, DestinationRegistry.load())
    except KeyError as exc:
        _cli.console.print(f"[red]{exc.args[0] if exc.args else exc}[/red]")
        raise typer.Exit(code=2) from None
    _cli.console.print(transit.render(_cli._glyphs()))

    charts_dir = cmd.out_dir / "charts"
    _print_event = _cli._make_event_printer(source=cmd.source, charts_dir=charts_dir)
    try:
        result = run_migration(cmd, on_event=_print_event)
    except PipelineError as exc:
        _cli._report_pipeline_error(exc, source=cmd.source, pack=cmd.render)
        raise typer.Exit(code=exc.exit_code) from None

    # ccda-standard renders no pipeline reconstruct/QA events (it runs
    # document-generic QA — layout/pagination + integrity — per patient and
    # records pack-driven checks as skipped; another agent is implementing that),
    # so report what it produced (the per-patient view PDFs) explicitly.
    if result.ccda_view is not None:
        view = result.ccda_view
        _cli.console.print(
            f"[green]{len(view.documents)} rendered[/green], "
            f"{len(view.skipped)} skipped, 0 failed {_cli._glyphs().arrow} {charts_dir}"
        )
    _cli._print_delivery(result.ccda_export)

    if save_profile is not None:
        default_migration_profiles().save(
            save_profile,
            {
                "source": cmd.source,
                "destination": cmd.destination,
                "render": cmd.render,
                "sections": dict(cmd.sections),
                "qa": cmd.qa,
            },
        )
        _cli.console.print(f"[green]saved migration profile[/green] {save_profile!r}")

    # The verdict below comes from the SAME shared classifier the GUI consumes,
    # so the frontends never drift.
    status = classify_migration(result)
    if status.needs_manual_import:
        # No viable AUTOMATED route to the destination (a known destination whose
        # capabilities offer none). The structured C-CDA + charts ARE written (the
        # C-CDA is the universal manual-import format), but make the gap loud and
        # exit 1 — consistent with `destination route`, never a silent exit-0 —
        # and point at the discovery wizard for a browser route.
        _cli.console.print(f"[yellow]{manual_import_notice(status)}[/yellow]")
        raise typer.Exit(code=status.exit_code)

    # A route resolved: the artifacts + the VERIFIED route plan are written, but
    # `migrate` executes no delivery route — a chosen route is a plan, not proof
    # a chart landed. Print the prepared notice (neutral, not the success-silent
    # path) so the operator sees delivery is still theirs to run, and keep exit 0
    # (preparation succeeded — the exit contract scripts rely on), and it is
    # NEVER `delivered` (that needs a receipt no executor yet produces).
    _cli.console.print(f"[cyan]{prepared_notice(status)}[/cyan]")


@app.command("migrate")
def migrate_cmd(
    export_dir: ExportDir,
    out: OutDir,
    source: Annotated[
        str | None,
        typer.Option(
            "--from", "-f", help="Source adapter to migrate FROM (e.g. pf-tebra). Required."
        ),
    ] = None,
    destination: Annotated[
        str | None,
        typer.Option(
            "--to", "-t", help="Destination to migrate TO (a registry name, e.g. tebra). Required."
        ),
    ] = None,
    render: Annotated[
        str | None,
        typer.Option(
            "--render",
            help="Chart representation: 'neutral' (default), 'ccda-standard', or a pack name.",
        ),
    ] = None,
    pack_dir: PackDirs = None,
    trust_pack: TrustPack = False,
    force: Force = False,
    section: Annotated[
        list[str] | None,
        typer.Option("--section", help="Override a section flag (pack renders only)."),
    ] = None,
    qa: Annotated[
        bool | None,
        typer.Option("--qa/--no-qa", help="Verify every rendered document (default on)."),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="Load a saved migration profile for source/to/render/etc."),
    ] = None,
    save_profile: Annotated[
        str | None,
        typer.Option(
            "--save-profile", help="Persist the resolved config under this name on success."
        ),
    ] = None,
) -> None:
    """Migrate records from one EHR to another (PF->Tebra is one instance).

    Emits BOTH the structured C-CDA payload the destination imports (``<out>/ccda``)
    and a human-readable chart archive (``<out>/charts``) in the chosen
    representation, after surfacing the destination's transit map. ``--render``
    selects the chart representation: ``neutral`` (the generic SOAP pack),
    ``ccda-standard`` (HL7's standard C-CDA view), or any pack name.
    """
    from anastomosis.core.migrate import MigrationCommand

    resolved = _resolve_migration_profile(
        profile,
        source=source,
        destination=destination,
        render=render,
        section=section,
        qa=qa,
    )
    src, dest, render_mode, sections, qa_on = resolved
    _run_migration(
        MigrationCommand(
            export_dir=export_dir,
            out_dir=out,
            source=src,
            destination=dest,
            render=render_mode,
            pack_dirs=tuple(pack_dir or ()),
            trust_new=trust_pack,
            force=force,
            sections=sections,
            qa=qa_on,
        ),
        save_profile,
    )
