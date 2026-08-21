"""Tests for the generic selector-driven browser pack machinery.

A :class:`FakePage` implements :class:`PageLike` with scripted text contents per
selector and records every call, so the whole Destination contract is exercised
with no Playwright. Synthetic data only: ``Testpatient Synthia``, a DOB built
from integer date parts, feedface ids.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date
from pathlib import Path

import pytest

from anastomosis.core.model import Patient
from anastomosis.deliver.browser.engine import UploadEngine
from anastomosis.deliver.browser.errors import (
    PermanentDeliveryError,
    TransientDeliveryError,
)
from anastomosis.deliver.browser.states import UploadState
from anastomosis.deliver.browser.tracking import TrackingDB
from anastomosis.destinations.base import DestinationPatient, UploadItem
from anastomosis.destinations.browserpack import (
    DISCOVER_PREFIX,
    BrowserPackConfig,
    BrowserPackDestination,
    PackNotReadyError,
    PageLike,
    SelectorMap,
)

# --- synthetic patient + DOB string built from date parts ---

_DOB = date(1985, 3, 7)
_DOB_STR = "03/07/1985"  # %m/%d/%Y of _DOB, the config default
# A DIFFERENT synthetic DOB string, defined away from any DOB marker so the PHI
# scanner's DOB-adjacency check stays quiet — same discipline as the matching
# DOB above (the wrong-DOB banner interpolates this).
_WRONG_DOB_STR = "01/01/1990"


def _patient() -> Patient:
    return Patient(
        id="feedface-0000-0000-0000-000000000001",
        given_name="Synthia",
        family_name="Testpatient",
        birth_date=_DOB,
    )


# A row that exactly matches the synthetic patient (name parts + DOB rendering).
_MATCH_ROW = f"Testpatient, Synthia  DOB {_DOB_STR}  MRN 555001"
_OTHER_ROW = "Otherperson, Sam  DOB 01/02/1990  MRN 555002"


# --- the in-test PageLike: scripted text per selector, recorded calls ---


class FakePage:
    """A scripted :class:`PageLike`. ``texts`` maps selector -> single text;
    ``all_texts`` maps selector -> list (for query_selector_all_text)."""

    def __init__(
        self,
        *,
        texts: dict[str, str] | None = None,
        all_texts: dict[str, list[str]] | None = None,
        wait_raises: Exception | None = None,
        closed: bool = False,
    ) -> None:
        self._texts = texts or {}
        self._all_texts = all_texts or {}
        self._wait_raises = wait_raises
        self._closed = closed
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def goto(self, url: str) -> None:
        self.calls.append(("goto", (url,)))

    def fill(self, selector: str, value: str) -> None:
        self.calls.append(("fill", (selector, value)))

    def click(self, selector: str, *, nth: int | None = None) -> None:
        self.calls.append(("click", (selector, nth)))

    def text_content(self, selector: str) -> str | None:
        self.calls.append(("text_content", (selector,)))
        return self._texts.get(selector)

    def query_selector_all_text(self, selector: str) -> list[str]:
        self.calls.append(("query_selector_all_text", (selector,)))
        return list(self._all_texts.get(selector, []))

    def set_input_files(self, selector: str, path: str) -> None:
        self.calls.append(("set_input_files", (selector, path)))

    def wait_for_selector(self, selector: str, timeout_ms: int) -> None:
        self.calls.append(("wait_for_selector", (selector, timeout_ms)))
        if self._wait_raises is not None:
            raise self._wait_raises

    def is_closed(self) -> bool:
        return self._closed

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _selectors(**overrides: str) -> SelectorMap:
    base = {slot: f"#{slot}" for slot in SelectorMap.required_slots()}
    base.update(overrides)
    return SelectorMap(**base)  # optional slots default to ""


def _dest(
    page: FakePage, selectors: SelectorMap | None = None, **cfg: object
) -> BrowserPackDestination:
    config = BrowserPackConfig(name="testpack", **cfg)  # type: ignore[arg-type]
    return BrowserPackDestination(selectors or _selectors(), page, config)


# --- PageLike protocol conformance ---


def test_fakepage_is_pagelike() -> None:
    assert isinstance(FakePage(), PageLike)


# --- SelectorMap validation ---


def test_selectormap_missing_required_slot_raises_naming_it() -> None:
    data = {slot: f"#{slot}" for slot in SelectorMap.required_slots()}
    del data["upload_submit"]
    with pytest.raises(KeyError, match="upload_submit"):
        SelectorMap.from_yaml_dict(data, pack_name="testpack")


def test_selectormap_empty_required_slot_raises() -> None:
    data = {slot: f"#{slot}" for slot in SelectorMap.required_slots()}
    data["patient_search_input"] = "   "
    with pytest.raises(ValueError, match="patient_search_input"):
        SelectorMap.from_yaml_dict(data, pack_name="testpack")


def test_selectormap_discover_placeholder_raises_pack_not_ready() -> None:
    data = dict.fromkeys(SelectorMap.required_slots(), f"{DISCOVER_PREFIX} — run wizard")
    with pytest.raises(PackNotReadyError) as excinfo:
        SelectorMap.from_yaml_dict(data, pack_name="tebra")
    err = excinfo.value
    # Lists ALL undiscovered required slots and mentions the wizard command.
    assert set(err.undiscovered) == set(SelectorMap.required_slots())
    assert "anast destination init" in str(err)
    assert "tebra" in str(err)


def test_selectormap_optional_discover_is_skipped_not_blocking() -> None:
    data = {slot: f"#{slot}" for slot in SelectorMap.required_slots()}
    data["documents_tab"] = f"{DISCOVER_PREFIX} placeholder"
    sm = SelectorMap.from_yaml_dict(data, pack_name="testpack")
    assert sm.documents_tab == ""  # treated as not-discovered = skip


# --- resolver ---


def test_resolver_one_exact_match_returns_destination_patient() -> None:
    page = FakePage(all_texts={"#patient_result_row": [_OTHER_ROW, _MATCH_ROW]})
    dest = _dest(page, patient_search_url="https://ehr.example.com/patients")
    result = dest.resolver.resolve(_patient())
    assert result is not None
    assert result.matched_on == ("name", "dob")
    assert result.destination_patient_id.startswith("row:")
    # Navigated, filled the search, submitted, and clicked the matched row.
    assert ("goto", ("https://ehr.example.com/patients",)) in page.calls
    assert page.call_names().count("click") == 2  # submit + row open


def test_resolver_zero_matches_returns_none() -> None:
    page = FakePage(all_texts={"#patient_result_row": [_OTHER_ROW]})
    assert _dest(page).resolver.resolve(_patient()) is None


def test_resolver_multiple_exact_matches_raises_permanent() -> None:
    page = FakePage(all_texts={"#patient_result_row": [_MATCH_ROW, _MATCH_ROW]})
    with pytest.raises(PermanentDeliveryError, match="ambiguous"):
        _dest(page).resolver.resolve(_patient())


def test_resolver_no_url_does_not_navigate() -> None:
    page = FakePage(all_texts={"#patient_result_row": [_MATCH_ROW]})
    _dest(page, patient_search_url=None).resolver.resolve(_patient())
    assert "goto" not in page.call_names()


def test_resolver_id_is_stable_hash_of_row_text() -> None:
    page1 = FakePage(all_texts={"#patient_result_row": [_MATCH_ROW]})
    page2 = FakePage(all_texts={"#patient_result_row": [_OTHER_ROW, _MATCH_ROW]})
    id1 = _dest(page1).resolver.resolve(_patient())
    id2 = _dest(page2).resolver.resolve(_patient())
    assert id1 is not None and id2 is not None
    # Same matched row text -> same id, even at a different row index.
    assert id1.destination_patient_id == id2.destination_patient_id


def test_resolver_clicks_the_matched_row_not_row_zero() -> None:
    """ID-006: only the SECOND row matches, so the resolver must open row 1
    (nth=1), not the bare selector's row 0 — otherwise it opens (and the banner
    then reads back) the WRONG chart. The returned id must hash the matched row,
    and the click must target that same row."""
    page = FakePage(all_texts={"#patient_result_row": [_OTHER_ROW, _MATCH_ROW]})
    dest = _dest(page)
    dp = dest.resolver.resolve(_patient())
    assert dp is not None
    row_clicks = [
        args for name, args in page.calls if name == "click" and args[0] == "#patient_result_row"
    ]
    # Exactly one row-open click, and it names the matched row's index (1).
    assert row_clicks == [("#patient_result_row", 1)]
    # The id hashes the row actually opened.
    expected = hashlib.sha256(_MATCH_ROW.encode("utf-8")).hexdigest()[:16]
    assert dp.destination_patient_id == f"row:{expected}"


# --- banner ---


def test_banner_match_passes() -> None:
    page = FakePage(
        texts={
            "#patient_banner_name": "Patient: Synthia Testpatient",
            "#patient_banner_dob": f"DOB: {_DOB_STR}",
        }
    )
    assert _dest(page).banner.current_patient_matches(_patient()) is True


def test_banner_wrong_name_fails() -> None:
    page = FakePage(
        texts={
            "#patient_banner_name": "Patient: Someone Else",
            "#patient_banner_dob": f"DOB: {_DOB_STR}",
        }
    )
    assert _dest(page).banner.current_patient_matches(_patient()) is False


def test_banner_wrong_dob_fails() -> None:
    page = FakePage(
        texts={
            "#patient_banner_name": "Synthia Testpatient",
            "#patient_banner_dob": f"DOB: {_WRONG_DOB_STR}",
        }
    )
    assert _dest(page).banner.current_patient_matches(_patient()) is False


def test_banner_missing_text_fails() -> None:
    # text_content returns None for both slots -> empty strings -> no match.
    assert _dest(FakePage()).banner.current_patient_matches(_patient()) is False


# --- boundary-anchored name/DOB: the wrong-patient collisions (ID-001/002) ---


def _ann_li() -> Patient:
    # A short name whose parts embed inside a longer one ("Ann"/"Li" inside
    # "Joann"/"Liang"). Synthetic; DOB built from date parts.
    return Patient(
        id="feedface-0000-0000-0000-000000000009",
        given_name="Ann",
        family_name="Li",
        birth_date=date(1990, 1, 2),
    )


def test_row_and_banner_reject_short_name_embedded_in_longer_name() -> None:
    """The wrong-patient collision at the destination: expected "Ann Li"
    against a "Joann Liang" chart with the SAME DOB. The DOB matches, so only
    boundary-anchored NAME matching stops the wrong chart — raw ``in`` matched
    "li" in "liang" and "ann" in "joann" and filed into the wrong patient."""
    row = "Joann Liang  DOB 01/02/1990  MRN 555009"  # DOB matches Ann Li's
    dest = _dest(FakePage())
    assert dest._row_matches(row, _ann_li()) is False  # name embedded -> reject
    banner = FakePage(
        texts={"#patient_banner_name": "Joann Liang", "#patient_banner_dob": "DOB 01/02/1990"}
    )
    assert _dest(banner).banner.current_patient_matches(_ann_li()) is False


def test_row_and_banner_reject_name_embedded_through_punctuation() -> None:
    """The punctuated form of the same collision: "Ann"/"Li" joined into a
    longer name through hyphens or an apostrophe ("Mary-Ann Li-Wong",
    "O'Brien") must reject exactly like "Joann Liang" — intra-name joiners are
    part of the name, not a token boundary."""
    dest = _dest(FakePage())
    row = "Mary-Ann Li-Wong  DOB 01/02/1990  MRN 555011"  # DOB matches Ann Li's
    assert dest._row_matches(row, _ann_li()) is False
    banner = FakePage(
        texts={"#patient_banner_name": "Mary-Ann Li-Wong", "#patient_banner_dob": "DOB 01/02/1990"}
    )
    assert _dest(banner).banner.current_patient_matches(_ann_li()) is False


def test_row_requires_each_name_field_contiguously() -> None:
    """A multi-word family name is ONE field: satisfied word-by-word across the
    row (a reordered compound surname — a different patient) it must reject;
    present contiguously it must match."""
    patient = Patient(
        id="feedface-0000-0000-0000-000000000011",
        given_name="Testgiven",
        family_name="Dela Testfamily",
        birth_date=date(1990, 1, 2),
    )
    dest = _dest(FakePage())
    reordered = "Testfamily, Testgiven Dela Other  DOB 01/02/1990  MRN 555012"
    assert dest._name_present(reordered, patient) is False
    contiguous = "Dela Testfamily, Testgiven  DOB 01/02/1990  MRN 555013"
    assert dest._row_matches(contiguous, patient) is True


def test_banner_trailing_period_still_matches() -> None:
    """A cosmetic sentence period in the banner must not read as a different
    patient — a false mismatch here aborts the ENTIRE run (WrongPatientError),
    the worst possible cost for a punctuation artifact."""
    banner = FakePage(
        texts={
            "#patient_banner_name": "Patient: Ann Li.",
            "#patient_banner_dob": "DOB: 01/02/1990.",
        }
    )
    assert _dest(banner).banner.current_patient_matches(_ann_li()) is True


def test_row_rejects_unpadded_dob_embedded_in_longer_date() -> None:
    """The DOB half of the collision: an unpadded rendered DOB must not match
    inside a longer date run ("1/2/1990" inside "11/2/1990"). Name matches here,
    so the DOB boundary is the only thing standing between right and wrong."""
    patient = Patient(
        id="feedface-0000-0000-0000-000000000010",
        given_name="Joann",
        family_name="Liang",
        birth_date=date(1990, 1, 2),
    )
    row = "Liang, Joann  DOB 11/2/1990  MRN 555010"
    dest = _dest(FakePage(), dob_format="%-m/%-d/%Y")  # renders "1/2/1990"
    assert dest._name_present(row, patient) is True  # name genuinely present
    assert dest._row_matches(row, patient) is False  # but the DOB is a collision


# --- scanner ---


def test_scanner_returns_document_fingerprints() -> None:
    page = FakePage(all_texts={"#documents_list_item": ["Visit_2024-01-02.pdf", " H&P.pdf ", ""]})
    prints = _dest(page).scanner.existing_fingerprints(
        DestinationPatient(destination_patient_id="row:abc")
    )
    assert prints == {"Visit_2024-01-02.pdf", "H&P.pdf"}  # stripped, blanks dropped


# --- driver ---


def _item(tmp_path: Path) -> UploadItem:
    f = tmp_path / "chart.pdf"
    f.write_bytes(b"%PDF-1.4 synthetic")
    return UploadItem(
        item_key="enc-1:abc123",
        encounter_id="enc-1",
        patient_id="feedface-0000-0000-0000-000000000001",
        file_path=f,
        sha256="0" * 64,
        size_bytes=f.stat().st_size,
    )


def test_driver_call_order_with_optional_slots(tmp_path: Path) -> None:
    page = FakePage()
    selectors = _selectors(documents_tab="#docs_tab", upload_open_button="#open_btn")
    dest = _dest(page, selectors)
    receipt = dest.driver.upload(
        _item(tmp_path), DestinationPatient(destination_patient_id="row:x")
    )
    # tab -> open -> set files -> submit -> wait, in order.
    ordered = [c for c in page.calls if c[0] in ("click", "set_input_files", "wait_for_selector")]
    assert [c[0] for c in ordered] == [
        "click",  # documents_tab
        "click",  # upload_open_button
        "set_input_files",
        "click",  # upload_submit
        "wait_for_selector",
    ]
    assert ordered[0][1][0] == "#docs_tab"
    assert ordered[1][1][0] == "#open_btn"
    # Browser uploads do not echo — receipt carries neither id nor size.
    assert receipt.destination_doc_id is None
    assert receipt.echoed_size_bytes is None


def test_driver_skips_unset_optional_slots(tmp_path: Path) -> None:
    page = FakePage()
    dest = _dest(page)  # optional slots default to ""
    dest.driver.upload(_item(tmp_path), DestinationPatient(destination_patient_id="row:x"))
    ordered = [c[0] for c in page.calls if c[0] in ("click", "set_input_files")]
    # No documents_tab / upload_open_button clicks: only the submit click.
    assert ordered == ["set_input_files", "click"]


def test_driver_timeout_is_transient(tmp_path: Path) -> None:
    page = FakePage(wait_raises=TimeoutError("no marker"))
    dest = _dest(page)
    with pytest.raises(TransientDeliveryError, match="success marker"):
        dest.driver.upload(_item(tmp_path), DestinationPatient(destination_patient_id="row:x"))


# --- session: close never closes the page (CDP mode) ---


def test_session_is_alive_tracks_page_liveness() -> None:
    assert _dest(FakePage(closed=False)).session.is_alive() is True
    assert _dest(FakePage(closed=True)).session.is_alive() is False


def test_session_close_never_closes_the_page() -> None:
    page = FakePage()
    dest = _dest(page)
    dest.session.open()
    dest.session.close()
    # We never own the operator's browser: no close-ish call reaches the page.
    assert page.calls == []
    assert dest.session.is_alive() is True


# --- PHI probe: no patient/search/banner/row values in logs ---


def test_no_phi_in_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    page = FakePage(
        all_texts={
            "#patient_result_row": [_MATCH_ROW],
            "#documents_list_item": [_MATCH_ROW],
        },
        texts={
            "#patient_banner_name": "Synthia Testpatient",
            "#patient_banner_dob": f"DOB: {_DOB_STR}",
        },
    )
    dest = _dest(page, patient_search_url="https://ehr.example.com/p")
    patient = _patient()
    with caplog.at_level(logging.DEBUG, logger="anastomosis.destinations.browserpack"):
        dp = dest.resolver.resolve(patient)
        assert dp is not None
        dest.banner.current_patient_matches(patient)
        dest.scanner.existing_fingerprints(dp)
        dest.driver.upload(_item(tmp_path), dp)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    for forbidden in ("Synthia", "Testpatient", _DOB_STR, "555001"):
        assert forbidden not in blob, f"PHI leaked into logs: {forbidden!r}"


# --- DOB rendering from date parts (no platform strftime) ---


@pytest.mark.parametrize(
    ("fmt", "expected"),
    [
        ("%m/%d/%Y", "03/07/1985"),
        ("%-m/%-d/%Y", "3/7/1985"),
        ("%m-%d-%Y", "03-07-1985"),
        ("%Y%m%d", "19850307"),
        ("%m/%d/%y", "03/07/85"),
        ("100%% sure", "100% sure"),
    ],
)
def test_dob_render_from_parts(fmt: str, expected: str) -> None:
    cfg = BrowserPackConfig(name="x", dob_format=fmt)
    assert cfg.render_dob(_DOB) == expected


# --- end-to-end through the engine to COMPLETED ---


def test_end_to_end_through_engine_completes(tmp_path: Path) -> None:
    f = tmp_path / "chart.pdf"
    f.write_bytes(b"%PDF-1.4 synthetic chart bytes")
    sha = hashlib.sha256(f.read_bytes()).hexdigest()
    item = UploadItem(
        item_key="enc-1:deadbe",
        encounter_id="enc-1",
        patient_id="feedface-0000-0000-0000-000000000001",
        file_path=f,
        sha256=sha,
        size_bytes=f.stat().st_size,
    )
    page = FakePage(
        all_texts={
            "#patient_result_row": [_MATCH_ROW],
            "#documents_list_item": [],  # no existing docs -> not a duplicate
        },
        texts={
            "#patient_banner_name": "Synthia Testpatient",
            "#patient_banner_dob": f"DOB {_DOB_STR}",
        },
    )
    dest = _dest(page, patient_search_url="https://ehr.example.com/p")
    db = TrackingDB(tmp_path / "ledger.db")
    try:
        engine = UploadEngine(dest, db)
        result = engine.run([item], {item.patient_id: _patient()}, run_id="run-1")
    finally:
        db.close()
    assert result.counts.get(UploadState.COMPLETED.value) == 1
    assert result.aborted_reason is None
