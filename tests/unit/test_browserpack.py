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
    WrongPatientError,
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
    ``all_texts`` maps selector -> list (for query_selector_all_text).

    The form half models a page that can push back, because that is the only
    way the driver's refusals can be exercised: ``values`` seeds what a control
    already holds (a prefilled patient name), ``options`` says which choices a
    ``<select>`` actually offers (anything else raises, as Playwright does), and
    ``ignores_fill`` names controls that record the fill and keep their old
    value — a date widget that silently declined the text it was handed.
    """

    def __init__(
        self,
        *,
        texts: dict[str, str] | None = None,
        all_texts: dict[str, list[str]] | None = None,
        values: dict[str, str] | None = None,
        options: dict[str, list[str]] | None = None,
        ignores_fill: set[str] | None = None,
        wait_raises: Exception | None = None,
        closed: bool = False,
    ) -> None:
        self._texts = texts or {}
        self._all_texts = all_texts or {}
        self._values = dict(values or {})
        self._options = options or {}
        self._ignores_fill = set(ignores_fill or ())
        self._wait_raises = wait_raises
        self._closed = closed
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def goto(self, url: str) -> None:
        self.calls.append(("goto", (url,)))

    def fill(self, selector: str, value: str) -> None:
        self.calls.append(("fill", (selector, value)))
        if selector not in self._ignores_fill:
            self._values[selector] = value

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

    def select_option(self, selector: str, value: str, *, by_label: bool = False) -> None:
        self.calls.append(("select_option", (selector, value, by_label)))
        offered = self._options.get(selector)
        if offered is not None and value not in offered:
            raise ValueError(f"no option matching {value!r} in {selector}")
        self._values[selector] = value

    def input_value(self, selector: str) -> str:
        self.calls.append(("input_value", (selector,)))
        return self._values.get(selector, "")

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


class _PreFormPage:
    """A page carrying exactly the verbs the seam had before the form slots.

    It can navigate, type, click, read text, attach a file and wait — and that
    is the whole vocabulary. Nothing here can choose an option in a dropdown or
    read a control's value back.
    """

    def goto(self, url: str) -> None: ...

    def fill(self, selector: str, value: str) -> None: ...

    def click(self, selector: str, *, nth: int | None = None) -> None: ...

    def text_content(self, selector: str) -> str | None: ...

    def query_selector_all_text(self, selector: str) -> list[str]: ...

    def set_input_files(self, selector: str, path: str) -> None: ...

    def wait_for_selector(self, selector: str, timeout_ms: int) -> None: ...

    def is_closed(self) -> bool: ...


def test_a_page_without_the_form_verbs_is_not_pagelike() -> None:
    """The seam has to be able to SAY what a filing dialog needs.

    Category, status and provider are dropdowns; the date and the prefilled
    patient are values read back off a control. Without ``select_option`` and
    ``input_value`` a pack could name those fields in its selectors and the
    driver would still have no way to drive or read one, so the slots would be
    decoration. This pins the widening: the old vocabulary is no longer enough.
    """
    assert not isinstance(_PreFormPage(), PageLike)
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


# --- the upload form's slots ---

# The seven slots that describe the filing dialog itself, as opposed to the two
# navigation slots that only get you to it.
_FORM_SLOTS = (
    "upload_filename_input",
    "upload_category_select",
    "upload_status_select",
    "upload_date_input",
    "upload_patient_prefill",
    "upload_provider_select",
    "upload_comments_input",
)


def test_the_upload_form_has_a_slot_for_every_field_it_asks_for() -> None:
    """A pack can name the dialog's fields — and every one of them is optional.

    Optional is not leniency here: ``from_yaml_dict``'s missing-key check
    raises a bare ``KeyError``, not the actionable ``PackNotReadyError``, so a
    newly-REQUIRED slot would hard-crash every ``selectors.yaml`` already
    discovered in the field.
    """
    for slot in _FORM_SLOTS:
        assert slot in SelectorMap.optional_slots(), slot
        assert slot not in SelectorMap.required_slots(), slot
    sm = SelectorMap.from_yaml_dict(
        {s: f"#{s}" for s in SelectorMap.required_slots()}, pack_name="testpack"
    )
    for slot in _FORM_SLOTS:
        assert getattr(sm, slot) == "", slot


def test_a_selectors_file_written_before_the_form_slots_still_loads() -> None:
    """The compatibility floor: an operator's already-discovered file, which
    knows only the original eleven slots, must keep loading and keep meaning
    exactly what it meant."""
    data = {
        slot: f"#{slot}"
        for slot in (*SelectorMap.required_slots(), "documents_tab", "upload_open_button")
    }
    sm = SelectorMap.from_yaml_dict(data, pack_name="testpack")
    assert sm.documents_tab == "#documents_tab"
    assert all(getattr(sm, slot) == "" for slot in _FORM_SLOTS)


def test_an_unknown_selector_slot_is_named_rather_than_dropped() -> None:
    """The silent-drop hole: the loader reads a closed list of slot names, so a
    selector written under any other key used to be read by nobody and reported
    to nobody — while the pack still announced itself ready. An operator who
    discovered a selector and watched the field stay empty had no way to find
    out why."""
    data = {slot: f"#{slot}" for slot in SelectorMap.required_slots()}
    data["upload_categories_select"] = "#a-plausible-typo"  # not a slot name
    with pytest.raises(ValueError, match="upload_categories_select"):
        SelectorMap.from_yaml_dict(data, pack_name="testpack")


def test_the_row_token_outside_the_upload_form_is_refused() -> None:
    """Only the form slots are rendered against a row, so the token anywhere
    else would reach the page verbatim and match nothing."""
    data = {slot: f"#{slot}" for slot in SelectorMap.required_slots()}
    data["patient_result_row"] = "#result-{idx}"
    with pytest.raises(ValueError, match="patient_result_row"):
        SelectorMap.from_yaml_dict(data, pack_name="testpack")


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


def _item(tmp_path: Path, *, dos: date | None = None, fingerprint: str = "") -> UploadItem:
    f = tmp_path / "chart.pdf"
    f.write_bytes(b"%PDF-1.4 synthetic")
    return UploadItem(
        item_key="enc-1:abc123",
        encounter_id="enc-1",
        patient_id="feedface-0000-0000-0000-000000000001",
        file_path=f,
        sha256="0" * 64,
        size_bytes=f.stat().st_size,
        fingerprint=fingerprint,
        date_of_service=dos,
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


def test_driver_uses_no_form_verb_when_no_form_slot_is_discovered(tmp_path: Path) -> None:
    """The backward-compatibility invariant, stated in verbs rather than order.

    A pack discovered before the dialog slots existed must drive the page the
    way it always did: attach the file, submit, wait. Not one fill, dropdown
    choice or value readback may appear — every one of them would be a new
    interaction with a form nobody said was there.
    """
    page = FakePage()
    _dest(page).driver.upload(_item(tmp_path), DestinationPatient(destination_patient_id="row:x"))
    assert page.call_names() == ["set_input_files", "click", "wait_for_selector"]


# --- the upload form: a filing dialog, driven and read back ------------------

# Selector names here are SYNTHETIC and deliberately so. What is being pinned is
# the SHAPE of a real filing dialog — per-document fields numbered by row, one
# provider dropdown shared across rows — not any vendor's actual DOM.
# Transcribing a live portal's selectors into a fixture would be the same
# no-hallucination violation the shipped pack refuses to commit.
_DIALOG_SLOTS = {
    "upload_filename_input": "#doc-name-{idx}",
    "upload_category_select": "#doc-category-{idx}",
    "upload_status_select": "#doc-status-{idx}",
    "upload_date_input": "#doc-date-{idx}",
    "upload_patient_prefill": "#doc-patient-{idx}",
    "upload_provider_select": "#doc-owner",  # shared: no row token
    "upload_comments_input": "#doc-note-{idx}",
}
# What each of those renders to for the one row this engine ever fills.
_ROW0 = {slot: sel.replace("{idx}", "0") for slot, sel in _DIALOG_SLOTS.items()}

_DOS = date(2023, 5, 10)
_DOS_STR = "05/10/2023"  # %m/%d/%Y of _DOS, the config default
_CATEGORY = "Clinical Summary"
_STATUS = "Processed"
_PROVIDER = "Testprovider, Sam"  # synthetic; a provider name is still a person
_NOTE = "Filed from a migrated chart."

_DIALOG_CONFIG: dict[str, object] = {
    "upload_category_label": _CATEGORY,
    "upload_status_label": _STATUS,
    "upload_provider_label": _PROVIDER,
    "upload_comment": _NOTE,
}

# The banner the chart shows before the dialog opens — what the prefill
# readback is checked against.
_BANNER = {
    "#patient_banner_name": "Synthia Testpatient",
    "#patient_banner_dob": f"DOB {_DOB_STR}",
}


def _dialog_page(
    *,
    values: dict[str, str] | None = None,
    options: dict[str, list[str]] | None = None,
    ignores_fill: set[str] | None = None,
) -> FakePage:
    """A page showing the right chart, with a dialog that prefills the patient."""
    seeded = {_ROW0["upload_patient_prefill"]: "Testpatient, Synthia"}
    seeded.update(values or {})
    offered = {
        _ROW0["upload_category_select"]: [_CATEGORY, "Lab Report"],
        _ROW0["upload_status_select"]: [_STATUS, "New"],
        _ROW0["upload_provider_select"]: [_PROVIDER],
    }
    offered.update(options or {})
    return FakePage(texts=_BANNER, values=seeded, options=offered, ignores_fill=ignores_fill)


def _dialog_dest(page: FakePage, **cfg: object) -> BrowserPackDestination:
    """A destination wired to the dialog, with the banner already confirmed.

    The banner readback is run first because that is what the engine does
    immediately before every upload — and the dialog's prefill check asks the
    same question a second time, from inside the dialog.
    """
    config = {**_DIALOG_CONFIG, **cfg}
    dest = _dest(page, _selectors(**_DIALOG_SLOTS), **config)
    assert dest.banner.current_patient_matches(_patient()) is True
    return dest


def _row(dest: BrowserPackDestination, tmp_path: Path, *, dos: date | None = _DOS) -> None:
    """Drive one document through the dialog."""
    dest.driver.upload(_item(tmp_path, dos=dos), DestinationPatient(destination_patient_id="row:x"))


def _submitted(page: FakePage) -> bool:
    return any(name == "click" and args[0] == "#upload_submit" for name, args in page.calls)


def test_driver_fills_the_whole_dialog_then_submits(tmp_path: Path) -> None:
    """The contract the working uploader proved a portal actually demands.

    Attaching the file is not the job: the dialog wants a display name, a
    category, a status, a document date, a provider and a note, and it shows
    which patient it believes it is filing for. This pins the full sequence in
    dialog-reading order, including the two readbacks, and that submit comes
    last — after everything has been checked, never before.
    """
    page = _dialog_page()
    dest = _dialog_dest(page)
    item = _item(tmp_path, dos=_DOS)

    dest.driver.upload(item, DestinationPatient(destination_patient_id="row:x"))

    driven = [c for c in page.calls if c[0] != "text_content"]
    assert driven == [
        ("set_input_files", ("#upload_file_input", str(item.file_path))),
        ("fill", (_ROW0["upload_filename_input"], item.fingerprint)),
        ("select_option", (_ROW0["upload_category_select"], _CATEGORY, True)),
        ("select_option", (_ROW0["upload_status_select"], _STATUS, True)),
        ("fill", (_ROW0["upload_date_input"], _DOS_STR)),
        ("input_value", (_ROW0["upload_date_input"],)),
        ("input_value", (_ROW0["upload_patient_prefill"],)),
        ("select_option", (_ROW0["upload_provider_select"], _PROVIDER, True)),
        ("fill", (_ROW0["upload_comments_input"], _NOTE)),
        ("click", ("#upload_submit", None)),
        ("wait_for_selector", ("#upload_success_marker", 30000)),
    ]


def test_driver_renders_the_row_token_for_the_row_it_fills(tmp_path: Path) -> None:
    """A dialog that numbers its fields per queued document can be described
    once, row-agnostically. Nothing reaches the page still carrying the token."""
    page = _dialog_page()
    dest = _dialog_dest(page)
    _row(dest, tmp_path)
    touched = [str(arg) for _name, args in page.calls for arg in args]
    assert not any("{idx}" in t for t in touched)
    assert any(t == _ROW0["upload_date_input"] for t in touched)


def test_driver_types_the_fingerprint_as_the_display_name(tmp_path: Path) -> None:
    """The display name is not cosmetic: it is what the destination renders in
    its documents list, and the duplicate scan compares that list against the
    item's fingerprint. Typing anything else means a resumed run cannot
    recognise a document it already filed, and files it twice."""
    page = _dialog_page()
    dest = _dialog_dest(page)
    # A fingerprint the pack chose, DIFFERENT from the file name — so typing
    # the file name instead would be visible here rather than coincidental.
    item = _item(tmp_path, dos=_DOS, fingerprint="2023-05-10 Office visit")

    dest.driver.upload(item, DestinationPatient(destination_patient_id="row:x"))

    assert item.fingerprint != item.file_path.name
    assert ("fill", (_ROW0["upload_filename_input"], item.fingerprint)) in page.calls
    # ...and that is exactly the string the scanner looks for on the next run.
    listed = FakePage(all_texts={"#documents_list_item": [item.fingerprint]})
    found = _dest(listed).scanner.existing_fingerprints(
        DestinationPatient(destination_patient_id="row:x")
    )
    assert item.fingerprint in found


def test_driver_selects_by_value_when_the_pack_says_so(tmp_path: Path) -> None:
    """Some portals identify an option by an opaque id rather than its label;
    the pack says which, and the seam carries both."""
    page = _dialog_page(
        options={_ROW0["upload_category_select"]: [_CATEGORY]},
    )
    dest = _dialog_dest(page, select_by="value")
    _row(dest, tmp_path)
    chosen = [args for name, args in page.calls if name == "select_option"]
    assert all(args[2] is False for args in chosen)


# --- the dialog's two gates: the date, and the patient ----------------------


def test_driver_refuses_a_date_the_form_did_not_take(tmp_path: Path) -> None:
    """A date widget that silently ignores the text it is handed keeps showing
    whatever it showed before — and the chart is filed under that date. The
    readback is the only thing that sees it, and it must stop the upload rather
    than retry it: typing the same text again produces the same refusal."""
    page = _dialog_page(
        values={_ROW0["upload_date_input"]: "01/01/2020"},
        ignores_fill={_ROW0["upload_date_input"]},
    )
    dest = _dialog_dest(page)
    with pytest.raises(PermanentDeliveryError, match="upload_date_input"):
        _row(dest, tmp_path)
    assert not _submitted(page)


def test_driver_refuses_a_date_the_form_reordered(tmp_path: Path) -> None:
    """The dangerous echo: the same three numbers, day and month swapped. A
    comparison that only counted digits would wave it through."""
    page = _dialog_page(
        values={_ROW0["upload_date_input"]: "10/05/2023"},  # _DOS is May 10th
        ignores_fill={_ROW0["upload_date_input"]},
    )
    dest = _dialog_dest(page)
    with pytest.raises(PermanentDeliveryError, match="upload_date_input"):
        _row(dest, tmp_path)
    assert not _submitted(page)


def test_driver_accepts_a_date_the_form_merely_reformatted(tmp_path: Path) -> None:
    """The benign echo: the pack types an unpadded date and the widget pads it
    back. Refusing that would strand every chart on a portal that tidies its
    own input, which is most of them."""
    page = _dialog_page(
        values={_ROW0["upload_date_input"]: "05/10/2023"},
        ignores_fill={_ROW0["upload_date_input"]},
    )
    dest = _dialog_dest(page, upload_date_format="%-m/%-d/%Y")
    _row(dest, tmp_path)
    assert ("fill", (_ROW0["upload_date_input"], "5/10/2023")) in page.calls
    assert _submitted(page)


def test_driver_refuses_an_item_with_no_date_of_service(tmp_path: Path) -> None:
    """A pack that discovered the date field has said this portal files by
    date. An item that never had one is a refusal, not a blank — a chart under
    the form's default date is misfiled in the way hardest to notice later."""
    page = _dialog_page()
    dest = _dialog_dest(page)
    with pytest.raises(PermanentDeliveryError, match="upload_date_input"):
        dest.driver.upload(
            _item(tmp_path, dos=None), DestinationPatient(destination_patient_id="row:x")
        )
    assert not _submitted(page)


def test_driver_aborts_when_the_dialog_prefilled_another_patient(tmp_path: Path) -> None:
    """The last wrong-patient gate, and the only one that can see inside the
    dialog. The banner readback happens before the dialog opens; a portal that
    prefills from its own notion of "the current patient" can disagree with the
    chart behind it, and this is where that shows. It raises the same
    WrongPatientError a failed banner does, so the run aborts — not just the
    item."""
    page = _dialog_page(values={_ROW0["upload_patient_prefill"]: "Otherperson, Sam"})
    dest = _dialog_dest(page)
    with pytest.raises(WrongPatientError):
        _row(dest, tmp_path)
    assert not _submitted(page)


def test_driver_refuses_the_prefill_check_with_nothing_to_check_against(
    tmp_path: Path,
) -> None:
    """Fails closed the other way too: a discovered slot says a check was
    expected, so no confirmed patient to compare against is a refusal rather
    than a skipped check."""
    page = _dialog_page()
    dest = _dest(page, _selectors(**_DIALOG_SLOTS), **_DIALOG_CONFIG)  # no banner check run
    with pytest.raises(PermanentDeliveryError, match="upload_patient_prefill"):
        _row(dest, tmp_path)
    assert not _submitted(page)


def test_recycling_the_session_clears_the_confirmed_patient(tmp_path: Path) -> None:
    """The manager close()s and open()s the session every N uploads, and a
    recycled session may come back on a different page. A confirmation that
    outlived the session it was made in would be the one thing able to wave the
    next dialog through unchecked, so it does not survive the recycle."""
    page = _dialog_page()
    dest = _dialog_dest(page)
    dest.session.close()
    dest.session.open()
    with pytest.raises(PermanentDeliveryError, match="upload_patient_prefill"):
        _row(dest, tmp_path)
    assert not _submitted(page)


def test_a_failed_banner_check_clears_the_confirmed_patient(tmp_path: Path) -> None:
    """A banner readback that MISSED must not leave its predecessor's
    confirmation behind — that stale confirmation would be the one thing able
    to wave the next dialog through."""
    page = _dialog_page()
    dest = _dialog_dest(page)  # confirmed the synthetic patient
    assert dest.banner.current_patient_matches(_ann_li()) is False
    with pytest.raises(PermanentDeliveryError, match="upload_patient_prefill"):
        _row(dest, tmp_path)
    assert not _submitted(page)


# --- the dialog's dropdowns -------------------------------------------------


def test_driver_refuses_a_choice_the_dropdown_does_not_offer(tmp_path: Path) -> None:
    """A category list that no longer carries the configured choice is a
    structural mismatch — the portal changed, or the pack points at the wrong
    ``<select>``. Leaving the field at whatever it defaulted to and filing
    anyway is how a chart lands uncategorised."""
    page = _dialog_page(options={_ROW0["upload_category_select"]: ["Lab Report"]})
    dest = _dialog_dest(page)
    with pytest.raises(PermanentDeliveryError, match="upload_category_select"):
        _row(dest, tmp_path)
    assert not _submitted(page)


@pytest.mark.parametrize(
    ("dropped", "slot"),
    [
        ("upload_category_label", "upload_category_select"),
        ("upload_status_label", "upload_status_select"),
        ("upload_provider_label", "upload_provider_select"),
        ("upload_comment", "upload_comments_input"),
    ],
)
def test_driver_refuses_a_discovered_field_the_config_never_filled_in(
    tmp_path: Path, dropped: str, slot: str
) -> None:
    """Half a configuration is worse than none: the pack has said the dialog
    demands this field and nothing has said what belongs in it. Filling it
    anyway would mean inventing a value that lands on a patient's chart."""
    page = _dialog_page()
    dest = _dialog_dest(page, **{dropped: None})
    with pytest.raises(PermanentDeliveryError, match=slot):
        _row(dest, tmp_path)
    assert not _submitted(page)


# --- what the dialog's failures are allowed to say --------------------------


def test_dialog_refusals_name_the_slot_and_never_the_value(tmp_path: Path) -> None:
    """Exception messages are what an operator reads, and these ones sit next
    to the most PHI-dense part of the run. Each names the slot at fault and
    nothing that was typed, read back, or chosen."""
    cases: list[tuple[FakePage, str]] = [
        (
            _dialog_page(
                values={_ROW0["upload_date_input"]: "01/01/2020"},
                ignores_fill={_ROW0["upload_date_input"]},
            ),
            "upload_date_input",
        ),
        (
            _dialog_page(values={_ROW0["upload_patient_prefill"]: "Otherperson, Sam"}),
            "upload_patient_prefill",
        ),
        (
            _dialog_page(options={_ROW0["upload_category_select"]: ["Lab Report"]}),
            "upload_category_select",
        ),
    ]
    for page, slot in cases:
        dest = _dialog_dest(page)
        with pytest.raises(PermanentDeliveryError) as excinfo:
            _row(dest, tmp_path)
        message = str(excinfo.value)
        assert slot in message
        for forbidden in ("Synthia", "Testpatient", "Otherperson", _DOS_STR, _CATEGORY, _NOTE):
            assert forbidden not in message, f"value leaked into a refusal: {forbidden!r}"


def test_no_phi_in_logs_from_the_upload_dialog(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The dialog is the most PHI-dense stretch of the run — a prefilled
    patient name, a date of service, a provider, a free-text note. Every log
    line it produces carries slot names and booleans."""
    page = _dialog_page()
    dest = _dialog_dest(page)
    with caplog.at_level(logging.DEBUG, logger="anastomosis.destinations.browserpack"):
        _row(dest, tmp_path)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    for forbidden in ("Synthia", "Testpatient", _DOS_STR, _CATEGORY, _STATUS, _PROVIDER, _NOTE):
        assert forbidden not in blob, f"PHI leaked into logs: {forbidden!r}"


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


def test_the_document_date_is_written_the_way_that_form_wants_it() -> None:
    """A portal's patient search and its filing dialog need not write a date
    the same way, so the two formats are separate knobs sharing one tokenizer —
    and neither goes through the platform's strftime."""
    cfg = BrowserPackConfig(name="x", dob_format="%m/%d/%Y", upload_date_format="%Y-%m-%d")
    assert cfg.render_dob(_DOB) == _DOB_STR
    assert cfg.render_upload_date(_DOB) == "1985-03-07"


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
