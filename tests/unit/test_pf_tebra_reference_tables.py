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
from anastomosis.sources.pf_tebra.loader import UnsupportedTablesError, read_export
from anastomosis.sources.pf_tebra.mapper import _reference_tables, _self_keyed, map_export

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


def test_a_table_pointing_at_patients_this_export_lacks_is_still_refused() -> None:
    """A patient key naming somebody absent is not a directory — it is an orphan."""
    export = read_export(FIXTURE)
    export["stray-rows"] = [{"PatientPracticeGuid": "feedface-dead-0000-0000-00000000beef"}]

    with pytest.raises(UnsupportedTablesError) as caught:
        list(map_export(export))

    assert "stray-rows" in str(caught.value)


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
