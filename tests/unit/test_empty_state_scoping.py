"""An empty state may not deny what the record holds: both built-in
packs select by ENCOUNTER, so a section finds nothing whenever its
facts belong to another visit or none at all (#239) — read, to the
person holding the chart, as a statement about the patient.

An encounter-scoped empty state scopes its claim to the visit and says
how many of that family the record holds elsewhere. When the record
holds none either, the original sentence stands unchanged. Counts
only, never values: a measurement is the chart.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from jinja2 import Environment, FileSystemLoader

from anastomosis.core.model import (
    Encounter,
    NoteSection,
    Observation,
    ObservationCategory,
    Patient,
    PatientRecord,
    ScreeningEvent,
    SectionKind,
)
from anastomosis.reconstruct import LoadedPack, discover_packs

PATIENT_ID = "feedface-0236-0000-0000-000000000001"
THIS_VISIT = "feedface-0236-e000-0000-000000000001"
OTHER_VISIT = "feedface-0236-e000-0000-000000000002"

#: An invented measurement name and value. If either ever appears in an absence
#: notice, the notice has started quoting the chart.
VITAL_NAME = "Quillfish pressure"
VITAL_VALUE = "121"


@pytest.fixture(scope="module")
def packs() -> dict[str, LoadedPack]:
    statuses = discover_packs()
    loaded = {name: status.pack for name, status in statuses.items() if status.pack is not None}
    assert {"generic_soap", "practice_fusion_soap"} <= set(loaded)
    return loaded


def _vital(encounter_id: str | None) -> Observation:
    return Observation(
        patient_id=PATIENT_ID,
        encounter_id=encounter_id,
        category=ObservationCategory.VITAL_SIGNS,
        code="8480-6",
        display=VITAL_NAME,
        value=VITAL_VALUE,
        unit="mm[Hg]",
    )


def _record(**collections: Any) -> PatientRecord:
    """One rendered visit plus a second the chart is not about, so a fact can
    sit in the record without belonging to the encounter under test."""
    return PatientRecord(
        patient=Patient(
            id=PATIENT_ID, given_name="Quill", family_name="Sentinel", birth_date=date(1985, 3, 14)
        ),
        encounters=[
            Encounter(
                id=THIS_VISIT,
                patient_id=PATIENT_ID,
                date_of_service=date(2023, 5, 10),
                encounter_type="SOAP",
                sections=[NoteSection(kind=SectionKind.SUBJECTIVE, html="<p>x</p>", text="x")],
            ),
            Encounter(
                id=OTHER_VISIT,
                patient_id=PATIENT_ID,
                date_of_service=date(2023, 1, 4),
                encounter_type="SOAP",
            ),
        ],
        **collections,
    )


def _cfg(pack: LoadedPack, **overrides: bool) -> dict[str, Any]:
    sections = {name: section.default for name, section in pack.manifest.sections.items()}
    sections.update(overrides)
    return {
        "sections": sections,
        "timezone": pack.manifest.timezone,
        "tokens": pack.manifest.tokens,
    }


def _render(pack: LoadedPack, record: PatientRecord, **overrides: bool) -> str:
    env = Environment(
        loader=FileSystemLoader(pack.root), autoescape=True, keep_trailing_newline=True
    )
    template = env.get_template(pack.template_path.name)
    return template.render(
        **pack.build_context(record.encounters[0], record, _cfg(pack, **overrides))
    )


def _context(pack: LoadedPack, record: PatientRecord, **overrides: bool) -> dict[str, Any]:
    return pack.build_context(record.encounters[0], record, _cfg(pack, **overrides))


# --- the count itself ---------------------------------------------------------


@pytest.mark.parametrize("pack_name", ["generic_soap", "practice_fusion_soap"])
@pytest.mark.parametrize(
    ("elsewhere", "expected"),
    [
        # attached to the OTHER visit, and attached to no visit at all: both are
        # in the record and neither can reach this chart.
        ([OTHER_VISIT], 1),
        ([None], 1),
        ([OTHER_VISIT, None], 2),
        ([], 0),
    ],
)
def test_the_count_is_what_the_record_holds_and_this_visit_did_not(
    packs: dict[str, LoadedPack], pack_name: str, elsewhere: list[str | None], expected: int
) -> None:
    record = _record(observations=[_vital(eid) for eid in elsewhere])
    assert _context(packs[pack_name], record)["vitals_elsewhere"] == expected


@pytest.mark.parametrize("pack_name", ["generic_soap", "practice_fusion_soap"])
def test_this_visits_own_vitals_are_not_counted_as_elsewhere(
    packs: dict[str, LoadedPack], pack_name: str
) -> None:
    """The section shows them, so there is nothing to disclose."""
    record = _record(observations=[_vital(THIS_VISIT)])
    assert _context(packs[pack_name], record)["vitals_elsewhere"] == 0


# --- generic_soap -------------------------------------------------------------


def test_generic_soap_says_where_the_vitals_are_instead_of_dropping_the_section(
    packs: dict[str, LoadedPack],
) -> None:
    html = _render(packs["generic_soap"], _record(observations=[_vital(OTHER_VISIT), _vital(None)]))
    assert "None recorded for this visit — 2 elsewhere in the record (see record summary)" in html
    # A count and a pointer, not the measurement.
    assert VITAL_NAME not in html and VITAL_VALUE not in html


def test_generic_soap_stays_silent_when_the_record_has_no_vitals_either(
    packs: dict[str, LoadedPack],
) -> None:
    """The layout has always dropped an empty vitals section, and over a record
    with no vitals that is the truth. Nothing new appears."""
    html = _render(packs["generic_soap"], _record())
    assert "elsewhere in the record" not in html
    assert "Vitals" not in html


def test_generic_soap_shows_the_table_when_the_visit_has_vitals(
    packs: dict[str, LoadedPack],
) -> None:
    html = _render(packs["generic_soap"], _record(observations=[_vital(THIS_VISIT)]))
    assert VITAL_NAME in html and VITAL_VALUE in html
    assert "elsewhere in the record" not in html


def test_a_suppressed_section_makes_no_claim_to_correct(packs: dict[str, LoadedPack]) -> None:
    """``--section vitals=off`` is the operator taking the section off the page.
    Disclosing a count there would put back exactly what they asked to remove.
    """
    record = _record(observations=[_vital(OTHER_VISIT)])
    assert _context(packs["generic_soap"], record, vitals=False)["vitals_elsewhere"] == 0
    assert "elsewhere in the record" not in _render(packs["generic_soap"], record, vitals=False)


# --- practice_fusion_soap -----------------------------------------------------


def test_pf_vitals_empty_state_scopes_itself_to_the_visit(packs: dict[str, LoadedPack]) -> None:
    """Two measurements attached to no visit: this chart has no section that can
    render them, so the count and the pointer are all the page can honestly say.
    """
    html = _render(
        packs["practice_fusion_soap"], _record(observations=[_vital(None), _vital(None)])
    )
    assert (
        "No vitals recorded for this visit — 2 elsewhere in the record (see record summary)" in html
    )
    assert ">No vitals recorded</div>" not in html
    assert VITAL_NAME not in html and VITAL_VALUE not in html


def test_pf_counts_a_prior_visits_vitals_even_though_the_flowsheet_shows_them(
    packs: dict[str, LoadedPack],
) -> None:
    """The count is about THIS visit's section, not about the whole page: the
    flowsheet below carries the prior visit's column, and the encounter block
    still has nothing of its own to show."""
    html = _render(packs["practice_fusion_soap"], _record(observations=[_vital(OTHER_VISIT)]))
    assert (
        "No vitals recorded for this visit — 1 elsewhere in the record (see record summary)" in html
    )


def test_pf_flowsheet_empty_state_scopes_itself_to_prior_visits(
    packs: dict[str, LoadedPack],
) -> None:
    """The flowsheet's window is prior visits, so a vital taken later — or at no
    visit — leaves it empty while the patient plainly has vitals."""
    html = _render(packs["practice_fusion_soap"], _record(observations=[_vital(None)]))
    assert (
        "No events recorded for Vitals at prior visits — 1 elsewhere in the record "
        "(see record summary)." in html
    )
    assert ">No events recorded for Vitals.</div>" not in html


def test_pf_screenings_empty_state_scopes_itself_to_the_visit(
    packs: dict[str, LoadedPack],
) -> None:
    record = _record(
        screening_events=[
            ScreeningEvent(patient_id=PATIENT_ID, encounter_id=OTHER_VISIT, name="Quillfish screen")
        ]
    )
    html = _render(packs["practice_fusion_soap"], record)
    assert (
        "No screenings/interventions/assessments recorded for this visit — 1 elsewhere in "
        "the record (see record summary)." in html
    )
    assert "Quillfish screen" not in html


@pytest.mark.parametrize(
    "plain",
    [
        ">No vitals recorded</div>",
        ">No events recorded for Vitals.</div>",
        ">No screenings/interventions/assessments recorded.</div>",
    ],
)
def test_pf_keeps_its_plain_absence_when_the_record_agrees(
    packs: dict[str, LoadedPack], plain: str
) -> None:
    """Over a record holding none of that family, the vendor's own sentence is
    true — and stays exactly as it was."""
    html = _render(packs["practice_fusion_soap"], _record())
    assert plain in html
    assert "elsewhere in the record" not in html
