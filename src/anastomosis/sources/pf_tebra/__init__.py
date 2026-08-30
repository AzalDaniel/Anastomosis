"""Practice Fusion / Tebra EHI export adapter.

Reads the §170.315(b)(10) export format: one TSV per entity, GUID foreign
keys, schema per Practice Fusion's public data dictionary v9 (see
``tests/fixtures/pf_tebra_v9/README.md`` for the verified/inferred ledger).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from anastomosis.core.model import PatientRecord
from anastomosis.sources.base import QuarantinedRows, register

from .loader import find_attachments, read_export
from .mapper import map_export

__all__ = ["PFTebraAdapter"]


class PFTebraAdapter:
    name = "pf-tebra"
    display = "Practice Fusion / Tebra"
    description = "Practice Fusion / Tebra EHI export (v9 TSV tables)"

    def __init__(self) -> None:
        #: What the last completed ``load`` could not place on any patient.
        #: Reset at the START of every load — the registry holds one adapter
        #: instance for the process, so a stale list from the previous export
        #: must not read as this export's quarantine.
        self.quarantine: list[QuarantinedRows] = []

    def detect(self, path: Path) -> bool:
        return (path / "patient-demographics.tsv").is_file() and (
            path / "patient-encounters.tsv"
        ).is_file()

    def load(self, path: Path) -> Iterator[PatientRecord]:
        self.quarantine = []

        def _hold(held: list[QuarantinedRows]) -> None:
            self.quarantine = held

        yield from map_export(
            read_export(path), attachments=find_attachments(path), on_quarantine=_hold
        )


register(PFTebraAdapter())
