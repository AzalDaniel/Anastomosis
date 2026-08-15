"""Practice Fusion / Tebra EHI export adapter.

Reads the §170.315(b)(10) export format: one TSV per entity, GUID foreign
keys, schema per Practice Fusion's public data dictionary v9 (see
``tests/fixtures/pf_tebra_v9/README.md`` for the verified/inferred ledger).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from anastomosis.core.model import PatientRecord
from anastomosis.sources.base import register

from .loader import read_export
from .mapper import map_export

__all__ = ["PFTebraAdapter"]


class PFTebraAdapter:
    name = "pf-tebra"
    description = "Practice Fusion / Tebra EHI export (v9 TSV tables)"

    def detect(self, path: Path) -> bool:
        return (path / "patient-demographics.tsv").is_file() and (
            path / "patient-encounters.tsv"
        ).is_file()

    def load(self, path: Path) -> Iterator[PatientRecord]:
        yield from map_export(read_export(path))


register(PFTebraAdapter())
