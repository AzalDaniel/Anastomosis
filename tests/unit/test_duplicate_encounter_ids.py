"""Two encounters carrying one id: the count and the files must agree.

A C-CDA may list two `<encounter>` entries under one `<id root>`, and the
parser keeps a GUID-shaped root verbatim so re-parsing the same document yields
the same ids. Two visits then arrive as two `Encounter` objects with identical
ids — different dates, different types, one id.

The archive claimed each encounter page by encounter id, and the ledger let a
re-claim by the same id through on purpose (a record legitimately delivered
twice keeps its slot). So the second page landed on the first, nothing raised,
and the run reported one more encounter than it had written. A physician
clicking the May visit read the July one, with nothing anywhere saying so.

Measured on the fixture below before the fix: **reported 3, wrote 2.**

The invariant these pin is the one the count exists to state: the number of
encounters reported equals the number of pages on disk, or the run refuses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anastomosis.deliver._shared import DeliveredNameCollision
from anastomosis.deliver.archive.archive import ArchiveDeliverer
from anastomosis.sources.ccda.parser import parse_document

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE = _FIXTURES / "ccda_edge_cases" / "feedface_ccd_duplicate_encounter_id.xml"
#: The ordinary corpus, for the control case. It lives in a separate directory
#: because the adapter loads a directory by globbing `*.xml`.
ORDINARY = _FIXTURES / "ccda" / "feedface_ccd.xml"
#: The id both `<encounter>` entries in the fixture carry.
SHARED_ID = "feedface-0000-4000-8000-00000000e001"


def test_a_ccda_really_can_hand_us_two_encounters_with_one_id() -> None:
    """Reachability, pinned — this is ordinary input, not a corrupt internal state.

    If a future parser change makes ids unique on its own, this fails and says
    so, rather than leaving the archive's guard defending nothing.
    """
    record = parse_document(FIXTURE)
    sharing = [e for e in record.encounters if e.id == SHARED_ID]

    assert len(sharing) == 2, "the two entries no longer arrive as two encounters"
    assert len({e.date_of_service for e in sharing}) == 2, "and they are genuinely different visits"


def test_the_archive_refuses_rather_than_reporting_a_page_it_did_not_write(
    tmp_path: Path,
) -> None:
    """The count and the files agree, or nothing is delivered."""
    record = parse_document(FIXTURE)
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()

    with pytest.raises(DeliveredNameCollision) as caught:
        ArchiveDeliverer().deliver([record], pdfs, tmp_path / "archive")

    message = str(caught.value)
    assert "same source id" in message
    assert SHARED_ID not in message, "the raw id must not reach the operator's message"


def test_an_ordinary_document_still_delivers(tmp_path: Path) -> None:
    """The guard costs nothing when ids are unique — which is every real run."""
    record = parse_document(ORDINARY)
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    out = tmp_path / "archive"

    result = ArchiveDeliverer().deliver([record], pdfs, out)

    pages = list((out / "patients").rglob("encounters/*.html"))
    assert result.encounter_count == len(pages), (
        f"reported {result.encounter_count} encounters, wrote {len(pages)} pages"
    )
