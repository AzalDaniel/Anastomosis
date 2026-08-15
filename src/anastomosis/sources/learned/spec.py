"""The learned-mapping spec: a validated, declarative description of one format.

A learned source is a directory ``~/.anastomosis/sources/<id>/`` whose
``mapping.json`` is parsed and validated by :class:`MappingSpec` here. The spec
is the whole contract between the wizard (which writes it) and the interpreter
(which executes it), so the validation is deliberately strict — it is the
safety boundary that makes "run a mapping a human wrote" trustworthy:

* ``model_config = extra="forbid"`` everywhere — an unknown key is a loud error,
  never silently ignored (a typo'd field name can't quietly disable a check).
* every ``field_mappings[*].target_path`` must be in the CLOSED
  :func:`~anastomosis.core.model_paths.canonical_target_paths` set — a mapping
  can only write to known canonical fields, never an arbitrary attribute;
* every ``transform`` must resolve against the closed verb table
  (:mod:`~anastomosis.sources.learned.transforms`) with correct arity;
* the grouping/source columns must be internally consistent (keys and mapped
  columns must be columns the format actually has);
* ``human_reviewed`` is recorded but NOT trusted by the parser — the discovery
  layer enforces it as a hard gate before ever building an adapter, so an
  unreviewed mapping that lands in the directory is skipped, not executed.

Parsed from JSON, never ``yaml.load`` — the format is data, and JSON has no code
path. ``MappingError`` names the file at fault (paths to mapping config are not
PHI) and is the single error type this package raises for a bad spec.

PHI: a spec carries column names, canonical field names, transform verbs, and an
optional value-translation table (code -> code, e.g. ``M`` -> ``male``). None of
that is patient data. The value-translation table is operator-authored
terminology, kept SEPARATE from the structural ``field_mappings`` by design.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from anastomosis.core.model_paths import canonical_target_paths
from anastomosis.sources.learned.transforms import TransformError, parse_transform

__all__ = [
    "FieldMapping",
    "Grouping",
    "MappingError",
    "MappingSpec",
    "SourceFormat",
    "ValueTranslation",
    "load_spec",
]

MAPPING_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SPEC_FILENAME = "mapping.json"


class MappingError(Exception):
    """A learned mapping is missing, malformed, or invalid — names the file."""


class SourceFormat(BaseModel):
    """How to read the raw export file (and how to recognize it again)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["csv", "tsv", "json", "ndjson"]
    #: Field delimiter for ``csv`` (``tsv`` is always a tab); ``None`` elsewhere.
    delimiter: str | None = None
    encoding: str = "utf-8-sig"
    #: sha256 over the sorted, normalized column-name set — the detect key.
    header_fingerprint: str
    #: The columns observed when the mapping was authored.
    columns: list[str]

    @field_validator("columns")
    @classmethod
    def _columns_non_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("source_format.columns must list at least one column")
        return value


class Grouping(BaseModel):
    """How flat rows fold into patients and encounters.

    * ``row_scope == "patient"`` — one row is one patient (a demographics
      export). ``patient_key`` still de-duplicates rows; ``encounter_key`` is
      unused. A single encounter is built per row only if encounter fields map.
    * ``row_scope == "encounter"`` — one row is one encounter (a visits/notes
      export). ``patient_key`` groups rows into patients; ``encounter_key`` (if
      given) identifies the encounter, else each row is its own encounter.
    """

    model_config = ConfigDict(extra="forbid")

    patient_key: str
    encounter_key: str | None = None
    row_scope: Literal["patient", "encounter"] = "patient"


class FieldMapping(BaseModel):
    """One structural rule: source column -> canonical field, via a transform."""

    model_config = ConfigDict(extra="forbid")

    source_path: str
    target_path: str
    transform: str = "strip"
    #: The matcher's confidence (0..1) when it proposed this; audit only.
    confidence: float = 0.0
    #: Whether a human accepted/edited this mapping in the wizard.
    human_confirmed: bool = False

    @field_validator("target_path")
    @classmethod
    def _known_target(cls, value: str) -> str:
        if value not in canonical_target_paths():
            raise ValueError(f"unknown canonical target_path {value!r}")
        return value

    @field_validator("transform")
    @classmethod
    def _valid_transform(cls, value: str) -> str:
        try:
            parse_transform(value)
        except TransformError as exc:
            raise ValueError(str(exc)) from None
        return value


class ValueTranslation(BaseModel):
    """An operator-authored code->code table for one source column.

    Applied BEFORE the column's transform (e.g. normalize ``M``/``F`` to
    ``male``/``female``). Kept separate from :class:`FieldMapping` so structural
    mapping (which column, parsed how) stays independent of terminology. A value
    not in the table passes through unchanged (lossless).
    """

    model_config = ConfigDict(extra="forbid")

    source_path: str
    table: dict[str, str]


class MappingSpec(BaseModel):
    """A complete, validated learned-source mapping."""

    model_config = ConfigDict(extra="forbid")

    mapping_id: str
    spec_version: Literal[1] = 1
    created_at: datetime
    #: Hard gate enforced by discovery, not here: an unreviewed mapping is never
    #: built into an adapter.
    human_reviewed: bool = False
    display: str
    source_format: SourceFormat
    target_model: Literal["PatientRecord"] = "PatientRecord"
    grouping: Grouping
    field_mappings: list[FieldMapping] = Field(default_factory=list)
    value_translations: list[ValueTranslation] = Field(default_factory=list)
    #: Columns the operator reviewed and chose not to map (still preserved in
    #: ``extensions`` — this list is for transparency/round-trip accounting).
    unmapped_source_fields: list[str] = Field(default_factory=list)

    @field_validator("mapping_id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not MAPPING_ID_RE.match(value):
            raise ValueError(
                f"mapping_id {value!r} must be a lowercase identifier "
                "(letters, digits, underscores; starting with a letter)"
            )
        return value

    @model_validator(mode="after")
    def _internally_consistent(self) -> MappingSpec:
        columns = set(self.source_format.columns)

        def require_column(name: str, role: str) -> None:
            if name not in columns:
                raise ValueError(f"{role} {name!r} is not one of the source columns")

        require_column(self.grouping.patient_key, "grouping.patient_key")
        if self.grouping.encounter_key is not None:
            require_column(self.grouping.encounter_key, "grouping.encounter_key")
            if self.grouping.encounter_key == self.grouping.patient_key:
                raise ValueError(
                    "grouping.encounter_key must differ from patient_key "
                    "(keying encounters on the patient id collapses them into one)"
                )

        seen_targets: set[str] = set()
        for mapping in self.field_mappings:
            require_column(mapping.source_path, "field_mappings.source_path")
            if mapping.target_path in seen_targets:
                raise ValueError(
                    f"two field_mappings target {mapping.target_path!r}; "
                    "each canonical field may be mapped at most once"
                )
            seen_targets.add(mapping.target_path)

        for translation in self.value_translations:
            require_column(translation.source_path, "value_translations.source_path")
        return self


def load_spec(path: Path) -> MappingSpec:
    """Read and validate a ``mapping.json``, raising :class:`MappingError`.

    The error names the file (mapping config paths are not PHI) for every
    failure mode: unreadable file, non-JSON, JSON that is not an object, or a
    spec that fails validation.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MappingError(f"cannot read mapping {path}: {type(exc).__name__}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise MappingError(f"mapping {path} is not valid JSON: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise MappingError(f"mapping {path} must be a JSON object, got {type(data).__name__}")
    try:
        return MappingSpec.model_validate(data)
    except ValidationError as exc:
        # error_count + the first message keep the diagnosis actionable without
        # dumping a value-bearing payload (defensive: specs carry no PHI, but the
        # habit holds everywhere).
        raise MappingError(f"mapping {path} is invalid: {exc.error_count()} error(s)") from exc
