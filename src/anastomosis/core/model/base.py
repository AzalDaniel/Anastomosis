"""Foundation of the canonical model.

``extensions`` holds every source field the adapter could not map, namespaced
by source system (the lossless guarantee). ``provenance`` traces an object
back to its source system, file and id.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from anastomosis.core.clock import now as _now


class Provenance(BaseModel):
    """Where a canonical object came from."""

    model_config = ConfigDict(extra="forbid")

    source_system: str
    source_file: str | None = None
    source_id: str | None = None
    ingested_at: datetime = Field(default_factory=_now)


class AnastBase(BaseModel):
    """Base class for all canonical models."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str = Field(default_factory=lambda: str(uuid4()))
    extensions: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance | None = None
