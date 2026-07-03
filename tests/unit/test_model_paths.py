# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""The closed canonical target-path enumeration (W2 learn-a-source foundation).

These pin the contract the learned-mapping spec validates against: scalar leaves
are derived from the model (so a field rename is caught here, not silently
dropped), and infrastructure / id-reference fields are never mapping targets.
"""

from __future__ import annotations

from anastomosis.core.model_paths import (
    ASSEMBLED_ENCOUNTER_PATHS,
    ASSEMBLED_PATIENT_PATHS,
    ENCOUNTER_SCALAR_PATHS,
    PATIENT_SCALAR_PATHS,
    canonical_target_paths,
    target_scope,
)


def test_patient_scalars_include_demographics_and_exclude_lists_and_infra() -> None:
    assert "patient.family_name" in PATIENT_SCALAR_PATHS
    assert "patient.given_name" in PATIENT_SCALAR_PATHS
    assert "patient.birth_date" in PATIENT_SCALAR_PATHS
    assert "patient.sex" in PATIENT_SCALAR_PATHS
    # List fields, nested models, and base bookkeeping are NOT scalar targets.
    assert "patient.race" not in PATIENT_SCALAR_PATHS  # list[str]
    assert "patient.identifiers" not in PATIENT_SCALAR_PATHS  # list
    assert "patient.guarantor" not in PATIENT_SCALAR_PATHS  # nested model
    assert "patient.id" not in PATIENT_SCALAR_PATHS
    assert "patient.extensions" not in PATIENT_SCALAR_PATHS
    assert "patient.provenance" not in PATIENT_SCALAR_PATHS


def test_encounter_scalars_exclude_id_references() -> None:
    assert "encounter.chief_complaint" in ENCOUNTER_SCALAR_PATHS
    assert "encounter.date_of_service" in ENCOUNTER_SCALAR_PATHS
    # *_id fields reference another object by id — never a flat-file target.
    assert "encounter.patient_id" not in ENCOUNTER_SCALAR_PATHS
    assert "encounter.provider_id" not in ENCOUNTER_SCALAR_PATHS
    assert "encounter.sections" not in ENCOUNTER_SCALAR_PATHS  # list


def test_assembled_paths_are_disjoint_from_scalars() -> None:
    assert ASSEMBLED_PATIENT_PATHS.isdisjoint(PATIENT_SCALAR_PATHS)
    assert ASSEMBLED_ENCOUNTER_PATHS.isdisjoint(ENCOUNTER_SCALAR_PATHS)
    assert "patient.address.line1" in ASSEMBLED_PATIENT_PATHS
    assert "patient.email" in ASSEMBLED_PATIENT_PATHS
    assert "encounter.subjective" in ASSEMBLED_ENCOUNTER_PATHS


def test_canonical_target_paths_is_the_union() -> None:
    paths = canonical_target_paths()
    assert paths == (
        PATIENT_SCALAR_PATHS
        | ENCOUNTER_SCALAR_PATHS
        | ASSEMBLED_PATIENT_PATHS
        | ASSEMBLED_ENCOUNTER_PATHS
    )
    assert all(p.startswith(("patient.", "encounter.")) for p in paths)


def test_target_scope() -> None:
    assert target_scope("patient.family_name") == "patient"
    assert target_scope("patient.address.city") == "patient"
    assert target_scope("encounter.subjective") == "encounter"
