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

#: Extensions this adapter reads as a C-CDA document, matched on
#: ``Path.suffix.lower()`` so ``.CCD``, ``.Xml``, ``.ccda`` all match on every
#: platform, a case-sensitive POSIX filesystem included. Kareo/Tebra write a
#: CCD as ``<name>.ccd``; other vendors write ``.ccda``. Before this set
#: existed, the walk below matched ``*.xml`` only (#384): a ``.ccd`` document
#: was never opened, never counted, and never mentioned — a whole patient's
#: chart silently absent from a run that exited 0 and reported success.
_DOCUMENT_SUFFIXES = frozenset({".xml", ".ccd", ".ccda"})


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


def _sniff(candidate: Path) -> bool:
    """Whether ``candidate``'s head reads as a CDA document.

    ``False`` rather than a raised ``OSError`` for anything this process
    cannot open (a directory, a broken symlink, a permission this account
    lacks) — the same tolerance ``detect`` has always needed, since it must
    never raise (see ``sources/base.py``), extended here to the wider walk
    that now also probes files this adapter does not intend to read at all.
    """
    try:
        head = candidate.read_bytes()[:_SNIFF_BYTES]
    except OSError:
        return False
    return _looks_like_cda(head)


def _scan(path: Path) -> tuple[list[Path], int]:
    """Every CDA document in ``path``, sorted by filename, and how many OTHER
    files in the export read as a CDA document too but under an extension
    this adapter does not accept.

    One walk answers both questions from the same directory listing, so "what
    gets read" and "what got left behind" cannot disagree about what was
    there. A file whose extension IS accepted but that does not sniff as CDA
    is neither read nor counted here — that filtering predates this function
    and stays exactly as narrow as it always was. A file whose extension is
    NOT accepted and does not sniff as CDA is not this adapter's business at
    all: an export legitimately carries non-CDA files beside its documents (a
    ``nonXMLBody``'s own referenced attachment, for one — see
    ``parser._resolved_reference``), and counting every one of those as
    "skipped" would bury the finding that matters — a document this adapter's
    own sniff recognises, left unread by an accident of its extension — in
    noise about files this adapter was never going to read regardless.

    An accepted file this process cannot even read raises, exactly as it
    always has: swallowing that here would mean an unreadable document simply
    vanished from ``len(documents)``, which is the same silent loss #384 was
    filed over, one step downstream.
    """
    documents: list[Path] = []
    unmatched = 0
    for candidate in sorted(path.iterdir()):
        if candidate.suffix.lower() in _DOCUMENT_SUFFIXES:
            if _looks_like_cda(candidate.read_bytes()[:_SNIFF_BYTES]):
                documents.append(candidate)
        elif _sniff(candidate):
            unmatched += 1
    return documents, unmatched


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
        #: How many files the last ``load`` recognised as a C-CDA document by
        #: content but left unread, because their extension named none of
        #: ``_DOCUMENT_SUFFIXES``. The count #384 asked for — never the
        #: filenames, which a C-CDA export names after the patient. Reset on
        #: every ``load``, on the same reset-and-``getattr`` contract as
        #: ``ledgers``: read by ``pipeline.settle_source_ledger`` into the run's
        #: source reading and ``loss_ledger.json``, never a channel of its own.
        self.skipped_files: int = 0

    def detect(self, path: Path) -> bool:
        for candidate in path.iterdir():
            if candidate.suffix.lower() in _DOCUMENT_SUFFIXES and _sniff(candidate):
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

        The document is named by POSITION, not by file name — a C-CDA export
        names its files after the patient, so a name in an error message is a
        patient value, and a position is not. Sorting the export's filenames
        finds it: every document accepted here is ``.xml``, ``.ccd``, or
        ``.ccda``, matched without regard to case (#384 — before this, ``ls
        *.xml | sort`` was the whole set; a Kareo/Tebra ``.ccd`` CCD sorted in
        beside them but was never opened).
        """
        self.ledgers = []
        documents, self.skipped_files = _scan(path)
        for position, document in enumerate(documents, start=1):
            try:
                record = parse_document(document)
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
            self.ledgers.append(document_ledger(document, record))
            yield record


register(CCDAAdapter())
