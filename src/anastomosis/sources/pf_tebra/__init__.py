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

    #: Declared on the class, not the instance: it is what the adapter IS,
    #: and a run validates its ``--include`` names against it before loading.
    selection_rules: tuple[SelectionRule, ...] = SELECTION_RULES

    def __init__(self, *, include: frozenset[str] = frozenset()) -> None:
        #: Held from the last load; reset at its start — the registry keeps
        #: one instance per process, so a stale list must not read as this export's.
        self.quarantine: list[QuarantinedRows] = []
        #: All selection rules minus the ones this run switched off; the
        #: registered adapter switches none off, so default behaviour is unchanged.
        self.selection: frozenset[str] = DEFAULT_SELECTION - include

    def including(self, rules: frozenset[str]) -> PFTebraAdapter:
        """A sibling adapter that does NOT apply ``rules`` (one run's choice).
        A new instance, not a mutation: the registry hands every caller the
        same object, and mutating it would choose for the next run too —
        including a GUI session's next run, with no flag on screen to explain
        it."""
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
