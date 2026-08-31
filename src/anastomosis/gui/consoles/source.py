"""The learn-a-source wizard backend (the source console).

Marshals the shared
:func:`anastomosis.core.source_init_command.run_source_init_command` core into
the wizard's JSON dict — synchronously (:meth:`SourceConsole.source_init`, which
describes the flow) and as a busy-guarded daemon job
(:meth:`SourceConsole.source_init_async`).

PHI rule: the proposal carries column names, inferred type labels, counts, and
masked shapes only — never a cell value; the example path the operator typed is
not echoed back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from anastomosis.gui.consoles.wizard import WizardConsole

if TYPE_CHECKING:
    from anastomosis.core.source_init_command import SourceInitResult

__all__ = ["SourceConsole"]


def _source_result_dict(result: SourceInitResult) -> dict[str, object]:
    """Marshal a :class:`SourceInitResult` into the learn-a-source wizard's dict.

    Preserves the wizard's JSON contract exactly: a pre-analyze failure
    (``InvalidSourceName`` / ``NoExampleFile`` / ``AmbiguousExample`` /
    ``CannotAnalyze``) is the bare ``{"ok": False, "error": <code>}``; once
    analysis succeeded the PHI-safe proposal rides along (column names, type
    labels, counts, masked shapes — never a cell value), plus the
    outcome-specific keys (``dropped`` / ``detail`` / the saved-mapping fields).
    """
    out: dict[str, object] = {"ok": result.ok, "error": result.error}
    if result.fmt_type is None:
        return out  # a pre-analyze failure carries no proposal
    out.update(
        {
            "format": result.fmt_type,
            "columns": result.columns,
            "patient_key": result.patient_key,
            "encounter_key": result.encounter_key,
            "row_scope": result.row_scope,
            "summary": list(result.summary),
            "suggestions": [
                {
                    "source": s.source,
                    "target": s.target,
                    "transform": s.transform,
                    "confidence": s.confidence,
                    "inferred_type": s.inferred_type,
                    "sample_shape": s.sample_shape,
                }
                for s in result.suggestions
            ],
            "mapped": result.mapped,
            "targets": list(result.targets),
        }
    )
    if result.ok:
        out.update(
            {
                "mapping_dir": str(result.mapping_dir),
                "mapping_md": result.mapping_md,
                "record_count": result.record_count,
                "unmapped": result.unmapped,
            }
        )
    elif result.error == "WouldDropColumns":
        out["dropped"] = list(result.dropped_columns)
    elif result.error == "MappingLoadFailed":
        out["detail"] = result.detail
        # The structured pointer, so the page can open and mark the exact row
        # instead of scraping the sentence. Names only, never a value.
        out["detail_column"] = result.detail_column
        out["detail_target"] = result.detail_target
        out["detail_transform"] = result.detail_transform
    elif result.error == "CannotBuildMapping":
        out["detail"] = result.detail
    return out


def _parse_decisions(raw: object) -> dict[str, tuple[str, str]]:
    """``review.decisions``, admitted one proven pair at a time."""
    if not isinstance(raw, dict):
        raise TypeError("review.decisions must be an object of column -> [target, transform]")
    decisions: dict[str, tuple[str, str]] = {}
    for column, pair in raw.items():
        if (
            not isinstance(column, str)
            or not isinstance(pair, (list, tuple))
            or len(pair) != 2
            or not all(isinstance(part, str) for part in pair)
        ):
            raise TypeError("review.decisions must map column -> [target, transform]")
        decisions[column] = (pair[0], pair[1])
    return decisions


@dataclass(frozen=True)
class _Review:
    """The browser's review, already proven to be the shape the command takes."""

    decisions: dict[str, tuple[str, str]]
    patient_key: str
    encounter_key: str | None
    row_scope: str


def _parse_review(review: dict[str, object] | None) -> _Review | None:
    """The browser's review, parsed defensively, or ``None`` for no review.

    Decisions submitted from the page are input, not truth: a malformed shape
    here is a stale frontend rather than an operator mistake, and it must
    surface as this console's ordinary failure dict, never a traceback. Only
    string-keyed ``[target, transform]`` pairs are admitted; everything else
    raises for the caller's catch-all to translate.
    """
    if review is None:
        return None
    decisions = _parse_decisions(review.get("decisions"))
    patient_key = review.get("patient_key")
    encounter_key = review.get("encounter_key")
    row_scope = review.get("row_scope")
    if not isinstance(patient_key, str) or not isinstance(row_scope, str):
        raise TypeError("review must carry patient_key and row_scope")
    if encounter_key is not None and not isinstance(encounter_key, str):
        raise TypeError("review.encounter_key must be a column name or null")
    return _Review(
        decisions=decisions,
        patient_key=patient_key,
        encounter_key=encounter_key,
        row_scope=row_scope,
    )


class SourceConsole(WizardConsole):
    """The learn-a-source wizard backend."""

    # The operation family this console owns; stamped on every event so only the
    # learn-a-source wizard page consumes them (the per-page flow guard).
    # The event STAGE stays "source"; the FLOW is the page-owning family name.
    _FLOW = "source_init"
    _STAGE = "source"

    def source_init(
        self,
        example_path: str,
        name: str,
        display: str | None = None,
        confirmed: bool = False,
        out_dir: str | None = None,
        review: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Learn a new structured-export format from one example (wizard backend).

        Mirrors the CLI ``anast source init`` headlessly: resolve the example to
        a single structured file, analyze it LOCALLY, and propose a mapping to the
        canonical model. Without ``confirmed`` this REFUSES
        (``error: ConfirmationRequired``) and writes nothing — returning the
        proposed mapping so the operator sees what they are confirming (the same
        two-step shape as :meth:`pack_init`). With ``confirmed`` it builds the
        mapping, round-trips it against the example to PROVE no column is dropped,
        and only then saves it (owner-only), returning the mapping directory and
        ``MAPPING.md``; a mapping that would drop a column refuses with
        ``error: WouldDropColumns`` and the offending column names.

        PHI rule: ``summary``/``suggestions`` carry column names, inferred type
        labels, counts, and digit/letter-masked shapes only — never a cell value;
        the example path the operator typed is not echoed back. Returns JSON-safe
        data; never raises.

        The analyze -> build -> round-trip -> save flow lives in the SHARED
        :func:`anastomosis.core.source_init_command.run_source_init_command` core
        (the same one ``anast source init`` runs), so the two frontends cannot
        diverge; this method only marshals its result into the wizard's dict.
        """
        try:
            from anastomosis.core.output import typed_path
            from anastomosis.core.source_init_command import (
                SourceInitCommand,
                run_source_init_command,
            )

            parsed = _parse_review(review)

            result = run_source_init_command(
                SourceInitCommand(
                    example=typed_path(example_path),
                    name=name,
                    display=display,
                    out_dir=typed_path(out_dir) if out_dir is not None else None,
                    confirmed=confirmed,
                    decisions=parsed.decisions if parsed else None,
                    patient_key=parsed.patient_key if parsed else None,
                    encounter_key=parsed.encounter_key if parsed else None,
                    row_scope=parsed.row_scope if parsed else None,
                )
            )
            return _source_result_dict(result)
        except Exception as exc:
            return self._fail("source_init", exc)

    def source_init_async(
        self,
        example_path: str,
        name: str,
        display: str | None = None,
        confirmed: bool = False,
        out_dir: str | None = None,
        review: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Run :meth:`source_init` on a daemon thread (the GUI stays responsive).

        :class:`~anastomosis.gui.consoles.wizard.WizardConsole` owns the busy
        guard, the events and the stashing; this method only supplies the source
        step to run. The JS fetches :meth:`last_source_result` after BOTH the
        ``done`` and the ``error`` event, so it can render the outcome-specific
        detail (dropped columns, the load-failure diagnosis). Never raises.
        """

        parsed = _parse_review(review)

        def _run() -> dict[str, object]:
            from anastomosis.core.output import typed_path
            from anastomosis.core.source_init_command import (
                SourceInitCommand,
                run_source_init_command,
            )

            return _source_result_dict(
                run_source_init_command(
                    SourceInitCommand(
                        example=typed_path(example_path),
                        name=name,
                        display=display,
                        out_dir=typed_path(out_dir) if out_dir is not None else None,
                        confirmed=confirmed,
                        decisions=parsed.decisions if parsed else None,
                        patient_key=parsed.patient_key if parsed else None,
                        encounter_key=parsed.encounter_key if parsed else None,
                        row_scope=parsed.row_scope if parsed else None,
                    )
                )
            )

        return self._submit_step(_run)

    def last_source_result(self) -> dict[str, object]:
        """The most recent :meth:`source_init_async` result, for the wizard to fetch.

        The proposal for a ``ConfirmationRequired`` checkpoint, the path +
        ``MAPPING.md`` for a saved mapping, or the dropped columns / load
        diagnosis for a refusal. PHI-safe (column names, type labels, counts,
        masked shapes, mapping config).
        """
        return self._last_result()
