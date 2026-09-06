"""The learned-mapping spec: a validated, declarative description of one format.

``mapping.json`` is parsed and validated here — the whole contract between
the wizard (which writes it) and the interpreter (which executes it),
validated strictly: ``extra="forbid"`` everywhere; every ``target_path`` in
the CLOSED :func:`~anastomosis.core.model_paths.canonical_target_paths`
set; every ``transform`` resolves against the closed verb table;
``human_reviewed`` is recorded but enforced only by the discovery layer.

PHI: a spec carries column/field names, transform verbs, and an
operator-authored code table — never patient data."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from anastomosis.core.model_paths import canonical_target_paths
from anastomosis.sources.learned.transforms import TransformError, parse_transform

__all__ = [
    "DestinationBinding",
    "FieldMapping",
    "Grouping",
    "MappingError",
    "MappingSpec",
    "SourceFormat",
    "ValueTranslation",
    "load_spec",
    "mapping_content_hash",
    "mapping_json_text",
]

MAPPING_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SPEC_FILENAME = "mapping.json"


class MappingError(Exception):
    """A learned mapping is missing, malformed, or invalid — names the
    file. The three optional attributes are a pointer, not prose: a raise
    site names the column/target/transform at fault so a frontend can
    route the operator to the exact control, never a value."""

    def __init__(
        self,
        message: str,
        *,
        column: str | None = None,
        target: str | None = None,
        transform: str | None = None,
        scope: str | None = None,
    ) -> None:
        super().__init__(message)
        self.column = column
        self.target = target
        self.transform = transform
        #: Which kind of control the refusal points at: ``"grouping"`` when the
        #: fault is the patient key, encounter key or row grain rather than any
        #: one column's read. ``None`` means the column/target pointer is the
        #: whole story.
        self.scope = scope


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
    """How flat rows fold into patients and encounters. ``row_scope`` is
    "patient" (one row is one patient; ``encounter_key`` unused) or
    "encounter" (``patient_key`` groups rows; ``encounter_key``, if given,
    identifies the encounter, else each row is its own)."""

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
    """An operator-authored code->code table for one source column,
    applied BEFORE the column's transform (e.g. ``M``/``F`` → ``male``/
    ``female``). Kept separate from :class:`FieldMapping` so structural
    mapping stays independent of terminology. An unmapped value passes
    through unchanged (lossless)."""

    model_config = ConfigDict(extra="forbid")

    source_path: str
    table: dict[str, str]


class DestinationBinding(BaseModel):
    """The destination this mapping was taught FOR, pinned by
    ``profile_hash`` (:attr:`anastomosis.core.profiles.DestinationProfile.
    profile_hash` at teaching time) — running it at a different
    destination, or the same one changed, is a different move made
    silently, so the migration refuses when it disagrees."""

    model_config = ConfigDict(extra="forbid")

    destination: str
    version: str
    profile_hash: str


class MappingSpec(BaseModel):
    """A complete, validated learned-source mapping."""

    model_config = ConfigDict(extra="forbid")

    mapping_id: str
    # Additive since 1: `destination_binding` defaults to None, so every
    # mapping written before it loads unchanged; one taught on a newer build
    # fails validation on an older one, rather than being read wrong.
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
    #: The destination chosen BEFORE teaching, if any. ``None`` (the default)
    #: means unbound: the mapping runs anywhere and refuses nothing.
    destination_binding: DestinationBinding | None = None

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
                first = next(
                    m.source_path
                    for m in self.field_mappings
                    if m.target_path == mapping.target_path
                )
                raise ValueError(
                    f"columns {first!r} and {mapping.source_path!r} both target "
                    f"{mapping.target_path!r}; each canonical field may be mapped at most once"
                )
            seen_targets.add(mapping.target_path)

        for translation in self.value_translations:
            require_column(translation.source_path, "value_translations.source_path")
        return self


def load_spec(path: Path) -> MappingSpec:
    """Read and validate a ``mapping.json``, raising :class:`MappingError`
    naming the file (mapping config paths are not PHI) for every failure
    mode: unreadable, non-JSON, not an object, or failed validation."""
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
        # error_count keeps the diagnosis actionable without dumping a
        # value-bearing payload.
        raise MappingError(f"mapping {path} is invalid: {exc.error_count()} error(s)") from exc


def mapping_json_text(spec: MappingSpec) -> str:
    """The exact ``mapping.json`` text a saved mapping holds — one
    definition, because ``save_mapping`` writes these bytes and
    :func:`anastomosis.core.profiles.capture_source_profile` recomputes
    the same digest with no file on disk; disagreeing on indentation or
    the trailing newline would read every fresh mapping as edited."""
    return json.dumps(spec.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def mapping_content_hash(spec: MappingSpec) -> str:
    """SHA-256 hex over :func:`mapping_json_text` — a mapping's content address."""
    return hashlib.sha256(mapping_json_text(spec).encode("utf-8")).hexdigest()
