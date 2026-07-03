# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""The closed set of canonical target paths a learned mapping may write to.

A learned source mapping (:mod:`anastomosis.sources.learned`) is *data*: it says
"source column X fills canonical field Y". For that to be safe — no embedded
code, no arbitrary attribute writes — "field Y" must be drawn from a CLOSED,
known enumeration rather than an open dotted-path the interpreter ``setattr``\\s
blindly. This module is that enumeration.

Two kinds of target paths exist:

* **scalar paths** — derived by walking :class:`Patient` / :class:`Encounter`
  ``model_fields`` and keeping only the scalar leaves (``str`` / date / number /
  bool), e.g. ``patient.family_name`` or ``encounter.chief_complaint``. Deriving
  them from the model means a field added to the canonical model becomes
  mappable automatically, and a field *renamed* breaks the
  ``test_model_paths`` coverage assertions loudly rather than silently dropping
  a mapping target.
* **assembled paths** — a small curated set the interpreter has explicit
  construction logic for, because the canonical shape is a list or nested model
  rather than a scalar: the single-address parts, the typed phone/email
  telecom slots, the SSN/MRN/PRN identifier slots, and the note-section bodies.

Everything OUTSIDE this set is never a mapping target; an unmapped source column
is preserved in the owning object's ``extensions`` instead (the lossless rule).

PHI: this module handles field *names* only (never patient values) and is pure —
introspection of the pydantic models, no I/O.
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
