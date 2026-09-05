"""C-CDA / CCD XML ingest adapter (HL7 CDA R2).

Reads HL7 Consolidated CDA R2.1 documents into canonical
:class:`PatientRecord` objects: one ``ClinicalDocument`` file per record, the
unit :class:`~.ledger.DocumentLedger` measures. The pipeline folds one
patient's several documents into one chart
(``pipeline._fold_records_sharing_a_patient``). See ``parser.py`` for the
mapping and ``tests/fixtures/ccda/README.md`` for the verified element
reference.
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

# The Clark-notation tag CDA's document element must carry. Matched against
# the first start event lxml reports, never a byte window (rule 56).
_CDA_ROOT_TAG = f"{{{V3}}}ClinicalDocument"

#: Extensions read as a C-CDA document, matched case-insensitively.
#: Kareo/Tebra export a CCD as ``.ccd``; Tebra's own extension for the
#: format is ``.ccda`` (``docs/EHR_FORMATS.md``).
_DOCUMENT_SUFFIXES = frozenset({".xml", ".ccd", ".ccda"})


def _entries(path: Path) -> Iterator[Path]:
    """Every FILE directly inside ``path``; never raises.

    ``iterdir()`` is lazy, so the walk is forced inside the guard: a missing,
    non-directory, or unreadable path all read as "no documents here" (#384).
    """
    try:
        candidates = list(path.iterdir())
    except OSError:
        return
    for candidate in candidates:
        if candidate.is_file():
            yield candidate


def _is_cda_document(candidate: Path) -> bool:
    """Whether ``candidate``'s document element is a CDA ``ClinicalDocument``.

    Decided from the first ``start`` event, never a byte window (rule 56);
    read under the real parse's hardened settings. Never raises.
    """
    try:
        events = etree.iterparse(str(candidate), events=("start",), **_HARDENED_XML_KWARGS)
        for _event, element in events:
            return bool(element.tag == _CDA_ROOT_TAG)
        return False
    except (OSError, etree.LxmlError):
        return False


def _scan(path: Path) -> tuple[list[Path], int]:
    """Contract: CDA documents under ``path`` in filename codepoint order
    (rule 56), and the count of other files that sniff as CDA under an
    unaccepted extension (#384) — never a document :func:`_entries` already
    excluded.
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
        #: One :class:`DocumentLedger` per document the last ``load`` parsed.
        #: Same reset/getattr contract as ``quarantine`` (``sources/base.py``).
        self.ledgers: list[DocumentLedger] = []
        #: Files the last ``load`` recognised as CDA by content but left
        #: unread for an unaccepted extension (#384); never the filenames.
        self.skipped_files: int = 0

    def detect(self, path: Path) -> bool:
        """Cheap sniff: does ``path`` hold a file that reads as a CDA document?

        Never raises: a missing or unlistable path answers ``False`` (via
        :func:`_entries`), never aborts the adapter-detection loop (#384).
        """
        return any(
            candidate.suffix.lower() in _DOCUMENT_SUFFIXES and _is_cda_document(candidate)
            for candidate in _entries(path)
        )

    def load(self, path: Path) -> Iterator[PatientRecord]:
        """Every CDA document in ``path``, filename order, one record each;
        folded into one chart by the pipeline. A failed parse is named by
        position, never filename (rule 56), and refuses the whole run.
        """
        # Reset both before `_scan` runs: a raise mid-scan (#384) must not
        # leave a stale count from the previous run.
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
