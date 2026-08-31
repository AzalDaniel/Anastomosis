"""Practice Fusion / Tebra EHI export adapter.

Reads the §170.315(b)(10) export format: one TSV per entity, GUID foreign
keys, schema per Practice Fusion's public data dictionary v9 (see
``tests/fixtures/pf_tebra_v9/README.md`` for the verified/inferred ledger).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from anastomosis.core.model import PatientRecord
from anastomosis.sources.base import QuarantinedRows, SelectionRule, register

from .loader import find_attachments, read_export
from .mapper import DEFAULT_SELECTION, SELECTION_RULES, map_export

__all__ = ["PFTebraAdapter"]


class PFTebraAdapter:
    name = "pf-tebra"
    display = "Practice Fusion / Tebra"
    description = "Practice Fusion / Tebra EHI export (v9 TSV tables)"

    #: The render-selection rules this adapter applies (see
    #: :data:`~anastomosis.sources.pf_tebra.mapper.SELECTION_RULES`). Declared
    #: on the class rather than the instance: it is what the adapter IS, and a
    #: run reads it to validate its ``--include`` names before loading anything.
    selection_rules: tuple[SelectionRule, ...] = SELECTION_RULES

    def __init__(self, *, include: frozenset[str] = frozenset()) -> None:
        #: What the last completed ``load`` could not place on any patient.
        #: Reset at the START of every load — the registry holds one adapter
        #: instance for the process, so a stale list from the previous export
        #: must not read as this export's quarantine.
        self.quarantine: list[QuarantinedRows] = []
        #: The rules this instance applies: all of them, minus the ones the run
        #: switched off. The registered adapter is built with none switched off,
        #: so it selects exactly what it always selected.
        self.selection: frozenset[str] = DEFAULT_SELECTION - include

    def including(self, rules: frozenset[str]) -> PFTebraAdapter:
        """A sibling adapter that does NOT apply ``rules`` (one run's choice).

        A new instance rather than a setting on this one: the registry hands
        every caller the same adapter object, and a run that mutated it would
        be choosing for the next run too — including a GUI session's next run,
        in the same process, with no flag on screen to explain it.
        """
        return PFTebraAdapter(include=rules)

    def detect(self, path: Path) -> bool:
        return (path / "patient-demographics.tsv").is_file() and (
            path / "patient-encounters.tsv"
        ).is_file()

    def load(self, path: Path) -> Iterator[PatientRecord]:
        self.quarantine = []

        def _hold(held: list[QuarantinedRows]) -> None:
            self.quarantine = held

        yield from map_export(
            read_export(path),
            attachments=find_attachments(path),
            on_quarantine=_hold,
            selection=self.selection,
        )


register(PFTebraAdapter())
