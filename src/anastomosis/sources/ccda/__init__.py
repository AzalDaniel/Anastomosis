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
_SNIFF_MARKERS = ("urn:hl7-org:v3", "ClinicalDocument")


def _looks_like_cda(head: bytes) -> bool:
    """Whether a file head looks like an HL7 CDA document, tolerant of the XML
    encodings real exports use.

    The markers are ASCII, but a UTF-16 document (some Windows EHRs export
    UTF-16) interleaves every ASCII byte with a NUL, so a raw byte search misses
    them and the file would be silently skipped (zero records, no error). Decode
    the head by its BOM first, then match as text. lxml parses UTF-8 and UTF-16
    (LE/BE) natively from the path, so a match here is safe to hand to
    ``parse_document`` — the document loads rather than failing closed.
    """
    encoding = "utf-16" if head[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8"
    text = head.decode(encoding, errors="ignore")
    return all(marker in text for marker in _SNIFF_MARKERS)


class CCDAAdapter:
    name = "ccda"
    description = "C-CDA / CCD XML documents (HL7 CDA R2)"

    def detect(self, path: Path) -> bool:
        for xml_file in path.glob("*.xml"):
            try:
                head = xml_file.read_bytes()[:_SNIFF_BYTES]
            except OSError:
                continue
            if _looks_like_cda(head):
                return True
        return False

    def load(self, path: Path) -> Iterator[PatientRecord]:
        for xml_file in sorted(path.glob("*.xml")):
            head = xml_file.read_bytes()[:_SNIFF_BYTES]
            if _looks_like_cda(head):
                yield parse_document(xml_file)


register(CCDAAdapter())
