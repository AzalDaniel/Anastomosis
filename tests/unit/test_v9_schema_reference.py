"""The fixture must agree with the vendor, not just with the mapper.

`tests/fixtures/pf_tebra_v9/` and the mapper were written to agree with
each other about 25 column names across 12 tables that no real v9 export
has (#247). Nothing here reads a fixture row or builds a record: these
tests compare two lists of names, the fixture's and the vendor's, which
the rest of the suite never does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = _ROOT / "tests" / "fixtures" / "pf_tebra_v9"
# The reference ships INSIDE the adapter now (the loader's vendor-header-defect
# repair reads its column orders at runtime), so the tests read the same copy
# the product does — two copies would drift exactly the way fixture-vs-vendor
# once did.
_REFERENCE = _ROOT / "src" / "anastomosis" / "sources" / "pf_tebra" / "pf_v9_columns.json"


def _vendor() -> dict[str, list[str]]:
    return json.loads(_REFERENCE.read_text(encoding="utf-8"))


def _fixture_tables() -> list[Path]:
    return sorted(_FIXTURE.glob("*.tsv"))


def test_the_reference_is_the_whole_v9_schema() -> None:
    """A partial reference would let an invented name hide in a missing table."""
    vendor = _vendor()
    assert len(vendor) == 85, "v9 has 85 tables"
    assert sum(len(cols) for cols in vendor.values()) == 1164
    assert all(cols and all(isinstance(c, str) and c for c in cols) for cols in vendor.values())


def test_the_reference_carries_names_and_nothing_else() -> None:
    """It is checked into a repository that must never hold PHI: a column
    name is vendor documentation, the same for every practice that ever
    ran the export, and a mapping of table name to a flat list of strings
    has nowhere to put a row."""
    for table, cols in _vendor().items():
        assert isinstance(table, str) and table
        assert isinstance(cols, list)
        assert all(isinstance(c, str) for c in cols)
        # Column names are identifiers. A patient value is not.
        assert all(c.isidentifier() or c.isalnum() for c in cols), table


@pytest.mark.parametrize("path", _fixture_tables(), ids=lambda p: p.stem)
def test_every_fixture_column_is_one_the_vendor_publishes(path: Path) -> None:
    """The check that would have caught #247 the day the fixture was
    written. Subset, not equality: a fixture table carrying five of a
    table's fifty columns is a perfectly good fixture, but a fifty-first
    the vendor never defined is not — the mapper will be written to read
    it, and the export will not have it."""
    vendor = _vendor()
    assert path.stem in vendor, f"{path.stem} is not a table in the v9 export"
    header = path.read_text(encoding="utf-8").splitlines()[0].split("\t")
    invented = [column for column in header if column not in vendor[path.stem]]
    assert invented == [], f"{path.stem} carries columns v9 does not define: {invented}"


@pytest.mark.parametrize("path", _fixture_tables(), ids=lambda p: p.stem)
def test_a_fixture_table_has_no_duplicate_columns(path: Path) -> None:
    """Cheap, and the kind of thing a hand-written TSV gets wrong quietly:
    a repeated header makes one of the two columns unreadable by name."""
    header = path.read_text(encoding="utf-8").splitlines()[0].split("\t")
    assert len(header) == len(set(header)), f"{path.stem} repeats a column name"


def test_the_guard_would_actually_have_failed(tmp_path: Path) -> None:
    """The guard above only counts if it bites, and a subset check can be
    written loosely enough that it never does: run its logic against the
    invented name #247 was really about — `patient-allergy.AllergyGuid`,
    whose absence dangled every reaction row's foreign key and refused
    the whole migration."""
    vendor = _vendor()
    assert "AllergyGuid" not in vendor["patient-allergy"]
    assert "PatientAllergyGuid" in vendor["patient-allergy"]

    header = ["PatientPracticeGuid", "AllergyGuid", "Substance", "Severity"]
    invented = [column for column in header if column not in vendor["patient-allergy"]]
    assert invented == ["AllergyGuid"]


def test_the_one_row_the_vendor_document_did_not_mean() -> None:
    """A published document is evidence, not scripture: the v9 dictionary
    lists an entry named `etc.` on `patient-drug-alert-overrides` with no
    data type — a preceding example list that ran across an HTML line
    break. Dropped by that structural signature, not by name: the only
    entry in all 85 tables shaped that way."""
    vendor = _vendor()
    assert "etc." not in vendor["patient-drug-alert-overrides"]
    assert len(vendor["patient-drug-alert-overrides"]) == 12  # the document says 13
    assert "InteractionReason" in vendor["patient-drug-alert-overrides"]


# Which v9 table each of the mapper's column allowlists is about. The mapper
# keeps these as bare frozensets next to the function that reads them, so the
# table they belong to is knowledge the module does not write down — this dict
# writes it down, and the test below makes it impossible to add a set without
# saying which table it reads.
_ALLOWLIST_TABLES = {
    "_ALLERGY_MAPPED": "patient-allergy",
    "_DEMOGRAPHICS_MAPPED": "patient-demographics",
    "_DIAGNOSIS_MAPPED": "patient-diagnoses",
    "_ENCOUNTER_DX_MAPPED": "patient-encounter-diagnoses",
    "_ENCOUNTER_MAPPED": "patient-encounters",
    "_ETHNICITY_MAPPED": "patient-ethnicity",
    "_GISO_MAPPED": "patient-gender-identity-sexual-orientation",
    "_GOAL_MAPPED": "patient-goals",
    "_GUARANTOR_MAPPED": "patient-guarantor",
    "_HEALTH_CONCERN_MAPPED": "patient-health-concerns",
    "_IMMUNIZATION_MAPPED": "patient-immunizations",
    "_INSURANCE_MAPPED": "patient-insurances",
    "_MEDICATION_MAPPED": "patient-medications",
    "_OBSERVATION_MAPPED": "patient-encounter-observations",
    "_PINNED_MAPPED": "pinned-notes",
    "_PRESCRIPTION_MAPPED": "patient-prescriptions",
    "_RACE_MAPPED": "patient-race",
    "_REACTION_MAPPED": "patient-allergy-reactions",
    "_SCREENING_EVENT_MAPPED": "patient-encounter-events",
    "_SUPERBILL_JOINED_MAPPED": "superbill-insurances",
}


def _allowlists() -> dict[str, frozenset[str]]:
    from anastomosis.sources.pf_tebra import mapper

    return {
        name: value
        for name in dir(mapper)
        if name.endswith("_MAPPED") and isinstance(value := getattr(mapper, name), frozenset)
    }


def test_every_column_the_mapper_reads_is_one_the_vendor_publishes() -> None:
    """Stronger than checking the fixture: a column the mapper reads but
    no fixture table carries is invisible to the header check —
    `patient-medications`'s `LastModifiedDateTimeUtc` (v9 spells it
    `DisplayLastModifiedDateTimeUtc` there) and `patient-prescriptions`'s
    bare `Refills`, a name no v9 table has at all."""
    vendor = _vendor()
    wrong: dict[str, list[str]] = {}
    for name, columns in _allowlists().items():
        table = _ALLOWLIST_TABLES[name]
        invented = sorted(c for c in columns if c not in vendor[table])
        if invented:
            wrong[f"{name} ({table})"] = invented
    assert wrong == {}, f"the mapper reads columns v9 does not define: {wrong}"


def test_a_new_allowlist_has_to_say_which_table_it_reads() -> None:
    """Otherwise the check above quietly stops covering the newest code:
    the mapper's allowlists are module-level frozensets with no table
    attached, so the correspondence lives in this file, and a stale one
    fails open — the worst way for a guard to fail."""
    assert set(_allowlists()) == set(_ALLOWLIST_TABLES)
    vendor = _vendor()
    for name, table in _ALLOWLIST_TABLES.items():
        assert table in vendor, f"{name} claims a table v9 does not have: {table}"
