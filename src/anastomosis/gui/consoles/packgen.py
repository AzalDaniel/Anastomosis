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

    # Stamped on every event so only this wizard page consumes them; STAGE
    # stays "packgen"/"pack_init", FLOW is the page-owning family name.
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

        Adapter over :func:`run_pack_init` (28); confirmed emits the draft,
        else refuses ``ConfirmationRequired``. ``out_dir=None`` uses the per-user dir (36)."""
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

        Shared by the sync and async paths. ``emit_failure=False`` (async)
        skips the sync path's own error event, avoiding a double emission.
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
                # The identity Charts/Migrate will offer, and the exact dir+hash
                # a later run binds to; named, since that's what's picked next.
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

        :class:`WizardConsole` owns the busy guard/events/stashing; this
        supplies only the pack step. Same confirm semantics as the sync path.
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

        Summary + caveat for ``ConfirmationRequired``, else the pack path +
        ``DRAFT.md``. PHI-safe (template text, counts, config).
        """
        return self._last_result()
