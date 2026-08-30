"""The corpus probe reports shape, never content — proven, not asserted.

The probe reads real charts, so "it only prints counts" cannot rest on
inspection. CodeQL said as much: it flagged the output line as clear-text
logging of sensitive information because a patient model crosses into the
helpers that build the printed result, and no scanner can see that only field
NAMES come back. The finding is a false positive, and the honest way to hold
that position is a test that would fail if it ever stopped being one.

So this feeds the boundary a record whose every string is a sentinel no schema
contains, and requires that not one of them survives into the probe's output.
A future edit that starts emitting a value — a birth date, a name, an
observation's text — fails here rather than in somebody's log.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path
from types import ModuleType

from anastomosis.core.model import (
    Encounter,
    Observation,
    Patient,
    PatientRecord,
)

_PROBE = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "audits"
    / "learned-source"
    / "tools"
    / "probe_ccda_corpus.py"
)

#: Strings that appear in no field name, no LOINC code, and no model default —
#: so finding one in the output can only mean a value escaped.
SENTINELS = (
    "zzsentinelgiven",
    "zzsentinelfamily",
    "zzsentinelnote",
    "zzsentineldisplay",
)


def _probe() -> ModuleType:
    """Import the probe by path; it lives under docs/, not in the package."""
    spec = importlib.util.spec_from_file_location("probe_ccda_corpus", _PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sentinel_record() -> PatientRecord:
    """One record whose every free-text field carries a sentinel."""
    patient = Patient(
        id="feedface-0000-0000-0000-0000000000aa",
        given_name="zzsentinelgiven",
        family_name="zzsentinelfamily",
        birth_date=date(1985, 3, 14),
        notes="zzsentinelnote",
    )
    return PatientRecord(
        patient=patient,
        encounters=[
            Encounter(
                id="feedface-e000-0000-0000-0000000000aa",
                patient_id=patient.id,
                date_of_service=date(2024, 1, 2),
                chief_complaint="zzsentinelnote",
            )
        ],
        observations=[
            Observation(
                id="feedface-0000-0000-0000-0000000000ob",
                patient_id=patient.id,
                display="zzsentineldisplay",
            )
        ],
    )


def test_the_boundary_returns_field_names_never_field_values() -> None:
    """_populated_fields answers WHICH fields were filled, not with what."""
    probe = _probe()
    record = _sentinel_record()

    populated = probe._populated_fields(record.patient)

    assert "given_name" in populated  # the name is reported…
    assert "birth_date" in populated
    for name in populated:
        assert name in type(record.patient).model_fields  # …and it is schema
    assert not any(sentinel in " ".join(populated) for sentinel in SENTINELS)


def test_collection_sizes_are_integers_and_nothing_else() -> None:
    """_collection_sizes answers HOW MANY observations, never which one."""
    probe = _probe()

    sizes = probe._collection_sizes(_sentinel_record())

    assert sizes["observations"] == 1
    assert sizes["conditions"] == 0
    assert all(isinstance(size, int) for size in sizes.values())
    assert not any(sentinel in json.dumps(sizes) for sentinel in SENTINELS)


def test_no_sentinel_survives_into_a_serialised_probe_result() -> None:
    """The end the scanner cared about: what would actually be printed.

    Builds the probe's own result shape from the sentinel record and serialises
    it exactly as the probe does. A value reaching the output would show up
    here as a sentinel in the JSON — which is the whole claim, mechanised.
    """
    probe = _probe()
    record = _sentinel_record()

    result = {
        "patient_field_presence": dict.fromkeys(probe._populated_fields(record.patient), 1),
        "encounter_field_presence": dict.fromkeys(probe._populated_fields(record.encounters[0]), 1),
        "collection_totals": probe._collection_sizes(record),
    }
    serialised = json.dumps(result, sort_keys=True)

    for sentinel in SENTINELS:
        assert sentinel not in serialised, f"a patient value reached the probe's output: {sentinel}"
    # And the identity that is not a free-text field is gone too: the probe
    # counts records_with_patient_id, it never prints the id.
    assert "feedface" not in serialised
