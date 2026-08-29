"""Tests for the PF/Tebra adapter against the synthetic v9 fixture.

Each test asserts one trap documented in tests/fixtures/pf_tebra_v9/README.md.
"""

import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
from anastomosis.core.model import (
    IdentifierKind,
    ObservationCategory,
    PatientRecord,
    SectionKind,
)
from anastomosis.sources import get_source
from anastomosis.sources.pf_tebra.loader import (
    KNOWN_TABLES,
    MalformedExportError,
    OrphanRowsError,
    UnsupportedTablesError,
    read_table,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"

P1 = "feedface-0000-0000-0000-000000000001"
P2 = "feedface-0000-0000-0000-000000000002"
P3 = "feedface-0000-0000-0000-000000000003"
E1 = "feedface-e000-0000-0000-000000000001"
E3 = "feedface-e000-0000-0000-000000000003"
E4 = "feedface-e000-0000-0000-000000000004"
E5 = "feedface-e000-0000-0000-000000000005"
E6 = "feedface-e000-0000-0000-000000000006"
E7 = "feedface-e000-0000-0000-000000000007"  # empty-SOAP, excluded from render
E8 = "feedface-e000-0000-0000-000000000008"  # adult growth chart, excluded


@pytest.fixture(scope="module")
def records() -> dict[str, PatientRecord]:
    adapter = get_source("pf-tebra")
    assert adapter.detect(FIXTURE)
    loaded = {record.patient.id: record for record in adapter.load(FIXTURE)}
    assert len(loaded) == 3
    return loaded


def test_detect_rejects_non_pf_dirs(tmp_path: Path) -> None:
    assert not get_source("pf-tebra").detect(tmp_path)


def test_patient_demographics(records: dict[str, PatientRecord]) -> None:
    ada = records[P1].patient
    assert ada.display_name == "Ada Q Fixture"
    assert ada.birth_date == date(1985, 3, 14)
    assert ada.sex == "Female"
    assert ada.race == ["White", "Asian"]
    assert ada.gender_identity == "Identifies as Female"
    assert ada.identifier(IdentifierKind.SOURCE_GUID) == P1
    assert ada.identifier(IdentifierKind.SSN) == "900-12-3456"
    phones = {t.kind.value: t.value for t in ada.telecom}
    assert phones["phone_home"] == "(206) 555-0142"
    assert phones["phone_mobile"] == "(206) 555-0188"
    assert ada.addresses[0].line1 == "123 Example St"
    assert ada.notes is not None and "Allergy alert" in ada.notes  # pinned note folded in


def test_lossless_extensions_carry_unmapped_columns(records: dict[str, PatientRecord]) -> None:
    ada = records[P1].patient
    assert ada.extensions["pf_tebra:NamePrefix"] == "Ms."
    assert ada.extensions["pf_tebra:IsMultipleBirth"] == "false"
    assert "pf_tebra:PatientCreatedDateTimeUtc" in ada.extensions
    # Mapped columns never duplicate into extensions; sentinel cells vanish.
    assert "pf_tebra:FirstName" not in ada.extensions
    boris = records[P2].patient
    assert "pf_tebra:MothersMaidenName" not in boris.extensions  # was \N


def _export_with_extra_table(
    dst: Path, table: str, header: list[str], rows: list[list[str]]
) -> Path:
    """Copy the fixture and drop in one extra TSV — used to exercise unmapped tables."""
    shutil.copytree(FIXTURE, dst)
    lines = ["\t".join(header), *("\t".join(row) for row in rows)]
    (dst / f"{table}.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dst


def test_unmapped_patient_keyed_table_is_preserved_in_extensions(tmp_path: Path) -> None:
    """A table the adapter does not map is preserved verbatim per patient, not dropped."""
    export = _export_with_extra_table(
        tmp_path / "export",
        "patient-procedures",
        ["PatientPracticeGuid", "ProcedureName", "ProcedureCode"],
        [[P1, "Appendectomy", "44970"]],
    )
    loaded = {record.patient.id: record for record in get_source("pf-tebra").load(export)}
    assert loaded[P1].extensions["pf_tebra:unmapped:patient-procedures"] == [
        {"PatientPracticeGuid": P1, "ProcedureName": "Appendectomy", "ProcedureCode": "44970"}
    ]
    # A patient with no row in the unmapped table gains no such key.
    assert "pf_tebra:unmapped:patient-procedures" not in loaded[P2].extensions


def test_unmapped_orphan_table_refuses_the_run(tmp_path: Path) -> None:
    """A table with data but no patient key fails closed — never silently discarded."""
    export = _export_with_extra_table(
        tmp_path / "export",
        "practice-codebook",
        ["CodeId", "Description"],
        [["X1", "a practice-level lookup with no patient key"]],
    )
    with pytest.raises(UnsupportedTablesError) as exc:
        list(get_source("pf-tebra").load(export))
    assert "practice-codebook" in str(exc.value)


GHOST = "feedface-0000-0000-0000-0000000000ff"  # a guid with no owning record
GISO_TABLE = "patient-gender-identity-sexual-orientation"


@pytest.mark.parametrize(
    ("table", "header", "row"),
    [
        (
            "patient-medications",
            ["PatientPracticeGuid", "MedicationGuid", "MedicationName"],
            [GHOST, "feedface-m000-0000-0000-0000000000ff", "Amoxicillin 500 MG"],
        ),
        ("patient-race", ["PatientPracticeGuid", "RaceName"], [GHOST, "White"]),
        (
            "patient-encounter-addendums",
            ["EncounterGuid", "Addendum"],
            ["feedface-e000-0000-0000-0000000000ff", "An addendum on no known encounter."],
        ),
    ],
)
def test_orphan_row_on_a_known_table_refuses_the_run(
    tmp_path: Path, table: str, header: list[str], row: list[str]
) -> None:
    """A MAPPED table's row whose foreign key names no record fails closed.

    The mapper reads these tables by slicing a per-key grouping, so such a row is
    grouped once and never read again — it would vanish with no sentinel and no
    extension, exactly like an orphan unmapped table."""
    export = _export_with_extra_table(tmp_path / "export", table, header, [row])
    with pytest.raises(OrphanRowsError) as exc:
        list(get_source("pf-tebra").load(export))
    assert table in str(exc.value)
    assert "feedface-" not in str(exc.value)  # table names and counts only, never a key value


def test_every_sliced_table_has_a_foreign_key_check() -> None:
    """Drift guard: every KNOWN table is either checked for foreign-key closure
    or one of the three read in FULL (never sliced by an owning record), so a
    newly mapped table cannot quietly reintroduce the orphan-row hole."""
    from anastomosis.sources.pf_tebra.mapper import _FOREIGN_KEYS

    checked = {table for table, _, _ in _FOREIGN_KEYS}
    assert checked <= set(KNOWN_TABLES)  # every checked table is read by the loader
    assert set(KNOWN_TABLES) - checked == {"providers", "facilities", "superbill-insurances"}


def test_keyless_demographics_row_is_refused(tmp_path: Path) -> None:
    """A demographics row with no PatientPracticeGuid owns no join key, so the
    whole patient would be skipped silently. Fail closed instead."""
    dst = tmp_path / "export"
    shutil.copytree(FIXTURE, dst)
    demo = dst / "patient-demographics.tsv"
    lines = demo.read_text(encoding="utf-8").splitlines()
    keyless = "\t".join(["", *lines[1].split("\t")[1:]])
    demo.write_text("\n".join([*lines, keyless]) + "\n", encoding="utf-8")
    with pytest.raises(OrphanRowsError, match="patient-demographics"):
        list(get_source("pf-tebra").load(dst))


def test_demographics_side_row_surplus_columns_are_preserved(tmp_path: Path) -> None:
    """Reading one column of a race/ethnicity/gender-identity row must not
    consume the whole row: every other populated cell lands in the patient's
    extensions under `pf_tebra:<table>:<row index>:<column>`."""
    dst = tmp_path / "export"
    shutil.copytree(FIXTURE, dst)
    for table in ("patient-race", "patient-ethnicity", GISO_TABLE):
        path = dst / f"{table}.tsv"
        header, *rows = path.read_text(encoding="utf-8").splitlines()
        path.write_text(
            "\n".join([f"{header}\tFutureColumn", *(f"{row}\tSENTINEL-{table}" for row in rows)])
            + "\n",
            encoding="utf-8",
        )
    loaded = {record.patient.id: record for record in get_source("pf-tebra").load(dst)}
    ext = loaded[P1].patient.extensions
    assert ext["pf_tebra:side:patient-race:0:FutureColumn"] == "SENTINEL-patient-race"
    # the second race row keeps its own index
    assert ext["pf_tebra:side:patient-race:1:FutureColumn"] == "SENTINEL-patient-race"
    assert ext["pf_tebra:side:patient-ethnicity:0:FutureColumn"] == "SENTINEL-patient-ethnicity"
    assert ext[f"pf_tebra:side:{GISO_TABLE}:0:FutureColumn"] == f"SENTINEL-{GISO_TABLE}"
    # Columns the fixture already carried beside the mapped one survive too.
    assert ext["pf_tebra:side:patient-race:0:CdcUniqueIdentifier"] == "2106-3"
    assert ext[f"pf_tebra:side:{GISO_TABLE}:0:GenderIdentityCode"] == "446141000124107"
    # Mapped columns are never duplicated into extensions (the _ext contract).
    assert "pf_tebra:side:patient-race:0:RaceName" not in ext
    assert loaded[P1].patient.race == ["White", "Asian"]  # structural mapping unchanged


def test_duplicate_patient_guid_is_refused(tmp_path: Path) -> None:
    """Two demographics rows for one patient fail closed — downstream the QA
    lookup and delivery key on the guid, so the second would silently overwrite
    the first. PHI-safe: the offending guid value never appears in the message."""
    dst = tmp_path / "export"
    shutil.copytree(FIXTURE, dst)
    demo = dst / "patient-demographics.tsv"
    lines = demo.read_text(encoding="utf-8").splitlines()
    demo.write_text("\n".join([*lines, lines[1]]) + "\n", encoding="utf-8")  # dup first patient
    with pytest.raises(ValueError, match="duplicate PatientPracticeGuid") as exc:
        list(get_source("pf-tebra").load(dst))
    assert "feedface-" not in str(exc.value)  # no guid value leaked into the error


def test_row_wider_than_its_header_is_refused(tmp_path: Path) -> None:
    """A row with more columns than the header (an unquoted tab split a cell,
    shifting every later column) fails closed — it must not pass with misaligned
    values. PHI-safe: only the file and line are named, never the row values."""
    (tmp_path / "patient-demographics.tsv").write_text(
        "PatientPracticeGuid\tFirstName\nfeedface-aa\tAda\tEXTRA\n", encoding="utf-8"
    )
    with pytest.raises(MalformedExportError, match="more columns than its header") as exc:
        read_table(tmp_path, "patient-demographics")
    assert "Ada" not in str(exc.value) and "EXTRA" not in str(exc.value)


def test_trailing_empty_column_is_tolerated(tmp_path: Path) -> None:
    """A purely trailing-empty surplus (some exporters append a delimiter to each
    data row) carries no data and misaligns no named column — it is dropped, not
    refused. Only a DATA-bearing surplus (a content-shifting embedded tab) fails."""
    (tmp_path / "patient-demographics.tsv").write_text(
        "PatientPracticeGuid\tFirstName\nfeedface-aa\tAda\t\t\n", encoding="utf-8"
    )
    assert read_table(tmp_path, "patient-demographics") == [
        {"PatientPracticeGuid": "feedface-aa", "FirstName": "Ada"}
    ]


def test_sentinel_cells_mean_absent(records: dict[str, PatientRecord]) -> None:
    boris = records[P2].patient
    assert boris.identifier(IdentifierKind.SSN) is None  # \N
    assert all(t.kind.value != "email" for t in boris.telecom)
    well_child = next(e for e in records[P3].encounters if e.id == E6)
    assert well_child.signed_at is None  # 1/1/0001 12:00:00 AM
    assert well_child.signed_by_id is None
    assert well_child.chief_complaint is None  # \N


def test_encounter_soap_sections_and_html_shadow(records: dict[str, PatientRecord]) -> None:
    encounter = next(e for e in records[P1].encounters if e.id == E1)
    assert encounter.encounter_type == "SOAP"
    subjective = encounter.section(SectionKind.SUBJECTIVE)
    assert subjective is not None
    # html is the sanitize_soap_html rendering path: rich HTML wrapped in
    # pf-rich-text; text is the plain shadow.
    assert subjective.html is not None
    assert subjective.html.startswith('<div class="pf-rich-text">')
    assert "<p>Reports good medication adherence. No dizziness or headache.</p>" in subjective.html
    assert subjective.text == "Reports good medication adherence. No dizziness or headache."
    assert encounter.signed_at == datetime(2023, 5, 10, 21, 32, 11, tzinfo=UTC)
    assert encounter.diagnosis_ids == ["feedface-d000-0000-0000-000000000001"]


def test_simple_note_maps_to_narrative(records: dict[str, PatientRecord]) -> None:
    simple = next(e for e in records[P2].encounters if e.id == E4)
    assert simple.encounter_type == "SIMPLE"
    assert [s.kind for s in simple.sections] == [SectionKind.NARRATIVE]
    assert simple.sections[0].text is not None and "Nurse visit" in simple.sections[0].text


def test_invalid_encounters_excluded_from_render_but_preserved(
    records: dict[str, PatientRecord],
) -> None:
    # Render SELECTION (see _skip_reason): empty-SOAP and adult-growth-chart
    # encounters are not rendered. Losslessness: they are kept in
    # record.extensions instead of being dropped.
    boris = records[P2]
    rendered_ids = {e.id for e in boris.encounters}
    assert E7 not in rendered_ids  # empty SOAP
    assert E8 not in rendered_ids  # adult growth chart
    assert {E4, E5} <= rendered_ids  # the valid ones still render
    skipped = boris.extensions["pf_tebra:skipped_encounters"]
    by_id = {entry["encounter"]["id"]: entry["reason"] for entry in skipped}
    assert by_id[E7] == "empty_soap"
    assert by_id[E8] == "adult_growth_chart"


def test_same_day_encounters_exist_for_collision_handling(
    records: dict[str, PatientRecord],
) -> None:
    # The fixture must keep offering the same-day pair the renderer's
    # filename-collision logic is tested against.
    dates = [e.date_of_service for e in records[P1].encounters if e.date_of_service]
    assert dates.count(date(2023, 5, 10)) == 2


def test_addendum_attached(records: dict[str, PatientRecord]) -> None:
    annual = next(e for e in records[P1].encounters if e.id == E3)
    assert len(annual.addenda) == 1
    addendum = annual.addenda[0]
    assert addendum.status == "Accepted"
    assert addendum.text is not None and "lipid panel" in addendum.text


def test_encounter_link_tables_grouped_once_preserve_per_encounter_order() -> None:
    """The encounter-keyed addendum/diagnosis link tables are grouped ONCE for the
    whole export and sliced per encounter (a perf hoist). Interleaved source rows
    must still land on the right encounter in source order — i.e. the hoisted
    index is byte-for-byte equivalent to the old per-encounter rebuild."""
    from anastomosis.sources.pf_tebra.loader import KNOWN_TABLES
    from anastomosis.sources.pf_tebra.mapper import map_export

    pat = "feedface-0000-0000-0000-0000000000f0"
    e1 = "feedface-e000-0000-0000-0000000000f1"
    e2 = "feedface-e000-0000-0000-0000000000f2"
    export: dict[str, list[dict[str, str | None]]] = {name: [] for name in KNOWN_TABLES}
    export["patient-demographics"] = [{"PatientPracticeGuid": pat, "IsActive": "true"}]
    export["patient-encounters"] = [
        {"EncounterGuid": e, "PatientPracticeGuid": pat, "IsSoapNote": "true", "Subjective": "s"}
        for e in (e1, e2)
    ]
    # Interleave rows across the two encounters so per-encounter order is testable.
    export["patient-encounter-addendums"] = [
        {"EncounterGuid": e1, "Addendum": "a1"},
        {"EncounterGuid": e2, "Addendum": "b1"},
        {"EncounterGuid": e1, "Addendum": "a2"},
    ]
    export["patient-encounter-diagnoses"] = [
        {"EncounterGuid": e2, "DiagnosisGuid": "dx-b1"},
        {"EncounterGuid": e1, "DiagnosisGuid": "dx-a1"},
        {"EncounterGuid": e2, "DiagnosisGuid": "dx-b2"},
    ]
    record = next(iter(map_export(export)))
    by_id = {e.id: e for e in record.encounters}
    assert [a.text for a in by_id[e1].addenda] == ["a1", "a2"]
    assert [a.text for a in by_id[e2].addenda] == ["b1"]
    assert by_id[e1].diagnosis_ids == ["dx-a1"]
    assert by_id[e2].diagnosis_ids == ["dx-b1", "dx-b2"]


def test_encounter_diagnosis_link_surplus_columns_preserved(tmp_path: Path) -> None:
    """A column beyond EncounterGuid/DiagnosisGuid on a diagnosis-link row
    survives on the encounter it links, not just the ones the mapper reads."""
    dst = tmp_path / "export"
    shutil.copytree(FIXTURE, dst)
    path = dst / "patient-encounter-diagnoses.tsv"
    header, *rows = path.read_text(encoding="utf-8").splitlines()
    path.write_text(
        "\n".join(
            [f"{header}\tZZExtraColumn", *(f"{row}\tSENTINEL-{i}" for i, row in enumerate(rows))]
        )
        + "\n",
        encoding="utf-8",
    )
    loaded = {record.patient.id: record for record in get_source("pf-tebra").load(dst)}
    e1 = next(e for e in loaded[P1].encounters if e.id == E1)
    ext = e1.extensions
    assert ext["pf_tebra:side:patient-encounter-diagnoses:0:ZZExtraColumn"] == "SENTINEL-0"
    # The link row's own (redundant) PatientPracticeGuid survives alongside it.
    assert ext["pf_tebra:side:patient-encounter-diagnoses:0:PatientPracticeGuid"] == P1
    # Mapped columns are never duplicated into extensions.
    assert "pf_tebra:side:patient-encounter-diagnoses:0:DiagnosisGuid" not in ext
    # structural mapping unchanged
    assert e1.diagnosis_ids == ["feedface-d000-0000-0000-000000000001"]


def test_bmi_auto_calc_trigger(records: dict[str, PatientRecord]) -> None:
    obs_e1 = records[P1].observations_for(E1)
    bmi = next(o for o in obs_e1 if o.code == "39156-5")
    # 2 decimal places for BMI. Weight charted as 29463-7
    # (modern alias) still fires the trigger keyed on 3141-9-or-alias.
    assert bmi.value == "25.75"  # round(703 * 150 / 64^2, 2)
    assert bmi.extensions["pf_tebra:computed"] == "bmi_auto_calc"
    # Pediatric encounter gets one too (height+weight, no explicit BMI).
    assert any(o.code == "39156-5" for o in records[P3].observations_for(E6))


def test_explicit_bmi_is_never_recomputed(records: dict[str, PatientRecord]) -> None:
    obs_e5 = [o for o in records[P2].observations_for(E5) if o.code == "39156-5"]
    assert len(obs_e5) == 1
    assert obs_e5[0].value == "30.0"
    assert "pf_tebra:computed" not in obs_e5[0].extensions


def test_vitals_are_loinc_categorized(records: dict[str, PatientRecord]) -> None:
    obs_e1 = records[P1].observations_for(E1)
    vitals = {o.code for o in obs_e1 if o.category == ObservationCategory.VITAL_SIGNS}
    # 29463-7 (weight) and 59408-5 (O2) are charted as the MODERN aliases here
    # and still categorize as vitals (dual-map: old primary + new alias).
    assert {"8302-2", "29463-7", "8480-6", "8462-4", "8867-4", "72514-3"} <= vitals
    pain = next(o for o in obs_e1 if o.code == "72514-3")
    assert pain.value == "4"
    # Head circumference charted as the modern 9843-4 alias still categorizes.
    head_circ = next(o for o in records[P3].observations_for(E6) if o.code == "9843-4")
    assert head_circ.category == ObservationCategory.VITAL_SIGNS
    assert head_circ.value == "18.5"


def test_social_history_observations(records: dict[str, PatientRecord]) -> None:
    ada_social = [
        o for o in records[P1].observations if o.category == ObservationCategory.SOCIAL_HISTORY
    ]
    by_label = {o.display: o.value for o in ada_social}
    assert by_label["Tobacco use"] == "Former smoker"
    assert by_label["Occupation"] == "Carpenter"
    assert by_label["Industry"] == "Construction"
    boris_social = {
        o.display: o.value
        for o in records[P2].observations
        if o.category == ObservationCategory.SOCIAL_HISTORY
    }
    assert boris_social["Education"] == "High school graduate"


def test_conditions_parse_code_equivalents(records: dict[str, PatientRecord]) -> None:
    htn = next(c for c in records[P1].conditions if c.display == "Essential hypertension")
    assert (htn.icd10, htn.snomed, htn.active) == ("I10", "59621000", True)
    derm = next(c for c in records[P1].conditions if "dermatitis" in (c.display or ""))
    assert derm.active is False  # has a StopDate
    well = records[P3].conditions[0]
    assert well.icd10 == "Z00.129" and well.snomed is None


def test_allergies_with_joined_reactions(records: dict[str, PatientRecord]) -> None:
    penicillin = records[P1].allergies[0]
    assert penicillin.category.value == "drug"
    assert penicillin.severity == "Severe"
    assert penicillin.reactions == ["Hives", "Anaphylaxis"]


def test_allergy_reaction_link_surplus_columns_preserved(tmp_path: Path) -> None:
    """A column beyond AllergyGuid/Reaction on a reaction-link row survives on
    the allergy it links, not just the ones the mapper reads."""
    dst = tmp_path / "export"
    shutil.copytree(FIXTURE, dst)
    path = dst / "patient-allergy-reactions.tsv"
    header, *rows = path.read_text(encoding="utf-8").splitlines()
    path.write_text(
        "\n".join(
            [f"{header}\tZZExtraColumn", *(f"{row}\tSENTINEL-{i}" for i, row in enumerate(rows))]
        )
        + "\n",
        encoding="utf-8",
    )
    loaded = {record.patient.id: record for record in get_source("pf-tebra").load(dst)}
    penicillin = loaded[P1].allergies[0]
    ext = penicillin.extensions
    assert ext["pf_tebra:side:patient-allergy-reactions:0:ZZExtraColumn"] == "SENTINEL-0"
    assert ext["pf_tebra:side:patient-allergy-reactions:1:ZZExtraColumn"] == "SENTINEL-1"
    # ReactionSnomedCode was dropped before this fix; now preserved too.
    assert ext["pf_tebra:side:patient-allergy-reactions:0:ReactionSnomedCode"] == "247472004"
    assert "pf_tebra:side:patient-allergy-reactions:0:Reaction" not in ext  # mapped, not duplicated
    assert penicillin.reactions == ["Hives", "Anaphylaxis"]  # structural mapping unchanged


def test_medication_activity_and_prescription_links(
    records: dict[str, PatientRecord],
) -> None:
    lisinopril = next(m for m in records[P1].medications if m.generic_name == "lisinopril")
    assert lisinopril.active is True
    # Two scripts now link to lisinopril (Rx 1 dispensed, Rx 3 verified-only).
    assert sorted(lisinopril.prescription_ids) == [
        "feedface-0e5c-0000-0000-000000000001",
        "feedface-0e5c-0000-0000-000000000003",
    ]
    metformin = next(m for m in records[P2].medications if m.generic_name == "metformin")
    assert metformin.active is False  # stopped + discontinued reason


def test_escript_status_resolution(records: dict[str, PatientRecord]) -> None:
    # Resolution runs on _ESCRIPT_LABEL_MAP keyed on the transaction
    # DESCRIPTION: dispensing (100) beats the refill (10) and the order-sent
    # VERIFIED (50).
    sent_rx = next(rx for rx in records[P1].prescriptions if rx.id.endswith("000000000001"))
    assert sent_rx.prefix == "ESCRIPT"
    assert sent_rx.status_label == "DISPENSED"
    assert [t.kind for t in sent_rx.transactions] == ["Sent", "Verified", "Dispensed"]
    printed_rx = records[P2].prescriptions[0]
    assert printed_rx.prefix == "SCRIPT"  # "Prescription printed" → SCRIPT
    assert printed_rx.status_label == "PRINTED"
    assert printed_rx.refills is None  # -1 sentinel


def test_escript_refill_does_not_override_verified(records: dict[str, PatientRecord]) -> None:
    # The refill-vs-verified rule: Order sent + Refill request approved with no
    # dispense resolves to VERIFIED — refills (priority 10) never beat the
    # baseline VERIFIED (priority 50).
    rx = next(rx for rx in records[P1].prescriptions if rx.id.endswith("000000000003"))
    descriptions = {t.description for t in rx.transactions}
    assert descriptions == {"Order sent", "Refill request approved"}
    assert rx.status_label == "VERIFIED"


def test_escript_display_date_uses_order_sent_eastern(records: dict[str, PatientRecord]) -> None:
    # ESCRIPT display date = the Order-sent transaction datetime converted to
    # practice-local Eastern (see resolve_display_date). Order sent
    # at 5/10/2023 9:40 PM UTC → 5:40 PM US/Eastern (EDT, UTC-4), same day.
    sent_rx = next(rx for rx in records[P1].prescriptions if rx.id.endswith("000000000001"))
    assert sent_rx.display_date is not None
    assert sent_rx.display_date.strftime("%Y-%m-%d %H:%M") == "2023-05-10 17:40"
    # SCRIPT (paper) falls back to the prescription DoS, not tz-converted.
    printed_rx = records[P2].prescriptions[0]
    assert printed_rx.display_date is not None
    assert printed_rx.display_date.date() == date(2023, 9, 15)


def test_plan_type_superbill_join_with_regex_fallback(
    records: dict[str, PatientRecord],
) -> None:
    # The three-tier superbill PlanType join: PIPG tier-1, plan-name tier-2,
    # payer tier-3, then the "(PPO)" regex as the heuristic of last resort.
    ada_coverages = {c.plan_name: c for c in records[P1].coverages}
    # Cascadia has NO superbill row → regex last-resort on "(PPO)".
    ppo = ada_coverages["Cascadia Choice (PPO)"]
    assert ppo.plan_type == "PPO"
    assert ppo.order_of_benefits == 0
    assert ppo.priority_label == "PRIMARY PAYER"
    # Evergreen Basic resolves via the superbill PLAN-NAME tier (no "(PPO)" in
    # the name) — proving the join beats the name-regex heuristic.
    basic = ada_coverages["Evergreen Basic"]
    assert basic.plan_type == "HMO"
    assert basic.order_of_benefits == 1
    # Medicare resolves via the superbill PIPG tier — TYPE is never "Medical"
    # (the predecessor's insurance QA fails on "Medical").
    medicare = records[P2].coverages[0]
    assert medicare.plan_type == "Medicare"
    assert medicare.coverage_type == "Medical"
    assert records[P3].coverages == []  # self-pay


def test_superbill_insurance_surplus_columns_preserved_on_joined_coverage(
    tmp_path: Path,
) -> None:
    """Every superbill-insurances column beyond the PIPG/name/PlanType join
    survives on the coverage its join actually won — proven for both the exact
    PIPG tier (Medicare) and the plan-name tier (Evergreen Basic), not just the
    columns _PlanTypeLookup itself reads."""
    dst = tmp_path / "export"
    shutil.copytree(FIXTURE, dst)
    path = dst / "superbill-insurances.tsv"
    header, *rows = path.read_text(encoding="utf-8").splitlines()
    path.write_text(
        "\n".join(
            [f"{header}\tZZExtraColumn", *(f"{row}\tSENTINEL-{i}" for i, row in enumerate(rows))]
        )
        + "\n",
        encoding="utf-8",
    )
    loaded = {record.patient.id: record for record in get_source("pf-tebra").load(dst)}
    medicare = loaded[P2].coverages[0]  # row 0: PIPG tier-1 join
    assert medicare.extensions["pf_tebra:side:superbill-insurances:ZZExtraColumn"] == "SENTINEL-0"
    # row 1: name tier-2 join
    basic = next(c for c in loaded[P1].coverages if c.plan_name == "Evergreen Basic")
    assert basic.extensions["pf_tebra:side:superbill-insurances:ZZExtraColumn"] == "SENTINEL-1"
    # Columns the join itself reads are never duplicated into the residual.
    assert "pf_tebra:side:superbill-insurances:PlanType" not in basic.extensions
    assert medicare.plan_type == "Medicare"  # typed slot unchanged by the added residual


def test_superbill_insurance_unjoined_row_preserved_non_attributingly(tmp_path: Path) -> None:
    """A superbill row whose PIPG/plan-name/payer-name join wins no coverage at
    all must still survive — non-attributingly (the record, not a nonexistent
    Coverage), per the table's read-in-full/never-orphaned convention."""
    dst = tmp_path / "export"
    shutil.copytree(FIXTURE, dst)
    path = dst / "superbill-insurances.tsv"
    orphan_row = "\t".join(
        [
            "feedface-5b11-0000-0000-000000000099",  # SuperbillGuid
            "",  # PatientInsurancePlanGuid — matches no coverage
            P3,  # PatientPracticeGuid — Cleo is self-pay, no coverages at all
            "Acme Indemnity",  # PayerName — matches no coverage payer
            "Acme Catastrophic",  # PlanName — matches no coverage plan name
            "EPO",  # PlanType
            "6/2/2023 1:45:09 PM",  # LastModifiedDateTimeUtc
        ]
    )
    path.write_text(path.read_text(encoding="utf-8") + orphan_row + "\n", encoding="utf-8")
    loaded = {record.patient.id: record for record in get_source("pf-tebra").load(dst)}
    assert loaded[P3].coverages == []  # still self-pay: the orphan row joins nothing
    assert loaded[P3].extensions["pf_tebra:unjoined_superbill_insurances"] == [
        {
            "pf_tebra:SuperbillGuid": "feedface-5b11-0000-0000-000000000099",
            "pf_tebra:PayerName": "Acme Indemnity",
            "pf_tebra:PlanName": "Acme Catastrophic",
            "pf_tebra:PlanType": "EPO",
            "pf_tebra:LastModifiedDateTimeUtc": "6/2/2023 1:45:09 PM",
        }
    ]
    # The two real fixture rows still join (P1, P2 unaffected by the orphan add).
    assert "pf_tebra:unjoined_superbill_insurances" not in loaded[P1].extensions
    assert "pf_tebra:unjoined_superbill_insurances" not in loaded[P2].extensions


def test_family_history_immunizations_directives(
    records: dict[str, PatientRecord],
) -> None:
    family = records[P1].family_history[0]
    assert (family.relation, family.diagnosis) == ("Mother", "Type 2 diabetes mellitus")
    cleo = records[P3]
    flu = next(i for i in cleo.immunizations if "Influenza" in (i.vaccine or ""))
    assert flu.administered_on == date(2022, 10, 3)
    assert flu.lot_number == "FLU2023A"
    dnr = records[P2].advance_directives[0]
    assert dnr.directive is not None and dnr.directive.startswith("Do not resuscitate")


def test_goals_are_mapped_and_split_by_active(tmp_path: Path) -> None:
    """``patient-goals`` feeds ``record.goals``, which the SOAP pack's Active and
    Inactive Goals sections read. Until it was mapped those sections printed
    "No active goals recorded" over an export that had goals in it (#236).
    """
    export = _export_with_extra_table(
        tmp_path / "export",
        "patient-goals",
        ["PatientPracticeGuid", "Goal", "StartDate", "IsActive", "CodeDescription"],
        [
            [P1, "Walk 30 minutes daily", "01/02/2023", "True", "Exercise"],
            [P1, "Quit tobacco", "01/02/2022", "False", "Tobacco cessation"],
        ],
    )
    loaded = {record.patient.id: record for record in get_source("pf-tebra").load(export)}
    walk, quit_tobacco = loaded[P1].goals
    assert (walk.description, walk.effective, walk.active) == (
        "Walk 30 minutes daily",
        date(2023, 1, 2),
        True,
    )
    assert (quit_tobacco.description, quit_tobacco.active) == ("Quit tobacco", False)
    # Columns the mapper does not read ride along instead of being dropped.
    assert walk.extensions["pf_tebra:CodeDescription"] == "Exercise"
    # A patient with no goal rows gets an empty list, not a phantom goal.
    assert loaded[P2].goals == []
    # And the table is mapped now, so it must not ALSO be preserved as unmapped.
    assert "pf_tebra:unmapped:patient-goals" not in loaded[P1].extensions


def test_guarantor_and_shared_actors(records: dict[str, PatientRecord]) -> None:
    cleo = records[P3]
    guarantor = cleo.patient.guarantor
    assert guarantor is not None
    assert guarantor.name == "Gus Placeholder"
    # The Billing* / bare City-State-Zip columns are this table's real names —
    # this pins them so the wrong Address*/RelationshipTo names (which silently
    # map nothing) cannot creep back.
    assert guarantor.relationship_to_patient == "Parent"
    assert guarantor.birth_date == date(1988, 3, 15)
    assert guarantor.sex == "Male"
    assert guarantor.ssn is None  # empty SSNumber cell stays None, never ""
    assert guarantor.payment_preference is None  # empty BillingPaymentType
    assert guarantor.address is not None and guarantor.address.city == "Springfield"
    assert [(p.kind.value, p.value) for p in guarantor.phones] == [("phone_home", "(206) 555-0163")]
    # Columns the mapping doesn't consume land in extensions (losslessness).
    assert guarantor.extensions == {"pf_tebra:MiddleName": "Q"}
    record = records[P1]
    encounter = next(e for e in record.encounters if e.id == E1)
    provider = record.practitioner(encounter.provider_id)
    assert provider is not None and provider.name == "Paige Providerson"
    facility = record.facility(encounter.facility_id)
    assert facility is not None and facility.name == "Example Family Medicine"
    assert facility.phone == "(206) 555-0199"


def test_provenance_traces_to_source(records: dict[str, PatientRecord]) -> None:
    ada = records[P1]
    assert ada.patient.provenance is not None
    assert ada.patient.provenance.source_file == "patient-demographics.tsv"
    assert ada.patient.provenance.source_id == P1
    assert ada.encounters[0].provenance is not None
    assert ada.encounters[0].provenance.source_file == "patient-encounters.tsv"


def test_patient_med_history_freetext_blocks_mapped(records: dict[str, PatientRecord]) -> None:
    """patient-med-history (verified against a real v9 export) maps each free-prose
    block to PastMedicalHistory(kind, text) — social/family/major-events — which
    the PF pack renders as the social-history freetext + the PMH sections. The
    structured subcategories the predecessor showed empty (alcohol, drug use,
    physical activity, diet, sexual activity, ...) have NO source table: absent."""
    pmh = records[P1].past_medical_history
    by_kind = {(p.kind or "").lower(): (p.text or "") for p in pmh}
    assert any("social" in k for k in by_kind)
    assert "retired librarian" in next(t for k, t in by_kind.items() if "social" in k)
    assert any("family" in k for k in by_kind)
    assert any("past medical" in k for k in by_kind)
    # A patient with no history blocks gets an empty list (no crash, nothing invented).
    assert records[P3].past_medical_history == []


def test_social_observation_prefers_clinical_effective_date() -> None:
    """effective_at on a social-history observation is the clinical EffectiveDate,
    not the administrative RecordedDate (the two differ in a real v9 export).
    RecordedDate is only a last-resort fallback when no clinical date is present;
    the non-chosen date is preserved verbatim in extensions (lossless)."""
    from anastomosis.core.timeutil import parse_dt
    from anastomosis.sources.pf_tebra.mapper import _social_observations

    guid = "feedface-0000-0000-0000-0000000000aa"
    empty_socials = {
        "occupation-industry": [],
        "patient-education": [],
        "patient-financial-resources": [],
        "tribal-affiliation": [],
    }

    # Both dates present: the clinical EffectiveDate wins over RecordedDate.
    both = {
        "patient-smokingstatus": [
            {
                "PatientPracticeGuid": guid,
                "TobaccoUseDescription": "Never smoker",
                "EffectiveDate": "2021-03-15",
                "RecordedDate": "2023-09-01",
            }
        ],
        **empty_socials,
    }
    obs = _social_observations(both, guid)
    assert len(obs) == 1
    assert obs[0].effective_at == parse_dt("2021-03-15")  # EffectiveDate, not RecordedDate
    assert obs[0].extensions["pf_tebra:RecordedDate"] == "2023-09-01"  # not lost

    # Only the administrative date present: it is the last-resort fallback.
    only_recorded = {
        "patient-smokingstatus": [
            {
                "PatientPracticeGuid": guid,
                "TobaccoUseDescription": "Never smoker",
                "RecordedDate": "2023-09-01",
            }
        ],
        **empty_socials,
    }
    obs2 = _social_observations(only_recorded, guid)
    assert obs2[0].effective_at == parse_dt("2023-09-01")


# --- the superbill TYPE join must not carry one patient's row onto another ------


_SHARED_PLAN_ROW = (
    "\t{guid}\tEvergreen Mutual\tEvergreen Basic\tMedical\tSelf\tEM55099\t\t"
    "Primary\t1/1/2023\t\t\\N\ttrue\t\t6/2/2023 1:45:09 PM\n"
)


def _fixture_with_second_patient_on_p1s_plan(dst: Path) -> Path:
    """P3 joins the plan P1's only superbill row describes.

    The row has no PatientInsurancePlanGuid, so P3's coverage can only reach it
    through the plan-name tier — the tier that matches across patients.
    """
    shutil.copytree(FIXTURE, dst)
    with (dst / "patient-insurances.tsv").open("a", encoding="utf-8") as fh:
        fh.write(_SHARED_PLAN_ROW.format(guid=P3))
    return dst


def test_shared_plan_name_does_not_carry_another_patients_row(tmp_path: Path) -> None:
    """Two patients on one plan: neither may receive the other's identifiers.

    The plan-name and payer-name tiers match across patients by construction —
    only the first superbill row per plan name is indexed — so before this was
    scoped, P1's PatientPracticeGuid and SuperbillGuid rode onto P3's Coverage
    and into P3's exported bundle.
    """
    export = _fixture_with_second_patient_on_p1s_plan(tmp_path / "export")
    records = {record.patient.id: record for record in get_source("pf-tebra").load(export)}

    for patient_id, record in records.items():
        for coverage in record.coverages:
            for key, value in coverage.extensions.items():
                if key.endswith("side:superbill-insurances:PatientPracticeGuid"):
                    assert value == patient_id, (
                        "a superbill row from another patient reached this coverage"
                    )


def test_shared_plan_name_still_resolves_the_type(tmp_path: Path) -> None:
    """The TYPE itself is a fact about the PLAN, so reading it across patients
    is correct and must survive the scoping above."""
    export = _fixture_with_second_patient_on_p1s_plan(tmp_path / "export")
    records = {record.patient.id: record for record in get_source("pf-tebra").load(export)}

    evergreen = [c for c in records[P3].coverages if c.plan_name == "Evergreen Basic"]
    assert evergreen and evergreen[0].plan_type == "HMO"


def test_a_row_lent_across_patients_is_still_preserved_for_its_own(tmp_path: Path) -> None:
    """A superbill row whose join won only ANOTHER patient's coverage was never
    read for its own patient, so it is still unjoined and must be preserved —
    counting the cross-patient read as consumption dropped it from the export."""
    export = _fixture_with_second_patient_on_p1s_plan(tmp_path / "export")
    insurances = (export / "patient-insurances.tsv").read_text(encoding="utf-8").splitlines()
    kept = [line for line in insurances if "feedface-c0fe-0000-0000-000000000002" not in line]
    (export / "patient-insurances.tsv").write_text("\n".join(kept) + "\n", encoding="utf-8")

    records = {record.patient.id: record for record in get_source("pf-tebra").load(export)}
    preserved = records[P1].extensions.get("pf_tebra:unjoined_superbill_insurances")
    assert preserved and len(preserved) == 1


def test_unjoined_superbill_row_with_no_home_patient_refuses(tmp_path: Path) -> None:
    """Unjoined rows are placed by their own PatientPracticeGuid, so one naming
    nobody in the export would vanish. superbill-insurances sits outside
    _check_key_closure, so this is the only place that can catch it."""
    export = tmp_path / "export"
    shutil.copytree(FIXTURE, export)
    with (export / "superbill-insurances.tsv").open("a", encoding="utf-8") as fh:
        fh.write(
            "feedface-5b11-0000-0000-000000000009\t\tfeedface-0000-0000-0000-000000000099\t"
            "Nowhere Health\tNowhere Plan\tPPO\t6/2/2023 1:45:09 PM\n"
        )

    with pytest.raises(OrphanRowsError) as excinfo:
        list(get_source("pf-tebra").load(export))
    assert "superbill-insurances" in str(excinfo.value)


def test_the_adapter_reads_the_real_v9_column_names() -> None:
    """The fixture is the only thing pinning the adapter's column contract, so a
    name invented there is invisible: the suite agrees with the fixture and
    nobody checks the fixture against the vendor (#247).

    These rows carry ONLY real v9 spellings. Each field below came back empty
    before, and the allergy key refused the run outright.
    """
    from anastomosis.sources.pf_tebra.mapper import (
        _map_allergy,
        _map_document,
        _map_facilities,
        _map_immunization,
    )

    # patient-allergy / -reactions are keyed on PatientAllergyGuid, not AllergyGuid.
    allergy_guid = "feedface-0000-0000-0000-00000000a001"
    allergy = _map_allergy(
        {
            "PatientPracticeGuid": P1,
            "PatientAllergyGuid": allergy_guid,
            "AllergenCategory": "Drug",
            "Substance": "Penicillin",
            "IsActive": "True",
        },
        {allergy_guid: [{"PatientAllergyGuid": allergy_guid, "Reaction": "Hives"}]},
    )
    assert allergy.id == allergy_guid, "a dangling allergy key refuses the whole run"
    assert allergy.reactions == ["Hives"]

    # patient-documents has no id of its own; the storage guid is the identity.
    storage = "feedface-0000-0000-0000-00000000d001"
    document = _map_document(
        {"PatientPracticeGuid": P1, "DocumentStorageGuid": storage, "DocumentName": "Referral"},
        P1,
        None,
    )
    assert document.id == storage, "an empty document id is unusable downstream"

    # facilities: Name/City/State/ZipCode/OfficePhone/OfficeFax, not the invented spellings.
    (facility,) = _map_facilities(
        {
            "facilities": [
                {
                    "FacilityGuid": "feedface-0000-0000-0000-00000000f001",
                    "Name": "Example Family Medicine",
                    "Address1": "100 Clinic Way",
                    "City": "Springfield",
                    "State": "WA",
                    "ZipCode": "98101",
                    "OfficePhone": "2065550199",
                    "OfficeFax": "2065550198",
                }
            ]
        }
    )
    assert facility.name == "Example Family Medicine"
    assert (facility.city, facility.state, facility.postal_code) == ("Springfield", "WA", "98101")
    assert facility.phone == "(206) 555-0199"

    # patient-immunizations: the date column was three guessed spellings, all wrong.
    immunization = _map_immunization(
        {
            "PatientPracticeGuid": P1,
            "ImmunizationGuid": "feedface-0000-0000-0000-00000000i001",
            "Vaccine": "Influenza",
            "VaccinationOrEffectiveDate": "10/03/2022",
            "Comments": "Left deltoid",
        }
    )
    assert immunization.administered_on == date(2022, 10, 3), "every dose came back undated"
    assert immunization.comment == "Left deltoid"
