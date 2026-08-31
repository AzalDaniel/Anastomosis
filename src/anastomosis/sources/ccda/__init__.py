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
from anastomosis.sources.base import SourceDataError, register

from .ledger import DocumentLedger, document_ledger
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
    display = "C-CDA"
    description = "C-CDA / CCD XML documents (HL7 CDA R2)"

    def __init__(self) -> None:
        #: One :class:`DocumentLedger` per document the last ``load`` parsed —
        #: what each offered against what its record kept. Same contract as the
        #: ``quarantine`` attribute (see ``sources/base.py``): reset when a load
        #: starts, complete once it has been fully consumed, read by the
        #: pipeline with ``getattr``. Kept on the hot path deliberately: the
        #: second walk costs about a millisecond per document — under half a
        #: parse, noise against a render — and a reading that only exists when
        #: someone thought to ask for it is how the original loss went unseen.
        self.ledgers: list[DocumentLedger] = []

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
        """Every CDA document in ``path``, in filename order.

        A document this adapter cannot parse refuses the RUN — a partial
        migration that silently omits a patient is the failure this project
        exists to prevent. But refusing has to tell the operator which document
        to repair, and until now it did not: the exception escaped as an
        arbitrary error, so the pipeline could only show its type. Against
        2,103 real documents that left bisecting by hand as the only recourse.

        The document is named by POSITION, not by file name. A C-CDA export
        names its files after the patient, so a name in an error message is a
        patient value; a position is not, and ``ls *.xml | sort`` finds it.
        """
        self.ledgers = []
        documents = [
            xml_file
            for xml_file in sorted(path.glob("*.xml"))
            if _looks_like_cda(xml_file.read_bytes()[:_SNIFF_BYTES])
        ]
        for position, xml_file in enumerate(documents, start=1):
            try:
                record = parse_document(xml_file)
            except SourceDataError:
                raise
            except Exception as exc:
                raise SourceDataError(
                    f"document {position} of {len(documents)}, in filename order, could not be "
                    f"read ({type(exc).__name__}). The {position - 1} before it parsed; the run "
                    f"refuses rather than migrating an export with a document missing from it. "
                    f"The file is identified by position because a C-CDA export names its "
                    f"documents after the patient."
                ) from None
            # Outside the wrap above on purpose: a ledger that cannot balance
            # its books over this document raises ConservationError, which is
            # not a document that "could not be read", and folding it into that
            # message would bury the count that says which column went short.
            # It propagates bare; ``load_records`` turns it into the same loud
            # conservation refusal the render seam gets.
            self.ledgers.append(document_ledger(xml_file, record))
            yield record


register(CCDAAdapter())
