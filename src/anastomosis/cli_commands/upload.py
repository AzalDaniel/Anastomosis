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
        typer.Option("--to", "-t", help="Browser route: destination pack name (e.g. tebra)."),
    ] = None,
    cdp: Annotated[
        str | None,
        typer.Option(
            "--cdp", help="Browser route: loopback CDP endpoint, e.g. http://127.0.0.1:9222."
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
        typer.Option("--pack-dir", help="Extra directories to find the destination pack in."),
    ] = None,
    verify: Annotated[
        bool,
        typer.Option(
            "--verify/--no-verify",
            help=(
                "Run the L0-L6 verification ladder around each upload (ON by "
                "default; needs the render extra and fails closed without it). "
                "Pass --no-verify to file WITHOUT the ladder — the engine's "
                "wrong-patient banner check still runs either way."
            ),
        ),
    ] = True,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the shared-machine attach confirmation."),
    ] = False,
) -> None:
    """File reconstructed charts into a destination EHR, by browser or by API.

    Reads the upload manifest written by `anast pipeline run --upload-manifest`
    or `anast migrate`, then drives the crash-resumable upload engine over
    exactly ONE delivery route:

    BROWSER (--to PACK --cdp URL) files each chart through the destination's web
    UI, in a browser YOU launched with a remote debug port and logged into
    yourself. The CDP attach is loopback-only and refuses any other host; the
    shared-machine warning is shown (and confirmed unless --yes) before any
    browser is touched. Anastomosis NEVER stores your EHR credentials and NEVER
    closes your browser — you log in by hand and the attach ends when you close it.

    API (--fhir URL) files each chart as a FHIR R4 DocumentReference over HTTPS
    (plain http is allowed only for a loopback host). The bearer token is read
    from an ENVIRONMENT VARIABLE — --fhir-token-env, default ANAST_FHIR_TOKEN —
    so it never appears in argv or in your shell history; an unset variable means
    unauthenticated. No browser is attached, so nothing is asked to confirm.

    Both routes share the skiplist, the retry budget, the L0-L6 verification
    ladder, and the resume-safe ledger. The manifest, ledger, and run report all
    stay inside the 0700 output dir.
    """
    # NB: the docstring's FIRST line is this command's SHORT help, so it is also
    # rendered into the top-level ``anast --help`` table — which
    # tests/unit/test_cli_help_encoding.py renders through a strict cp1252
    # console. Keep that line plain ASCII (no em dash); the body below is only
    # rendered by ``anast upload --help``, where the house style's em dashes are
    # fine. The note lives in a comment, not in the help text an operator reads.
    from rich.markup import escape as _escape

    from anastomosis import cli as _cli
    from anastomosis.core.upload_command import (
        UploadCommand,
        VerificationUnavailableError,
        resolve_manifest_root,
        run_upload_command,
    )
    from anastomosis.deliver.browser.cdp import SHARED_MACHINE_WARNING, CdpEndpoint
    from anastomosis.deliver.browser.manifest import load_skiplist
    from anastomosis.deliver.browser.persist import ManifestError, read_upload_manifest
    from anastomosis.deliver.fhir_api.client import FhirEndpoint
    from anastomosis.destinations.browserpack import PackNotReadyError
    from anastomosis.destinations.loader import BrowserPackError, load_destination_pack

    # 1. Route selection FIRST — it is pure argv, so it costs nothing and a
    #    mis-typed invocation never reaches the disk or the network. EXACTLY one
    #    route may be selected: a half-specified browser route (--to without
    #    --cdp) or two routes at once is an operator-input error (exit 2, no
    #    traceback), never a partially-configured run.
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

    # 2. Validate the manifest (cheap, pre-attach), so a missing/malformed one
    #    fails fast (exit 2) BEFORE the operator confirms the attach. Try the
    #    dir itself, then a <dir>/charts subdir (migrate's layout). The
    #    AUTHORITATIVE read happens inside run_upload_command under the output
    #    lock (lock-then-read), so this early copy is validation only.
    try:
        read_upload_manifest(resolve_manifest_root(out_dir))
    except ManifestError as exc:
        _cli.console.print(f"[red]{_escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from None

    # 3. The route's own pre-flight, then its attach seam. Both seams are
    #    resolved LATE through the cli module so the monkeypatch seams hold; both
    #    return a Destination, so step 5 below is route-agnostic.
    attach: Callable[[], object]
    if fhir is not None:
        # 3a. The transport gate — BEFORE any request is sent. FhirEndpoint
        #     enforces https (http only for a loopback host, the cdp.py rule)
        #     because a base URL carries the bearer token and patient
        #     identifiers; a rejected URL is a clean exit 2, not a traceback.
        #     The token is read from the ENVIRONMENT, never from argv (which is
        #     ps-visible); an unset/blank variable means unauthenticated, which
        #     is the normal case for a local HAPI server. Surrounding whitespace
        #     is stripped: a trailing newline from `export TOKEN=$(cat file)`
        #     would otherwise be rejected as an illegal HTTP header value.
        bearer_token = os.environ.get(fhir_token_env, "").strip() or None
        try:
            FhirEndpoint(fhir, bearer_token=bearer_token)
        except ValueError as exc:
            _cli.console.print(f"[red]{_escape(str(exc))}[/red]")
            raise typer.Exit(code=2) from None
        # NO shared-machine warning and no attach confirmation here: this route
        # touches no browser, so there is no session for a bystander to inherit
        # and nothing for the operator to accept (--yes is inert on this route).
        base_url = fhir  # rebound as a plain str for the closure below

        def _attach_api() -> object:
            return _cli._make_fhir_destination(
                base_url,
                bearer_token=bearer_token,
                create_missing_patients=create_patients,
            )

        attach = _attach_api
    else:
        # The route gate in step 1 guarantees both browser flags are present.
        assert to is not None and cdp is not None
        cdp_url = cdp

        # 3b. The loopback gate — BEFORE any browser touch.
        try:
            CdpEndpoint(cdp_url)
        except ValueError as exc:
            _cli.console.print(f"[red]{_escape(str(exc))}[/red]")
            raise typer.Exit(code=2) from None

        # 3c. Surface the shared-machine warning and confirm (unless --yes).
        #     --yes still PRINTS the warning — the operator is told what they
        #     accepted.
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

    # 5. Drive the engine through the SHARED upload command: it harden-locks the
    #    output dir, reads the manifest UNDER the lock (lock-then-read), then
    #    attaches the destination (the only browser/network touch — the injectable
    #    seam) and drives recover -> run -> finish -> report. A locked dir, a
    #    manifest that vanished after the pre-flight, or any other unexpected
    #    drive failure is a clean exit 2 named by exception TYPE only (no PHI, no
    #    traceback); a process-kill BaseException sails through to resume on the
    #    next run.
    from anastomosis.core.locking import OutputLockedError
    from anastomosis.deliver.browser.reports import summary_line

    cmd = UploadCommand(
        out_dir=out_dir, skiplist=skiplist_set, max_attempts=max_attempts, verify=verify
    )
    try:
        result = run_upload_command(cmd, attach)
    except VerificationUnavailableError as exc:
        # Fail closed: verification was requested but its dependency is missing.
        _cli.console.print(f"[red]{_escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from None
    except OutputLockedError as exc:
        _cli.console.print(f"[red]{_escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from None
    except ManifestError as exc:
        _cli.console.print(f"[red]{_escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from None
    except Exception as exc:
        _cli.console.print(
            f"[red]could not attach or drive the upload ({type(exc).__name__})[/red]"
        )
        raise typer.Exit(code=2) from None
    _cli.console.print(summary_line(result.counts))
    _cli.console.print(f"run report {_cli._glyphs().arrow} {result.report_path}")
    # The verdict (0 on a clean landing, 1 on abort/any non-clean terminal) is
    # the SHARED classifier on the result, so the CLI exit and the GUI's
    # done-vs-error branch cannot drift.
    if result.exit_code != 0:
        raise typer.Exit(code=result.exit_code)
