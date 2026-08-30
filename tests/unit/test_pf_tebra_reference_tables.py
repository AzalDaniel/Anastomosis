"""A practice's own directories are not patient rows, and must not be refused.

The adapter's losslessness rule had two categories: an unmapped table whose
rows carry a patient key is preserved into that patient's extensions, and one
whose rows cannot be attributed to anybody is refused, because failing closed
beats dropping clinical data.

Both are right. The taxonomy was missing a third case. `labs`, `pharmacies`,
`provider-profiles` and `users` are part of the standard Practice Fusion
export and have no patient column because they are not patient rows — they are
the practice's directories, keyed by their own id and referenced BY patient
rows: a prescription names a PharmacyGuid, a lab result names a LabGuid.
Demanding a patient key of a directory is a category error, and the result was
that the adapter refused every real export it was written for:

    UnsupportedTablesError: export contains unmapped tables that cannot be
    attributed to a patient (no PatientPracticeGuid column): ['labs',
    'pharmacies', 'provider-profiles', 'users']

The fixture now carries all four, so the lane exercises the format the adapter
actually meets rather than a subset that happened to avoid the bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
from anastomosis.sources import get_source
from anastomosis.sources.base import QuarantinedRows
from anastomosis.sources.pf_tebra.loader import UnsupportedTablesError, read_export
from anastomosis.sources.pf_tebra.mapper import (
    _patient_scoped_guids,
    _reference_tables,
    _self_keyed,
    map_export,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"
DIRECTORIES = ("labs", "pharmacies", "provider-profiles", "users")


def test_the_standard_export_tables_no_longer_refuse_the_run() -> None:
    """The four that stopped the adapter opening a real export."""
    export = read_export(FIXTURE)
    for table in DIRECTORIES:
        assert export[table], f"the fixture no longer carries {table}"

    records = list(get_source("pf-tebra").load(FIXTURE))

    assert records, "a standard export still refuses to load"


def test_each_record_carries_the_directories_its_rows_reference() -> None:
    """A record has to stand alone — the bundle is one directory per patient.

    Same choice `providers` and `facilities` have always made: the practice's
    tables are attached whole to every record, so a prescription naming a
    pharmacy travels with the pharmacy it names.
    """
    records = list(get_source("pf-tebra").load(FIXTURE))

    for record in records:
        carried = {key.split(":practice:")[1] for key in record.extensions if ":practice:" in key}
        assert carried == set(DIRECTORIES), record.patient.id


def test_a_table_with_no_patient_key_and_no_identity_is_still_refused() -> None:
    """The refusal is narrowed, not removed. Failing closed still beats dropping."""
    export = read_export(FIXTURE)
    # Rows that name neither a patient nor themselves: nowhere to put them.
    export["mystery-notes"] = [{"Note": "a"}, {"Note": "b"}]

    with pytest.raises(UnsupportedTablesError) as caught:
        list(map_export(export))

    assert "mystery-notes" in str(caught.value)


def test_a_table_pointing_at_patients_this_export_lacks_is_quarantined_not_refused() -> None:
    """A patient key naming somebody absent is not a directory — it is an orphan.

    Since #280 an orphan no longer takes the run down with it: the row is held
    in quarantine (verbatim, with the reason) and the migration proceeds. It
    still lands on NO patient — held is not guessed.
    """
    export = read_export(FIXTURE)
    stray = {"PatientPracticeGuid": "feedface-dead-0000-0000-00000000beef"}
    export["stray-rows"] = [stray]

    held: list[QuarantinedRows] = []
    records = list(map_export(export, on_quarantine=held.extend))

    (entry,) = held
    assert entry.table == "stray-rows"
    assert entry.rows == (stray,)
    assert "names no patient" in entry.reason
    for record in records:
        assert "pf_tebra:unmapped:stray-rows" not in record.extensions


def test_a_directory_is_recognised_by_its_shape_not_by_its_name() -> None:
    """A real export has 85 tables; the four that broke this were one export's four.

    So the rule reads the data: a `*Guid` column that is present, non-empty and
    DISTINCT on every row. Uniqueness is what separates a directory from a link
    table that merely mentions an id many times.
    """
    assert _self_keyed([{"WidgetGuid": "a"}, {"WidgetGuid": "b"}]) == "WidgetGuid"
    assert _self_keyed([{"WidgetGuid": "a"}, {"WidgetGuid": "a"}]) is None, "repeated: a link table"
    assert _self_keyed([{"WidgetGuid": "a"}, {"WidgetGuid": ""}]) is None, "a blank key is no key"
    assert _self_keyed([{"Name": "a"}]) is None, "no identity of its own"


def test_a_table_with_a_patient_key_is_never_read_as_a_directory() -> None:
    """Patient-scoped preservation keeps precedence — that data belongs to someone."""
    export = read_export(FIXTURE)
    patient = export["patient-demographics"][0]["PatientPracticeGuid"]
    export["patient-widgets"] = [
        {"PatientPracticeGuid": patient, "WidgetGuid": "feedface-0000-0000-0000-00000000w001"}
    ]

    reference = _reference_tables(export)
    (record,) = [r for r in map_export(export) if r.patient.id == patient]

    assert "patient-widgets" not in reference
    assert "pf_tebra:unmapped:patient-widgets" in record.extensions


def test_a_directory_that_names_a_patient_record_joins_to_its_one_owner() -> None:
    """The #234 shape: no patient column, a unique guid of its own, and a foreign
    key into patient scope. Classified as a directory it was copied whole into
    EVERY record — 773 rows x 2,167 patients on the real export. Refusing was
    the first fix; #280 finishes it: the join is DECLARED (_INDIRECT_JOINS),
    and a row resolving to exactly one known patient lands on that patient —
    never broadcast, never guessed. The join's failure modes are pinned in
    test_pf_quarantine.py.
    """
    export = read_export(FIXTURE)
    plan_row = export["patient-insurances"][0]
    plan = plan_row["PatientInsurancePlanGuid"]
    owner = plan_row["PatientPracticeGuid"]
    eligibility = {
        "PatientInsuranceEligibilityGuid": "feedface-0000-0000-0000-00000000e001",
        "PatientInsurancePlanGuid": plan,
        "CopayAmount": "25.00",
    }
    export["patient-insurance-eligibilities"] = [eligibility]

    # It is self-keyed, so the old rule called it a directory.
    assert _self_keyed(export["patient-insurance-eligibilities"]) is not None
    assert "patient-insurance-eligibilities" not in _reference_tables(export)

    records = list(map_export(export))
    (owned,) = [r for r in records if r.patient.id == owner]
    assert owned.extensions["pf_tebra:unmapped:patient-insurance-eligibilities"] == [eligibility]
    for record in records:
        if record.patient.id != owner:
            assert "pf_tebra:unmapped:patient-insurance-eligibilities" not in record.extensions


def test_the_five_real_practice_directories_are_still_carried() -> None:
    """The invariant must not cost the tables it was built to admit. These are the
    real export's genuine directories — each keyed by itself, none naming a patient.
    """
    export = read_export(FIXTURE)
    export["care-team-profiles"] = [
        {"CareTeamProfileGuid": "feedface-0000-0000-0000-00000000c001", "Name": "Care team"}
    ]
    export["users"] = [{"UserGuid": "feedface-0000-0000-0000-00000000u001", "Name": "A user"}]

    reference = _reference_tables(export)
    for table in ("labs", "pharmacies", "provider-profiles", "care-team-profiles", "users"):
        assert table in reference, f"{table} is a directory and must still be carried"


def test_a_directory_referenced_BY_patient_rows_is_still_a_directory() -> None:
    """Being named by patient rows is what a directory is FOR — it must not be
    mistaken for being patient data.

    On the real export ``PharmacyGuid``, ``LabGuid`` and ``CareTeamProfileGuid``
    all appear in patient-keyed tables, because prescriptions name pharmacies and
    results name labs. A rule that looked at a table's own key as though it were
    a foreign key would refuse three of the five genuine directories and stop the
    migration cold.
    """
    export = read_export(FIXTURE)
    patient = export["patient-demographics"][0]["PatientPracticeGuid"]
    pharmacy = export["pharmacies"][0]["PharmacyGuid"]
    # A patient-keyed table that NAMES the directory, exactly as prescriptions do.
    export["patient-pharmacy-picks"] = [{"PatientPracticeGuid": patient, "PharmacyGuid": pharmacy}]

    assert "PharmacyGuid" in _patient_scoped_guids(export)
    assert "pharmacies" in _reference_tables(export), "a referenced directory is still a directory"


def test_a_patient_foreign_key_is_caught_even_when_it_is_not_named_Patient() -> None:
    """The column name is a convenience, not the rule. What makes a column
    patient-scoped is that it keys a row somewhere that carries a patient.
    """
    export = read_export(FIXTURE)
    patient = export["patient-demographics"][0]["PatientPracticeGuid"]
    export["patient-widget-links"] = [
        {"PatientPracticeGuid": patient, "WidgetGuid": "feedface-0000-0000-0000-00000000w001"}
    ]
    # Directory-shaped, and its foreign key carries no "Patient" in the name.
    export["widget-eligibilities"] = [
        {
            "WidgetEligibilityGuid": "feedface-0000-0000-0000-00000000x001",
            "WidgetGuid": "feedface-0000-0000-0000-00000000w001",
            "Amount": "25.00",
        }
    ]

    assert _self_keyed(export["widget-eligibilities"]) is not None
    assert "widget-eligibilities" not in _reference_tables(export)
    with pytest.raises(UnsupportedTablesError) as caught:
        list(map_export(export))
    assert "widget-eligibilities" in str(caught.value)


def test_a_patient_named_key_is_caught_even_when_no_patient_table_carries_it() -> None:
    """The two halves of the rule cover different misses.

    The cross-table check needs the foreign key to appear in some patient-keyed
    table. A second-order table can name a patient's record through a column that
    appears nowhere else — the name is the only signal left, so it is kept.
    """
    export = read_export(FIXTURE)
    export["consent-eligibilities"] = [
        {
            "ConsentEligibilityGuid": "feedface-0000-0000-0000-00000000y001",
            "PatientConsentGuid": "feedface-0000-0000-0000-00000000y002",
        }
    ]

    assert "PatientConsentGuid" not in _patient_scoped_guids(export), "no table carries it"
    assert "consent-eligibilities" not in _reference_tables(export)
    with pytest.raises(UnsupportedTablesError) as caught:
        list(map_export(export))
    assert "consent-eligibilities" in str(caught.value)
