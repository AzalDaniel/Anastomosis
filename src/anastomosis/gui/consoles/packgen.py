"""The pack-from-samples wizard backend (the packgen console).

A thin adapter over the shared :func:`anastomosis.core.packinit.run_pack_init`
core — the analyze -> confirm -> emit flow the CLI's ``anast pack init`` runs —
offered both synchronously (:meth:`PackgenConsole.pack_init`) and as a
busy-guarded daemon job (:meth:`PackgenConsole.pack_init_async`). Every method
keeps the controller's contract: JSON-safe dict, never raise, PHI-safe events
(static template text + counts only, never a sample path or a cell value).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from anastomosis.gui.consoles.wizard import WizardConsole
from anastomosis.gui.events import error_event

if TYPE_CHECKING:
    from anastomosis.core.packinit import PackInitResult

__all__ = ["PackgenConsole"]


class PackgenConsole(WizardConsole):
    """The pack-from-samples wizard backend."""

    # The operation family this console owns; stamped on every event so only the
    # pack-from-samples wizard page consumes them (the per-page flow guard).
    # The event STAGE stays "packgen"/"pack_init"; the FLOW is the page-owning
    # family name.
    _FLOW = "pack_init"
    _STAGE = "packgen"

    def pack_init(
        self,
        samples_dir: str,
        name: str,
        display: str | None = None,
        confirmed_distinct_patients: bool = False,
        out_dir: str | None = None,
    ) -> dict[str, object]:
        """Learn a DRAFT template pack from sample PDFs (the wizard's backend).

        A thin adapter over the shared
        :func:`anastomosis.core.packinit.run_pack_init` — the SAME analyze →
        confirm → emit flow the CLI's ``anast pack init`` runs. Validate the
        pack name, collect the sample PDFs, harvest + analyze them, render the
        PHI-safe :meth:`PackAnalysis.summary_lines` digest, and — only with
        ``confirmed_distinct_patients`` checked (the CLI's interactive
        same-patient guard, ported as a required checkbox) — emit the draft and
        return its name, path, trusted content hash and ``DRAFT.md`` text for
        display.

        ``out_dir`` defaults to ``None``, which the shared core reads as the
        per-user pack directory. That is not a cosmetic default: a relative
        ``packs/`` was resolved against whatever directory the app happened to
        be launched from, so the wizard reported a written layout that the
        Charts and Migrate choosers then could not offer.

        Without the confirmation this REFUSES (``ok: False``, ``error:
        ConfirmationRequired``) and writes nothing — the same guard the CLI
        enforces with ``typer.confirm``. The single-sample text-suppression
        behavior is inherited from ``summary_lines`` (the draft never echoes
        per-patient text).

        PHI rule: ``summary`` carries only static template text (recurring
        across distinct samples) and counts; sample paths are never echoed (the
        count is). Returns JSON-safe data; never raises.
        """
        try:
            from anastomosis.core.output import typed_path
            from anastomosis.core.packinit import PackInitCommand, run_pack_init

            result = run_pack_init(
                PackInitCommand(
                    samples=[samples_dir],
                    name=name,
                    display=display,
                    out_dir=typed_path(out_dir) if out_dir is not None else None,
                    confirmed=confirmed_distinct_patients,
                )
            )
            return self._pack_init_result_dict(result)
        except Exception as exc:
            return self._fail("pack_init", exc)

    def _pack_init_result_dict(
        self, result: PackInitResult, *, emit_failure: bool = True
    ) -> dict[str, object]:
        """Map a :class:`PackInitResult` to the wizard's JSON-safe dict.

        Shared by the sync :meth:`pack_init` and the async worker so both
        present an identical surface. The PHI-free validation errors
        (``InvalidPackName`` / ``NoSamplesFound``) return the bare code; the
        refusal carries the summary + caveat so the operator can confirm;
        success carries the pack path + ``DRAFT.md``. An analyze/emit failure
        (``error`` is an :func:`exc_tag` type name) ALSO emits a ``pack_init``
        ``error`` event (the sync controller's loud-failure contract) — but only
        when ``emit_failure`` is set. The async worker passes
        ``emit_failure=False`` because IT emits the single ``packgen`` error
        event on its own channel; the gate prevents a double, stage-mismatched
        error event on the async path.
        """
        if result.error in {"InvalidPackName", "NoSamplesFound"}:
            return {"ok": False, "error": result.error}
        if result.error == "ConfirmationRequired":
            return {
                "ok": False,
                "error": "ConfirmationRequired",
                "caveat": result.caveat,
                "summary": result.summary,
                "sample_count": result.sample_count,
                "low_confidence": result.low_confidence,
            }
        if result.ok:
            return {
                "ok": True,
                # The identity the Charts and Migrate choosers will offer, and
                # the exact directory + hash a later run binds to. The wizard
                # names the layout it wrote rather than only its path, because
                # the name is what the operator has to pick on the next screen.
                "pack": result.pack_name,
                "pack_dir": str(result.pack_dir),
                "content_hash": result.content_hash,
                "draft_md": result.draft_md,
                "summary": result.summary,
                "sample_count": result.sample_count,
                "low_confidence": result.low_confidence,
            }
        # An analyze/emit failure: a type-name diagnosis. Mirror _fail — emit an
        # error event AND return the no-traceback error dict (sync path only;
        # the async worker emits its own packgen error event).
        error = result.error or "PackInitError"
        if emit_failure:
            self._emit(error_event(self._FLOW, "pack_init", error))
        return {"ok": False, "error": error}

    def pack_init_async(
        self,
        samples_dir: str,
        name: str,
        display: str | None = None,
        confirmed_distinct_patients: bool = False,
        out_dir: str | None = None,
    ) -> dict[str, object]:
        """Run :meth:`pack_init` on a daemon thread (the GUI stays responsive).

        :class:`~anastomosis.gui.consoles.wizard.WizardConsole` owns the busy
        guard, the events and the stashing; this method only supplies the pack
        step to run. The same-patient semantics match the sync path:
        ``confirmed=False`` analyzes and stops at ``ConfirmationRequired`` with
        the summary + caveat, so the JS can use the async path for BOTH wizard
        steps. Never raises.
        """

        def _run() -> dict[str, object]:
            from anastomosis.core.output import typed_path
            from anastomosis.core.packinit import PackInitCommand, run_pack_init

            return self._pack_init_result_dict(
                run_pack_init(
                    PackInitCommand(
                        samples=[samples_dir],
                        name=name,
                        display=display,
                        out_dir=typed_path(out_dir) if out_dir is not None else None,
                        confirmed=confirmed_distinct_patients,
                    )
                ),
                emit_failure=False,  # WizardConsole emits the single packgen event
            )

        return self._submit_step(_run)

    def last_pack_result(self) -> dict[str, object]:
        """The most recent :meth:`pack_init_async` result, for the wizard to fetch.

        Either the summary + caveat for a ``ConfirmationRequired`` checkpoint or
        the pack path + ``DRAFT.md`` for a written draft. PHI-safe (static
        template text, counts, pack config).
        """
        return self._last_result()
