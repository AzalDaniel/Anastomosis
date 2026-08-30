"""The vendor's header lies about ``patient-contacts``; the rows do not.

Two independent real v9 exports ship this table with another schema's
five-column header — while every data row has the 15 cells the vendor's own
dictionary documents. The loader read that as an unquoted-tab corruption and
stopped the entire migration on line 2 (#279), telling the operator to "fix
the export" — by hand, against PHI they must not edit.

The repair is a registered defect, not a tolerance: table name, exact
anomalous header, and uniform reference-width rows must all agree, or the
refusal stands exactly as before. Every test here is a synthetic fixture with
``feedface-`` guids and invented names.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anastomosis.sources.pf_tebra.loader import (
    MalformedExportError,
    read_table,
    v9_reference_columns,
)

#: The header two real exports actually ship — another table's schema, down to
#: the two modification columns in the wrong order.
ANOMALOUS_HEADER = (
    "PatientPracticeGuid\tNoteType\tNoteText\tLastModifiedDateTimeUtc\tLastModifiedByUserGuid"
)

REFERENCE = v9_reference_columns()["patient-contacts"]


def _contact_row(guid: str = "feedface-0000-0000-0000-0000000000aa") -> str:
    """One synthetic 15-cell contacts row, in the reference column order."""
    cells = {
        "PatientPracticeGuid": guid,
        "FirstName": "Kinfolk",
        "LastName": "Probe",
        "RelationToPatient": "Sibling",
        "PhoneNumber": "(206) 555-0142",
        "City": "Springfield",
        "State": "WA",
    }
    return "\t".join(cells.get(col, "") for col in REFERENCE)


def _write(tmp_path: Path, name: str, *lines: str) -> Path:
    (tmp_path / f"{name}.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


def test_rows_under_the_anomalous_header_load_under_the_reference_schema(
    tmp_path: Path,
) -> None:
    """The #279 case: the unchanged export advances instead of dying on line 2."""
    root = _write(tmp_path, "patient-contacts", ANOMALOUS_HEADER, _contact_row(), _contact_row())
    rows = read_table(root, "patient-contacts")
    assert len(rows) == 2
    # Keyed by the REFERENCE columns — the repair replaced the header wholesale.
    assert rows[0]["FirstName"] == "Kinfolk"
    assert rows[0]["RelationToPatient"] == "Sibling"
    assert set(rows[0]) == set(REFERENCE)


def test_an_empty_table_under_the_anomalous_header_loads_empty(tmp_path: Path) -> None:
    """The second real export's exact shape: the defective header, no rows."""
    root = _write(tmp_path, "patient-contacts", ANOMALOUS_HEADER)
    assert read_table(root, "patient-contacts") == []


def test_one_row_of_the_wrong_width_refuses_the_whole_table(tmp_path: Path) -> None:
    """The repair never widens into a general tolerance: a 14-cell row among
    15-cell rows is corruption, and uniformity was the license to repair."""
    short = _contact_row().rsplit("\t", 1)[0]  # 14 cells
    root = _write(tmp_path, "patient-contacts", ANOMALOUS_HEADER, _contact_row(), short)
    with pytest.raises(MalformedExportError) as excinfo:
        read_table(root, "patient-contacts")
    assert "patient-contacts" in str(excinfo.value)


def test_a_row_wider_than_the_reference_refuses(tmp_path: Path) -> None:
    wide = _contact_row() + "\tsurplus"
    root = _write(tmp_path, "patient-contacts", ANOMALOUS_HEADER, wide)
    with pytest.raises(MalformedExportError):
        read_table(root, "patient-contacts")


def test_the_same_header_on_a_different_table_is_still_corruption(tmp_path: Path) -> None:
    """The defect is registered per TABLE. Fifteen-wide rows under this header
    in some other file match no registered repair and refuse as before."""
    root = _write(tmp_path, "patient-goals", ANOMALOUS_HEADER, _contact_row())
    with pytest.raises(MalformedExportError):
        read_table(root, "patient-goals")


def test_a_nearly_anomalous_header_does_not_qualify(tmp_path: Path) -> None:
    """One column renamed and the exact-match license is gone — a header that
    merely RESEMBLES the defect is an unknown export shape, not a known one."""
    nearly = ANOMALOUS_HEADER.replace("NoteType", "NoteKind")
    root = _write(tmp_path, "patient-contacts", nearly, _contact_row())
    with pytest.raises(MalformedExportError):
        read_table(root, "patient-contacts")


def test_arbitrary_overflow_elsewhere_still_refuses(tmp_path: Path) -> None:
    """The original defense is untouched: a data-bearing surplus cell in an
    ordinary table is an unquoted tab shifting named columns, and stops."""
    root = _write(
        tmp_path,
        "patient-goals",
        "PatientPracticeGuid\tDescription",
        "feedface-0000-0000-0000-0000000000aa\ta goal\tleaked-extra-cell",
    )
    with pytest.raises(MalformedExportError):
        read_table(root, "patient-goals")


def test_the_registered_header_and_reference_disagreeing_would_be_loud() -> None:
    """The repair's premise, pinned: the anomalous header is NOT the reference
    schema (else there would be nothing to repair), and the reference really
    carries 15 named columns for this table."""
    assert len(REFERENCE) == 15
    assert tuple(ANOMALOUS_HEADER.split("\t")) != REFERENCE[:5]
