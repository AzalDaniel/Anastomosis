"""Foundation of the canonical model.

Every Anastomosis model carries two things beyond its mapped fields:

* ``extensions`` — a namespaced dict holding **every source field the adapter
  could not map**. Nothing from a source export is ever silently dropped;
  this is the toolkit's lossless-migration guarantee. Keys are namespaced by
  source system, e.g. ``"pf_tebra:PatientContactCode"``.
* ``provenance`` — where this object came from (system, file, original id),
  so any reconstructed document can be traced back to its source rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class Provenance(BaseModel):
    """Where a canonical object came from."""

    model_config = ConfigDict(extra="forbid")

    source_system: str
    source_file: str | None = None
    source_id: str | None = None
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AnastBase(BaseModel):
    """Base class for all canonical models."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str = Field(default_factory=lambda: str(uuid4()))
    extensions: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance | None = None
