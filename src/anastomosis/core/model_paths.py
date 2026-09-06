"""The closed set of canonical target paths a learned mapping may write to
(30) — never an open dotted-path the interpreter ``setattr``\\s blindly.
Scalar paths are derived from :class:`Patient`/:class:`Encounter`
``model_fields`` directly, so a renamed field breaks coverage loudly rather
than silently dropping a target; assembled paths are a small curated set
for the list/nested shapes (address, telecom, identifiers, note sections).
PHI: field *names* only, never values; pure introspection, no I/O.
"""

from __future__ import annotations

import types
from datetime import date, datetime
from typing import Union, get_args, get_origin

from pydantic import BaseModel

from anastomosis.core.model import Encounter, Patient

__all__ = [
    "ASSEMBLED_ENCOUNTER_PATHS",
    "ASSEMBLED_PATIENT_PATHS",
    "ENCOUNTER_SCALAR_PATHS",
    "PATIENT_SCALAR_PATHS",
    "canonical_target_paths",
    "target_scope",
]

# Base-model bookkeeping fields and id cross-references are never mapping
# targets: ``id``/``extensions``/``provenance`` are infrastructure, and a
# ``*_id`` field references another object by id (a flat export has no such
# graph, so a value there would dangle). A provider/facility NAME column in a
# flat export is preserved in ``extensions`` instead.
_NEVER_TARGET = frozenset({"id", "extensions", "provenance"})
_SCALAR_TYPES = (str, bool, int, float, date, datetime)


def _is_scalar(annotation: object) -> bool:
    """True if ``annotation`` is a scalar leaf (optionally ``| None``).

    Peels ``X | None`` unions and rejects any parameterized container
    (``list[...]``, ``dict[...]``) or nested pydantic model.
    """
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        members = [arg for arg in get_args(annotation) if arg is not type(None)]
        return bool(members) and all(_is_scalar(member) for member in members)
    if origin is not None:  # a parameterized container (list/dict/...) — not scalar
        return False
    try:
        return issubclass(annotation, _SCALAR_TYPES)  # type: ignore[arg-type]
    except TypeError:
        return False


def _scalar_paths(model: type[BaseModel], prefix: str) -> frozenset[str]:
    """``{prefix}.{field}`` for every scalar field of ``model`` that may be set."""
    return frozenset(
        f"{prefix}.{name}"
        for name, field in model.model_fields.items()
        if name not in _NEVER_TARGET and not name.endswith("_id") and _is_scalar(field.annotation)
    )


# Scalar leaves, derived from the canonical model so they stay in sync with it.
PATIENT_SCALAR_PATHS = _scalar_paths(Patient, "patient")
ENCOUNTER_SCALAR_PATHS = _scalar_paths(Encounter, "encounter")

# Assembled composites — the interpreter builds the list/nested shape these name.
# Kept as literal strings here (the single source of truth for membership); the
# interpreter maps each to its concrete enum/builder and a coverage test asserts
# the two agree.
ASSEMBLED_PATIENT_PATHS = frozenset(
    {
        "patient.address.line1",
        "patient.address.line2",
        "patient.address.city",
        "patient.address.state",
        "patient.address.postal_code",
        "patient.phone_home",
        "patient.phone_mobile",
        "patient.phone_work",
        "patient.phone_other",
        "patient.email",
        "patient.ssn",
        "patient.mrn",
        "patient.prn",
    }
)
ASSEMBLED_ENCOUNTER_PATHS = frozenset(
    {
        "encounter.subjective",
        "encounter.objective",
        "encounter.assessment",
        "encounter.plan",
        "encounter.narrative",
    }
)


def canonical_target_paths() -> frozenset[str]:
    """Every target path a learned mapping is allowed to write to."""
    return (
        PATIENT_SCALAR_PATHS
        | ENCOUNTER_SCALAR_PATHS
        | ASSEMBLED_PATIENT_PATHS
        | ASSEMBLED_ENCOUNTER_PATHS
    )


def target_scope(path: str) -> str:
    """``"patient"`` or ``"encounter"`` — which object a target path belongs to."""
    return "patient" if path.startswith("patient.") else "encounter"
