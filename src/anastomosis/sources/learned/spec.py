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
    """A learned mapping is missing, malformed, or invalid — names the file.

    The three optional attributes are a pointer, not prose: a raise site that
    knows which column, target or transform the refusal is about says so here,
    and a frontend can route the operator to the exact control instead of
    scraping an English sentence. Names only, never a value — the same
    discipline the message itself already keeps. Every existing bare
    ``raise MappingError("...")`` stays valid.
    """

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


class DestinationBinding(BaseModel):
    """The destination this mapping was taught FOR, pinned by profile hash.

    A mapping decides which source column becomes which canonical field, and
    that decision is made with a target system in mind — the operator picks the
    destination first (``anast source init --to``), then teaches. Running that
    mapping at a different destination is not a smaller version of the same
    move; it is a different one, made silently. So the choice is recorded here
    and the migration refuses when it disagrees.

    ``profile_hash`` is
    :attr:`anastomosis.core.profiles.DestinationProfile.profile_hash` at the
    moment of teaching, so the refusal fires for a destination that CHANGED
    (a version bump, a capability that appeared or vanished) as well as for one
    that was swapped outright. Names, versions and hex only — no PHI.
    """

    model_config = ConfigDict(extra="forbid")

    destination: str
    version: str
    profile_hash: str


class MappingSpec(BaseModel):
    """A complete, validated learned-source mapping."""

    model_config = ConfigDict(extra="forbid")

    mapping_id: str
    # Additive since 1: `destination_binding` is optional and defaults to None,
    # so every mapping written before it loads unchanged. The other direction
    # is the rough edge — a mapping taught with `--to` on this build, read by an
    # older one, fails validation as an invalid spec rather than as a version
    # it does not know. A bump would have forced dual handling for a field that
    # takes nothing away.
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
    #: The destination chosen BEFORE teaching, when one was. ``None`` for a
    #: mapping taught without a destination in view (the pre-existing flow, and
    #: still the default): unbound, so it runs anywhere and refuses nothing.
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


def mapping_json_text(spec: MappingSpec) -> str:
    """The exact ``mapping.json`` text a saved mapping holds.

    One definition, because two things hash it: ``save_mapping`` writes these
    bytes and records their digest in ``source_trust.json``, and
    :func:`anastomosis.core.profiles.capture_source_profile` recomputes the
    digest when no file is on disk. When those two disagree about indentation
    or the trailing newline, every freshly-taught mapping reads as edited —
    the failure mode ``_atomic_write``'s docstring already records once.
    """
    return json.dumps(spec.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def mapping_content_hash(spec: MappingSpec) -> str:
    """SHA-256 hex over :func:`mapping_json_text` — a mapping's content address."""
    return hashlib.sha256(mapping_json_text(spec).encode("utf-8")).hexdigest()
