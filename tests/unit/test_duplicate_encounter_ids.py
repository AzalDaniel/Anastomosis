"""Two encounters carrying one id: the count and the files must agree.

A C-CDA may list two `<encounter>` entries under one `<id root>` (the
parser keeps a GUID-shaped root verbatim, rule 7), so two visits arrive as
two `Encounter` objects with identical ids — different dates, different
types, one id. Since the archive's re-claim-by-id path also lets a
legitimately re-delivered record keep its slot, a second page could land
on the first silently. The invariant: encounters reported equals pages on
disk, or the run refuses.
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


def test_our_own_round_trip_no_longer_doubles_every_visit() -> None:
    """A C-CDA describes one encounter twice on purpose: an entry in the
    46240-8 Encounters section and the Note Activity in 34109-9, both
    legitimately sharing an ``<id root>``. The parser must fold these into
    one Encounter, not append one per mention (#242)."""
    import tempfile

    from anastomosis.deliver.ccda_export.builder import build_ccd
    from anastomosis.sources import get_source

    fixture = _FIXTURES / "pf_tebra_v9"
    records = list(get_source("pf-tebra").load(fixture))

    for record in records:
        blob = build_ccd(record)
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "ccd.xml"
            path.write_bytes(blob if isinstance(blob, bytes) else blob.encode())
            reingested = parse_document(path)

        assert len(reingested.encounters) == len(record.encounters), "a visit was doubled"
        ids = [e.id for e in reingested.encounters]
        assert len(ids) == len(set(ids)), "two encounters came back sharing one id"

    # And the fold keeps both halves rather than dropping one of them.
    blob = build_ccd(records[0])
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "ccd.xml"
        path.write_bytes(blob if isinstance(blob, bytes) else blob.encode())
        folded = parse_document(path).encounters[0]

    assert folded.encounter_type == "SOAP", "the Encounters half was lost"
    assert folded.note_type == "SOAP note", "the Notes half was lost"
    assert folded.sections, "the note body was lost"
