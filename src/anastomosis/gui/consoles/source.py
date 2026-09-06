"""The learn-a-source wizard backend (the source console).

Marshals :func:`run_source_init_command` into the wizard's JSON dict,
synchronously and as a busy-guarded daemon job.

PHI rule: the proposal carries column names, type labels, counts and
masked shapes only — never a cell value or the example path.
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

    A pre-analyze failure is the bare ``{"ok": False, "error": <code>}``;
    once analysis succeeds the PHI-safe proposal rides along, plus outcome-specific keys.
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
            # Echoed back so the view shows what this mapping is bound to;
            # ``None`` for an unbound teach — the default, and every mapping predating this step.
            "destination": result.destination,
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
        out["detail_scope"] = result.detail_scope
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

    Malformed input surfaces as this console's ordinary failure dict, never
    a traceback: only string-keyed ``[target, transform]`` pairs are admitted.
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

    # Stamped on every event so only this wizard page consumes them; STAGE
    # stays "source", FLOW is the page-owning family name.
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
        destination: str | None = None,
    ) -> dict[str, object]:
        """Learn a new structured-export format from one example (wizard backend).

        Mirrors ``anast source init`` via :func:`run_source_init_command`
        (28, 29). ``destination`` binds a profile hash (32).
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
                    destination=destination,
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
        destination: str | None = None,
    ) -> dict[str, object]:
        """Run :meth:`source_init` on a daemon thread (the GUI stays responsive).

        :class:`WizardConsole` owns the busy guard/events/stashing; this
        supplies only the source step. Never raises.
        """

        def _run() -> dict[str, object]:
            from anastomosis.core.output import typed_path
            from anastomosis.core.source_init_command import (
                SourceInitCommand,
                run_source_init_command,
            )

            # Parsed INSIDE the step, not before: a malformed review must
            # surface as this console's ordinary failure dict, which only the step runner catches.
            parsed = _parse_review(review)
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
                        destination=destination,
                    )
                )
            )

        return self._submit_step(_run)

    def last_source_result(self) -> dict[str, object]:
        """The most recent :meth:`source_init_async` result, for the wizard to fetch.

        The proposal, the saved mapping path + ``MAPPING.md``, or the
        dropped columns / load diagnosis for a refusal. PHI-safe.
        """
        return self._last_result()
