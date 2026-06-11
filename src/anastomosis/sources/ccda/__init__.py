"""C-CDA / CCD XML ingest adapter (HL7 CDA R2).

Reads HL7 Consolidated CDA R2.1 documents — the format every certified EHR can
export and most can import — into canonical :class:`PatientRecord` objects.
One ``ClinicalDocument`` XML file yields one record; sections the adapter does
not structurally parse have their narrative preserved into the patient's
``extensions`` (the lossless guarantee). See ``parser.py`` for the mapping and
``tests/fixtures/ccda/README.md`` for the verified element reference.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from anastomosis.core.model import PatientRecord
from anastomosis.sources.base import register

from .parser import parse_document

__all__ = ["CCDAAdapter"]

# Cheap structural sniff: a CDA document declares the HL7 v3 namespace and a
# ClinicalDocument root. Reading the first 4 KB avoids a full parse (and avoids
# matching a PF/Tebra TSV export, which has no XML at all).
_SNIFF_BYTES = 4096
_SNIFF_MARKERS = (b"urn:hl7-org:v3", b"ClinicalDocument")


class CCDAAdapter:
    name = "ccda"
    description = "C-CDA / CCD XML documents (HL7 CDA R2)"

    def detect(self, path: Path) -> bool:
        for xml_file in path.glob("*.xml"):
            try:
                head = xml_file.read_bytes()[:_SNIFF_BYTES]
            except OSError:
                continue
            if all(marker in head for marker in _SNIFF_MARKERS):
                return True
        return False

    def load(self, path: Path) -> Iterator[PatientRecord]:
        for xml_file in sorted(path.glob("*.xml")):
            head = xml_file.read_bytes()[:_SNIFF_BYTES]
            if all(marker in head for marker in _SNIFF_MARKERS):
                yield parse_document(xml_file)


register(CCDAAdapter())
