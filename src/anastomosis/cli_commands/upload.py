"""``anast upload`` — drive the resumable upload engine over ONE delivery route.

See :mod:`anastomosis.cli_commands` for the split/registration rationale. Both
attach seams are resolved LATE through the ``cli`` module — the browser route's
``_cli._make_destination`` (Playwright over CDP) and the API route's
``_cli._make_fhir_destination`` (FHIR R4 over HTTPS) — so
``monkeypatch.setattr(cli, "_make_destination", ...)`` and
``monkeypatch.setattr(cli, "_make_fhir_destination", ...)`` keep driving this
command with no browser and no server.

Exactly ONE route runs per invocation: ``--to PACK --cdp URL`` (browser) or
``--fhir URL`` (API). Only the pre-flight differs — the loopback gate, the
shared-machine confirmation and the pack-readiness check belong to the browser
route; the https-or-loopback gate and the env-var token belong to the API route.
Everything downstream of the attach seam is shared: the skiplist, the retry
budget, the L0-L6 verification ladder, the ledger, the run report, and the
exit-code rule, because both routes hand :func:`run_upload_command` the same
:class:`~anastomosis.destinations.base.Destination` protocol.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from anastomosis.cli import app
from anastomosis.cli_commands._paths import in_file
from anastomosis.core.outcome import declined
from anastomosis.core.upload_command import DEFAULT_MAX_ATTEMPTS
from anastomosis.deliver.fhir_api.attach import DEFAULT_TOKEN_ENV

if TYPE_CHECKING:
    from collections.abc import Callable

    from anastomosis.core.upload_command import UploadCommand, UploadCommandResult


def _drive_or_exit(cmd: UploadCommand, attach: Callable[[], object]) -> UploadCommandResult:
    """Drive the shared upload command, turning every failure into a clean exit 2.

    Extracted from ``upload_cmd`` so the command reads as its five numbered
    steps rather than as a wall of ``except`` clauses, and so a new refusal
    (the delivery gate) is one clause here instead of another branch in a
    function the complexity ratchet already holds at its ceiling.

    Four failures get their message printed VERBATIM because each one is
    PHI-free by its own module's contract and each names its own remedy: the
    missing verification dependency, a locked output directory, a bundle the
    delivery gate refused, and a malformed manifest. Anything else is reported
    by exception TYPE only — no message, no traceback — because nothing has
    promised what an arbitrary failure's text contains. A ``BaseException``
    (a process kill) is deliberately not caught: the run resumes next time.
    """
    from rich.markup import escape as _escape

    from anastomosis import cli as _cli
    from anastomosis.core.locking import OutputLockedError
    from anastomosis.core.upload_command import (
        VerificationUnavailableError,
        run_upload_command,
    )
    from anastomosis.deliver.browser.gates import DeliveryRefused
    from anastomosis.deliver.browser.persist import ManifestError

    try:
        return run_upload_command(cmd, attach)
    except (
        VerificationUnavailableError,
        OutputLockedError,
        DeliveryRefused,
        ManifestError,
    ) as exc:
        _cli.console.print(f"[red]{_escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from None
    except Exception as exc:
        _cli.console.print(
            f"[red]could not attach or drive the upload ({type(exc).__name__})[/red]"
        )
        raise typer.Exit(code=2) from None


@app.command("upload")
def upload_cmd(
    out_dir: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            readable=True,
            help="A render/migrate output dir holding upload_manifest.json.",
        ),
    ],
    to: Annotated[
        str | None,
        typer.Option("--to", "-t", help="Through a browser: which filing assistant (e.g. tebra)."),
    ] = None,
    cdp: Annotated[
        str | None,
        typer.Option(
            "--cdp",
            help="Through a browser: the browser on this computer to work through, "
            "e.g. http://127.0.0.1:9222.",
        ),
    ] = None,
    fhir: Annotated[
        str | None,
        typer.Option(
            "--fhir",
            help=(
                "API route: destination FHIR R4 base URL (https, or http only for a loopback host)."
            ),
        ),
    ] = None,
    fhir_token_env: Annotated[
        str,
        typer.Option(
            "--fhir-token-env",
            help=(
                "API route: the ENVIRONMENT VARIABLE holding the bearer token, "
                "so it never appears in argv. Unset means unauthenticated."
            ),
        ),
    ] = DEFAULT_TOKEN_ENV,
    create_patients: Annotated[
        bool,
        typer.Option(
            "--create-patients/--no-create-patients",
            help=(
                "API route: create a destination Patient when none matches (ON "
                "by default — a migration target may not hold the patients yet). "
                "A search that matches MORE than one is always refused."
            ),
        ),
    ] = True,
    search_by_ssn: Annotated[
        bool,
        typer.Option(
            "--search-by-ssn/--no-search-by-ssn",
            help=(
                "API route: look a patient up by their SSN when they carry no "
                "other identifier (OFF by default — a search parameter rides in "
                "the URL query string, which the destination and any proxy log). "
                "Off, such a patient is not found: with --create-patients that "
                "makes a duplicate, which is visible and fixable."
            ),
        ),
    ] = False,
    skiplist: Annotated[
        Path | None,
        typer.Option(
            "--skiplist",
            parser=in_file,
            help="File of item_key/encounter_id lines to exclude.",
        ),
    ] = None,
    max_attempts: Annotated[
        int, typer.Option("--max-attempts", help="Retry budget per item before FAILED.")
    ] = DEFAULT_MAX_ATTEMPTS,
    pack_dir: Annotated[
        list[Path] | None,
        typer.Option("--pack-dir", help="Another folder to look for filing assistants in."),
    ] = None,
    verify: Annotated[
        bool,
        typer.Option(
            "--verify/--no-verify",
            help=(
                "Double-check each chart after filing: confirm the right chart "
                "landed on the right patient before moving on. ON by default, "
                "and it refuses to run rather than skip the check if the parts "
                "it needs are not installed. --no-verify files without that "
                "check; the wrong-patient warning still stops a run either way."
            ),
        ),
    ] = True,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the shared-machine attach confirmation."),
    ] = False,
) -> None:
    """File finished charts into another EHR, one of two ways.

    Reads what `anast pipeline run --upload-manifest` or `anast migrate`
    prepared, then files each chart by ONE of these routes. A run that stops
    part way can be started again and picks up where it left off.

    THROUGH A BROWSER (--to ASSISTANT --cdp URL). Anastomosis files each chart
    through the destination's own web pages, in a browser YOU opened and signed
    into. It connects only to a browser on this computer and refuses any other
    address. Before it touches the browser it warns you about shared machines
    and waits for you to confirm, unless you pass --yes. Anastomosis NEVER
    stores your sign-in, and NEVER closes your browser — you sign in by hand,
    and the connection ends when you close it.

    DIRECTLY TO THE DESTINATION'S FHIR INTERFACE (--fhir URL), over HTTPS —
    plain http only for an address on this computer. The sign-in token is read
    from an ENVIRONMENT VARIABLE (--fhir-token-env, default ANAST_FHIR_TOKEN),
    so it never appears in the command you type or in your shell history. If
    that variable is unset, no token is sent. No browser is involved, so
    nothing is asked to confirm.

    Either way: the skip list, how many times a chart is retried, and the
    double-check after each chart all work the same. Everything the run writes
    stays in the results folder, which is created readable only by you.
    """
    # The docstring's FIRST line is also the top-level `anast --help` table's
    # short help, rendered through a strict cp1252 console
    # (test_cli_help_encoding.py): keep that one line plain ASCII.
    from rich.markup import escape as _escape

    from anastomosis import cli as _cli
    from anastomosis.core.upload_command import UploadCommand, resolve_manifest_root
    from anastomosis.deliver.browser.cdp import SHARED_MACHINE_WARNING, CdpEndpoint
    from anastomosis.deliver.browser.manifest import load_skiplist
    from anastomosis.deliver.browser.persist import ManifestError, read_upload_manifest
    from anastomosis.deliver.fhir_api.client import FhirEndpoint
    from anastomosis.destinations.browserpack import PackNotReadyError
    from anastomosis.destinations.loader import BrowserPackError, load_destination_pack

    # 1. Route selection FIRST — pure argv, so a mis-typed invocation never
    #    reaches the disk or the network. Exactly one route may be selected.
    if fhir is not None and (to is not None or cdp is not None):
        _cli.console.print(
            "[red]choose ONE upload route: --to PACK --cdp URL (browser) or "
            "--fhir URL (API), not both[/red]"
        )
        raise typer.Exit(code=2)
    if fhir is None and (to is None or cdp is None):
        _cli.console.print(
            "[red]choose an upload route: --to PACK --cdp URL (browser) or --fhir URL (API)[/red]"
        )
        raise typer.Exit(code=2)

    # 2. Validate the manifest (cheap, pre-attach) before the operator confirms
    #    the attach. The authoritative read happens under the output lock
    #    inside run_upload_command; this copy is validation only.
    try:
        read_upload_manifest(resolve_manifest_root(out_dir))
    except ManifestError as exc:
        _cli.console.print(f"[red]{_escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from None

    # 3. The route's own pre-flight, then its attach seam, both resolved LATE
    #    through the cli module so the monkeypatch seams hold; both return a
    #    Destination, so step 5 below is route-agnostic.
    attach: Callable[[], object]
    if fhir is not None:
        # 3a. Transport gate before any request: https only (loopback excepted),
        #     because the base URL carries the bearer token and patient ids.
        #     Token comes from the ENVIRONMENT, never argv (ps-visible); a
        #     trailing newline from `export TOKEN=$(cat file)` is stripped, or
        #     it would be rejected as an illegal HTTP header value.
        bearer_token = os.environ.get(fhir_token_env, "").strip() or None
        try:
            FhirEndpoint(fhir, bearer_token=bearer_token)
        except ValueError as exc:
            _cli.console.print(f"[red]{_escape(str(exc))}[/red]")
            raise typer.Exit(code=2) from None
        # No shared-machine warning or confirmation: this route touches no
        # browser, so --yes is inert here.
        base_url = fhir  # rebound as a plain str for the closure below

        def _attach_api() -> object:
            return _cli._make_fhir_destination(
                base_url,
                bearer_token=bearer_token,
                create_missing_patients=create_patients,
                search_by_ssn=search_by_ssn,
            )

        attach = _attach_api
    else:
        # The route gate in step 1 guarantees both browser flags are present.
        assert to is not None and cdp is not None
        cdp_url = cdp

        # 3b. The loopback gate — before any browser touch.
        try:
            CdpEndpoint(cdp_url)
        except ValueError as exc:
            _cli.console.print(f"[red]{_escape(str(exc))}[/red]")
            raise typer.Exit(code=2) from None

        # 3c. --yes still PRINTS the warning: the operator is told what they accepted.
        _cli.console.print(SHARED_MACHINE_WARNING)
        prompt = "Connect to this browser and start filing?"
        if not yes and not typer.confirm(prompt, default=False):
            _cli.console.print("aborted")
            declined("No charts were filed.")
            raise typer.Exit(code=0)

        # 3d. Load the destination pack and gate on readiness (selectors found).
        try:
            loaded = load_destination_pack(to, list(pack_dir or []))
        except BrowserPackError as exc:
            _cli.console.print(f"[red]{_escape(str(exc))}[/red]")
            raise typer.Exit(code=2) from None
        if not loaded.ready:
            try:
                loaded.require_selectors()
            except PackNotReadyError as exc:
                _cli.console.print(f"[red]{_escape(str(exc))}[/red]")
                raise typer.Exit(code=2) from None

        def _attach_browser() -> object:
            return _cli._make_destination(cdp_url, loaded)

        attach = _attach_browser

    # 4. Load the operator skiplist if given (a missing file raises -> exit 2).
    skiplist_set: frozenset[str] = frozenset()
    if skiplist is not None:
        try:
            skiplist_set = load_skiplist(skiplist)
        except OSError as exc:
            _cli.console.print(f"[red]could not read skiplist ({type(exc).__name__})[/red]")
            raise typer.Exit(code=2) from None

    # 5. Drive the engine through the shared upload command: it harden-locks
    #    the output dir, reads the manifest under that lock, attaches the
    #    destination, and drives recover -> run -> finish -> report. Any
    #    unexpected drive failure is a clean exit 2 named by exception TYPE
    #    only; a process-kill BaseException sails through to resume next time.
    from anastomosis.deliver.browser.reports import summary_line

    result = _drive_or_exit(
        UploadCommand(
            out_dir=out_dir, skiplist=skiplist_set, max_attempts=max_attempts, verify=verify
        ),
        attach,
    )
    _cli.console.print(summary_line(result.counts))
    _cli.console.print(f"run report {_cli._glyphs().arrow} {result.report_path}")
    # Shared classifier: the CLI exit and the GUI's done-vs-error branch can't drift.
    if result.exit_code != 0:
        raise typer.Exit(code=result.exit_code)
