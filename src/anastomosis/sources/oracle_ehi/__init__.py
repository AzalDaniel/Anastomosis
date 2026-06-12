"""Oracle Health (Cerner Millennium) EHI export adapter.

Reads the single-patient Millennium EHI export — the V500 data model shipped
as MySQL ``INSERT`` dumps under ``v500/{schema,activity,reference}`` per the
brief's §5.1 packaging — into canonical :class:`PatientRecord` objects. The
contract is ``docs/vendor_refs/ORACLE_EHI_SCHEMA.md``; every table, column,
join, and packaging fact this adapter relies on is cited there by section
number, and facts the brief marks "could not determine" (§8) raise loudly
instead of being guessed (see :func:`~.mapper.decode_ce_blob`).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from anastomosis.core.model import PatientRecord
from anastomosis.sources.base import register

from .loader import read_export
from .mapper import map_export

__all__ = ["OracleEHIAdapter"]


class OracleEHIAdapter:
    name = "oracle-ehi"
    description = "Oracle Health / Cerner Millennium EHI export (V500 MySQL dumps)"

    def detect(self, path: Path) -> bool:
        """Cheap structural sniff for the §5.1 single-patient export shape.

        The export's signature is a ``v500`` directory carrying ``schema`` +
        at least one data subdirectory, with the schema file naming the brief
        documents (``V500TableSchema*.sql``). PF/Tebra (flat TSVs) and C-CDA
        (loose XML) have no such tree, so this never collides with them.
        """
        v500 = path / "v500"
        if not v500.is_dir():
            return False
        schema = v500 / "schema"
        if not schema.is_dir():
            return False
        has_table_schema = any(schema.glob("V500TableSchema*.sql"))
        has_data_dir = (v500 / "activity").is_dir() or (v500 / "reference").is_dir()
        return has_table_schema and has_data_dir

    def load(self, path: Path) -> Iterator[PatientRecord]:
        yield from map_export(read_export(path))


register(OracleEHIAdapter())
