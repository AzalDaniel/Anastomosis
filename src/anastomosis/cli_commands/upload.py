# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""``anast upload`` — drive the resumable browser engine over a CDP attach.

The command body and its exit-code rule split out of :mod:`anastomosis.cli`. It
registers against the top-level ``app`` defined there; ``console`` / ``_glyphs``
resolve late through the ``cli`` module, and the Playwright-attach seam
``_make_destination`` is resolved LATE through it too (``_cli._make_destination``)
so ``monkeypatch.setattr(cli, "_make_destination", ...)`` keeps driving this
command with no browser. See :mod:`anastomosis.cli_commands` for the rationale.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from anastomosis.cli import app
from anastomosis.core.upload_command import DEFAULT_MAX_ATTEMPTS

# Terminal states that count as a clean landing (an item reached a safe end).
# Anything else terminal — FAILED, PRE/POST_VERIFY_FAILED, PATIENT_NOT_FOUND,
# PREFLIGHT_FAILED — is a non-clean outcome that makes `anast upload` exit 1.
_CLEAN_UPLOAD_STATES: frozenset[str] = frozenset(
    {"completed", "skipped_skiplist", "duplicate_at_destination"}
)


def _upload_exit_code(counts: dict[str, int], aborted_reason: str | None) -> int:
    """The process exit code for a finished run: 1 on abort/any non-clean terminal."""
    if aborted_reason is not None:
        return 1
    for state, n in counts.items():
        if n and state not in _CLEAN_UPLOAD_STATES:
            # A non-clean state still carrying items (a non-terminal leftover or a
            # failure terminal) is a non-clean run — exit 1 so scripts branch on it.
            return 1
    return 0


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
    to: Annotated[str, typer.Option("--to", "-t", help="Destination pack name (e.g. tebra).")],
    cdp: Annotated[
        str,
        typer.Option("--cdp", help="Loopback CDP endpoint, e.g. http://127.0.0.1:9222."),
    ],
    skiplist: Annotated[
        Path | None,
        typer.Option("--skiplist", help="File of item_key/encounter_id lines to exclude."),
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
    """File reconstructed charts into a destination EHR through its web UI.

    Reads the upload manifest written by `anast pipeline run --upload-manifest`
    or `anast migrate`, then drives the crash-resumable upload engine against a
    browser YOU launched with a remote debug port and logged into yourself.

    SAFETY: the CDP attach is loopback-only and refuses any other host; the
    shared-machine warning is shown (and confirmed unless --yes) before any
    browser is touched. Anastomosis NEVER stores your EHR credentials and NEVER
    closes your browser — you log in by hand and the attach ends when you close
    it. The manifest, ledger, and run report all stay inside the 0700 output dir.
    """
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
    from anastomosis.destinations.browserpack import PackNotReadyError
    from anastomosis.destinations.loader import BrowserPackError, load_destination_pack

    # 1. Validate the manifest FIRST (cheap, pre-attach), so a missing/malformed
    #    one fails fast (exit 2) BEFORE the operator confirms the attach. Try the
    #    dir itself, then a <dir>/charts subdir (migrate's layout). The
    #    AUTHORITATIVE read happens inside run_upload_command under the output
    #    lock (lock-then-read), so this early copy is validation only.
    try:
        read_upload_manifest(resolve_manifest_root(out_dir))
    except ManifestError as exc:
        _cli.console.print(f"[red]{_escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from None

    # 2. The loopback gate — BEFORE any browser touch.
    try:
        CdpEndpoint(cdp)
    except ValueError as exc:
        _cli.console.print(f"[red]{_escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from None

    # 3. Surface the shared-machine warning and confirm (unless --yes). --yes
    #    still PRINTS the warning — the operator is told what they accepted.
    _cli.console.print(SHARED_MACHINE_WARNING)
    if not yes and not typer.confirm("Attach to this browser debug port?", default=False):
        _cli.console.print("aborted")
        raise typer.Exit(code=0)

    # 4. Load the destination pack and gate on readiness (selectors discovered).
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

    # 5. Load the operator skiplist if given (a missing file raises -> exit 2).
    skiplist_set: frozenset[str] = frozenset()
    if skiplist is not None:
        try:
            skiplist_set = load_skiplist(skiplist)
        except OSError as exc:
            _cli.console.print(f"[red]could not read skiplist ({type(exc).__name__})[/red]")
            raise typer.Exit(code=2) from None

    # 6. Drive the engine through the SHARED upload command: it harden-locks the
    #    output dir, reads the manifest UNDER the lock (lock-then-read), then
    #    attaches the browser (the only Playwright touch — the injectable seam) and
    #    drives recover -> run -> finish -> report. A locked dir, a manifest that
    #    vanished after the pre-flight, or any other unexpected drive failure is a
    #    clean exit 2 named by exception TYPE only (no PHI, no traceback); a
    #    process-kill BaseException sails through to resume on the next run.
    from anastomosis.core.locking import OutputLockedError
    from anastomosis.deliver.browser.reports import summary_line

    cmd = UploadCommand(
        out_dir=out_dir, skiplist=skiplist_set, max_attempts=max_attempts, verify=verify
    )
    try:
        result = run_upload_command(cmd, lambda: _cli._make_destination(cdp, loaded))
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
    code = _upload_exit_code(result.counts, result.aborted_reason)
    if code != 0:
        raise typer.Exit(code=code)
