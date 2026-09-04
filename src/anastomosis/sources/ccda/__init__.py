"""C-CDA / CCD XML ingest adapter (HL7 CDA R2).

Reads HL7 Consolidated CDA R2.1 documents — the format every certified EHR can
export and most can import — into canonical :class:`PatientRecord` objects.
One ``ClinicalDocument`` XML file yields one record — the unit the conservation
ledger has to measure — and the pipeline folds the several records of one
patient into the one chart every destination is keyed by
(``pipeline._fold_records_sharing_a_patient``). Sections the adapter does not
structurally parse have their narrative preserved into the patient's
``extensions`` (the lossless guarantee). See ``parser.py`` for the mapping and
``tests/fixtures/ccda/README.md`` for the verified element reference.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from lxml import etree

from anastomosis.core.ccda_codes import V3
from anastomosis.core.model import PatientRecord
from anastomosis.sources.base import SourceDataError, register

from .ledger import DocumentLedger, document_ledger
from .parser import _HARDENED_XML_KWARGS, parse_document

__all__ = ["CCDAAdapter"]

# The Clark-notation tag CDA's document element must carry. Compared against
# the FIRST start event lxml reports (see `_is_cda_document`) rather than
# against a byte window: a comment, BOM, processing instruction or DTD of any
# length precedes no element, so none of them can move where that first event
# falls, and a well-formed XML document has exactly one root to find.
_CDA_ROOT_TAG = f"{{{V3}}}ClinicalDocument"

#: Extensions this adapter reads as a C-CDA document, matched on
#: ``Path.suffix.lower()`` so ``.CCD``, ``.Xml``, ``.ccda`` all match on every
#: platform, a case-sensitive POSIX filesystem included. Kareo/Tebra write a
#: CCD as ``<name>.ccd`` (observed in a real export); Tebra's own extension
#: for the format is ``.ccda`` (``docs/EHR_FORMATS.md``). Before this set
#: existed, the walk below matched ``*.xml`` only (#384): a ``.ccd`` document
#: was never opened, never counted, and never mentioned — a whole patient's
#: chart silently absent from a run that exited 0 and reported success.
_DOCUMENT_SUFFIXES = frozenset({".xml", ".ccd", ".ccda"})


def _entries(path: Path) -> Iterator[Path]:
    """Every FILE directly inside ``path`` — never a raise, only "nothing here".

    ``Path.iterdir()`` is lazy: the call itself never raises, only the FIRST
    step through it does, so the whole walk is forced (``list(...)``) inside
    the guard rather than only the call that returns the generator. A path
    that turns out to be a file (``NotADirectoryError``), one that is simply
    not there (``FileNotFoundError``), or one this process lacks permission to
    list (``PermissionError``) all read as "no documents here", not a crash —
    the auto-detect loop trying every adapter's ``detect`` in turn is the
    caller that pays for the difference: one adapter raising used to abort
    every OTHER adapter's chance to answer, including the learned adapter's,
    which is documented to accept a FILE path directly (#384 round two,
    finding 1). A subdirectory or broken symlink sharing a document's
    extension (finding 7) is skipped the same way: ``is_file()`` answers
    ``False`` for both without raising.
    """
    try:
        candidates = list(path.iterdir())
    except OSError:
        return
    for candidate in candidates:
        if candidate.is_file():
            yield candidate


def _is_cda_document(candidate: Path) -> bool:
    """Whether ``candidate``'s DOCUMENT ELEMENT is a CDA ``ClinicalDocument``.

    Decided by the first ``start`` event lxml reports while walking the file,
    never a byte window: a real CCD carrying a 5.6 KB vendor comment before
    its root parsed cleanly through :func:`~.parser.parse_document` and was
    STILL silently absent from every run, because the old 4 KB peek never
    reached the markers it was looking for (#384 round two, finding 4). A
    leading comment, BOM, processing instruction or DTD of any length
    precedes no element, so none of them can move where the first ``start``
    event falls.

    Read under the same hardened settings as the real parse
    (:data:`~.parser._HARDENED_XML_KWARGS` — no network, no entity
    resolution, no DTD load, no unbounded tree): a document is not read under
    weaker rules just because it is only being sniffed. Any failure — a file
    with no recoverable start tag, a namespace or root name that is not CDA's,
    one this process cannot even open — reads as "not CDA", the same
    tolerance ``detect`` has always needed (it must never raise), extended
    here to the wider walk that also probes files this adapter never intends
    to read. This keeps the existing behaviour that an accepted-extension
    file which is not genuinely CDA is neither read nor counted: an ordinary
    non-CDA XML file has SOME root element, just never this one.
    """
    try:
        events = etree.iterparse(str(candidate), events=("start",), **_HARDENED_XML_KWARGS)
        for _event, element in events:
            return bool(element.tag == _CDA_ROOT_TAG)
        return False
    except (OSError, etree.LxmlError):
        return False


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

    Sorted by ``p.name`` rather than by ``Path`` comparison: ``Path`` objects
    compare case-FOLDED on Windows, so once mixed-case extensions were
    admitted (#384) the same export's "document N of M" pointed at a
    different document per platform. Sorting the name strings is codepoint
    order everywhere, matching what ``load``'s refusal promises.

    Every entry comes from :func:`_entries`, so a subdirectory or broken
    symlink sharing a document's extension is already excluded by the time
    this loop sees it (finding 7), and neither this walk nor the sniff it
    calls ever raises: a document that IS recognised here and only fails
    later, once ``load`` actually opens it for the real parse, still refuses
    the whole run by position — that is the loud-failure guarantee this
    adapter keeps; a file that never gets that far because it could not even
    be sniffed is indistinguishable from one that was never CDA to begin
    with, and both stay silent at this layer on purpose (``detect`` shares
    the same sniff and must never raise either).
    """
    documents: list[Path] = []
    unmatched = 0
    for candidate in sorted(_entries(path), key=lambda p: p.name):
        if candidate.suffix.lower() in _DOCUMENT_SUFFIXES:
            if _is_cda_document(candidate):
                documents.append(candidate)
        elif _is_cda_document(candidate):
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
        """Cheap sniff: does ``path`` hold a file that reads as a CDA document?

        Never raises (the source-adapter contract) — a file path, a missing
        path, or a directory this process cannot list all answer ``False``
        via :func:`_entries` rather than aborting :func:`~.base.detect_source`'s
        loop over every adapter (#384 round two, finding 1): before this fix,
        one adapter raising on a path shaped for a DIFFERENT adapter (a file
        path, which the learned adapter is documented to accept) meant no
        adapter after it in the loop was ever consulted, in the CLI and in
        the GUI wizard's picker alike.
        """
        return any(
            candidate.suffix.lower() in _DOCUMENT_SUFFIXES and _is_cda_document(candidate)
            for candidate in _entries(path)
        )

    def load(self, path: Path) -> Iterator[PatientRecord]:
        """Every CDA document in ``path``, in filename order — one record each.

        A patient with several documents therefore leaves here as several
        records, and the pipeline folds them into one chart before anything is
        delivered (``pipeline._fold_records_sharing_a_patient``). The split
        stays here because a DOCUMENT is what :class:`DocumentLedger` has to
        account for, construct by construct; a ledger over a merged record could
        not say which document went short.

        A document this adapter cannot parse refuses the RUN — a partial
        migration that silently omits a patient is the failure this project
        exists to prevent. But refusing has to tell the operator which document
        to repair, and until now it did not: the exception escaped as an
        arbitrary error, so the pipeline could only show its type. Against
        2,103 real documents that left bisecting by hand as the only recourse.

        The document is named by POSITION, not by file name — a C-CDA export
        names its files after the patient, so a name in an error message is a
        patient value, and a position is not. Sorting the export's filenames
        (by name, codepoint order — see :func:`_scan`) finds it: every
        document accepted here is ``.xml``, ``.ccd``, or ``.ccda``, matched
        without regard to case (#384 — before this, ``ls *.xml | sort`` was
        the whole set; a Kareo/Tebra ``.ccd`` CCD sorted in beside them but
        was never opened).
        """
        # Reset together, before `_scan` runs, not only by its return value:
        # `ledgers` was already reset unconditionally here, but `skipped_files`
        # used to be set ONLY by the assignment below — so anything that ever
        # raises between this line and that one (as a subdirectory sharing a
        # document's extension used to, #384 round two, finding 7) would leave
        # this process-singleton adapter reporting the PREVIOUS run's count on
        # a run that never got to scan anything of its own.
        self.ledgers = []
        self.skipped_files = 0
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
