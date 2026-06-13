"""Shared PDF-attribution helper for the archive and bundle deliverers.

The reconstruction engine names each chart ``{family}_{given}_{dos}_{type}.pdf``
and a patient id never appears in the filename, so the only way to attribute a
rendered PDF to a patient is by the leading ``{family}_{given}_`` prefix. Both
deliverers compute that prefix the same way, so it lives once here.
"""

from __future__ import annotations

import re

from anastomosis.core.model import Patient

__all__ = ["patient_prefix"]


def patient_prefix(patient: Patient) -> str:
    """The engine's filename prefix for a patient (``{family}_{given}_``).

    Returns ``""`` when either name is missing — the patient cannot be
    attributed to any chart by name, so the deliverers copy nothing for them.
    The trailing underscore makes patient prefixes mutually non-prefixing
    (``Smith_John_`` never starts with ``Smith_Jo_``), so a chart matches at
    most one patient.
    """
    family = re.sub(r"[^A-Za-z0-9_-]+", "_", (patient.family_name or "").strip()).strip("_")
    given = re.sub(r"[^A-Za-z0-9_-]+", "_", (patient.given_name or "").strip()).strip("_")
    if not (family and given):
        return ""
    return f"{family}_{given}_"
