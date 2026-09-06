"""The one generic adapter that executes any learned mapping.

Exactly ONE interpreter, not one-per-format: a :class:`LearnedSourceAdapter`
runs a validated :class:`~.spec.MappingSpec` — a new format is new *data*,
never new code. Rows group by ``patient_key`` (mirroring ``pf_tebra._by``);
``row_scope`` says whether each row is its own encounter, and runtime
checks refuse blank/duplicate keys or conflicting values. Lossless like
the built-ins: an unconsumed column lands in ``learned:<id>:<column>``
extensions; a mapping writes only to the closed canonical target set."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from anastomosis.core.model import (
    Address,
    ContactKind,
    ContactPoint,
    Encounter,
    Identifier,
    IdentifierKind,
    NoteSection,
    Patient,
    PatientRecord,
    Provenance,
    SectionKind,
)
from anastomosis.core.model_paths import (
    ASSEMBLED_ENCOUNTER_PATHS,
    ASSEMBLED_PATIENT_PATHS,
    target_scope,
)
from anastomosis.core.textutil import clean_cell, html_to_text, sanitize_soap_html
from anastomosis.sources.learned.reader import Row, find_source_file, read_rows
from anastomosis.sources.learned.spec import MappingError, MappingSpec
from anastomosis.sources.learned.transforms import is_lossy, parse_transform

__all__ = ["LearnedSourceAdapter"]

# Assembled-path routing. These must cover the model_paths assembled sets
# exactly; ``_assert_assembled_coverage`` proves it at import (a path added to
# the model_paths set without a builder here is a loud bug, not a silent drop).
_ADDRESS_PARTS = {
    "patient.address.line1": "line1",
    "patient.address.line2": "line2",
    "patient.address.city": "city",
    "patient.address.state": "state",
    "patient.address.postal_code": "postal_code",
}
_PHONE_KINDS = {
    "patient.phone_home": ContactKind.PHONE_HOME,
    "patient.phone_mobile": ContactKind.PHONE_MOBILE,
    "patient.phone_work": ContactKind.PHONE_WORK,
    "patient.phone_other": ContactKind.PHONE_OTHER,
}
_IDENTIFIER_KINDS = {
    "patient.ssn": IdentifierKind.SSN,
    "patient.mrn": IdentifierKind.MRN,
    "patient.prn": IdentifierKind.PRN,
}
_EMAIL_PATH = "patient.email"
_SECTION_KINDS = {
    "encounter.subjective": SectionKind.SUBJECTIVE,
    "encounter.objective": SectionKind.OBJECTIVE,
    "encounter.assessment": SectionKind.ASSESSMENT,
    "encounter.plan": SectionKind.PLAN,
    "encounter.narrative": SectionKind.NARRATIVE,
}
_SECTION_TITLES = {
    SectionKind.SUBJECTIVE: "Subjective",
    SectionKind.OBJECTIVE: "Objective",
    SectionKind.ASSESSMENT: "Assessment",
    SectionKind.PLAN: "Plan",
    SectionKind.NARRATIVE: None,
}


def _assert_assembled_coverage() -> None:
    patient = set(_ADDRESS_PARTS) | set(_PHONE_KINDS) | set(_IDENTIFIER_KINDS) | {_EMAIL_PATH}
    if patient != set(ASSEMBLED_PATIENT_PATHS):
        raise RuntimeError("interpreter assembled-patient routing is out of sync with model_paths")
    if set(_SECTION_KINDS) != set(ASSEMBLED_ENCOUNTER_PATHS):
        raise RuntimeError("interpreter section routing is out of sync with model_paths")


_assert_assembled_coverage()

_M = TypeVar("_M", bound=BaseModel)


class _FieldPlan:
    """A pre-resolved field mapping: source column, target, scope, bound transform."""

    __slots__ = ("scope", "source_path", "target_path", "transform", "transform_spec")

    def __init__(
        self,
        source_path: str,
        target_path: str,
        transform: Callable[[str | None], Any],
        transform_spec: str,
    ) -> None:
        self.source_path = source_path
        self.target_path = target_path
        self.scope = target_scope(target_path)
        self.transform = transform
        # The spec STRING the callable was bound from, kept so a refusal can
        # name what it was trying to do — the callable alone cannot say.
        self.transform_spec = transform_spec


class LearnedSourceAdapter:
    """A :class:`~anastomosis.sources.base.SourceAdapter` built from a mapping."""

    def __init__(self, spec: MappingSpec) -> None:
        self.name = spec.mapping_id
        # Free text from `anast source init --display`, not derived from
        # `name` — a re-cased mapping id is not a real title.
        self.display = spec.display
        self.description = f"learned: {spec.display}"
        self._spec = spec
        self._patient_key = spec.grouping.patient_key
        self._encounter_key = spec.grouping.encounter_key
        # Pre-resolve every transform once (the spec already validated them).
        self._plan: list[_FieldPlan] = [
            _FieldPlan(m.source_path, m.target_path, parse_transform(m.transform), m.transform)
            for m in spec.field_mappings
        ]
        self._patient_plan = [p for p in self._plan if p.scope == "patient"]
        self._encounter_plan = [p for p in self._plan if p.scope == "encounter"]
        self._translations = {t.source_path: t.table for t in spec.value_translations}
        # A column read through a LOSSY verb is consumed only as a mapping,
        # never as evidence: its raw value still rides extensions (`const`
        # overwrites, `split` keeps one piece, case folds away). round_trip()
        # holds these to the same verbatim-survival proof as unmapped columns.
        self._consumed = {
            m.source_path for m in spec.field_mappings if not is_lossy(m.transform)
        } | {self._patient_key}
        if self._encounter_key is not None:
            self._consumed.add(self._encounter_key)

    @property
    def spec(self) -> MappingSpec:
        """The mapping this adapter executes — read-only, for profiling it.
        :func:`anastomosis.core.profiles.capture_source_profile` needs the
        mapping's id, version and destination binding to address the
        adapter by content; the spec is frozen by convention, so handing
        it out is a read, not a seam."""
        return self._spec

    # --- SourceAdapter protocol ------------------------------------------------

    def detect(self, path: Path) -> bool:
        """True iff ``path`` holds a file matching this mapping's fingerprint."""
        try:
            find_source_file(path, self._spec.source_format)
        except MappingError:
            return False
        return True

    def load(self, path: Path) -> Iterator[PatientRecord]:
        """Read the matching file into one :class:`PatientRecord` per patient."""
        source_file = find_source_file(path, self._spec.source_format)
        rows = read_rows(source_file, self._spec.source_format)
        file_name = source_file.name
        for group_key, group_rows in self._group_by_patient(rows):
            yield self._assemble(group_key, group_rows, file_name)

    # --- assembly --------------------------------------------------------------

    def _group_by_patient(self, rows: list[Row]) -> list[tuple[str, list[Row]]]:
        """Group rows by patient key, preserving first-seen order (deterministic).

        A declared patient key is an identity boundary: a blank/missing key or
        duplicate patient-grained row is rejected before any record is built.
        """
        order: list[str] = []
        groups: dict[str, list[Row]] = {}
        for row_number, row in enumerate(rows, start=1):
            key = clean_cell(row.get(self._patient_key))
            if key is None:
                raise MappingError(
                    f"learned mapping {self.name!r}: blank or missing patient key in "
                    f"column {self._patient_key!r} (row {row_number})",
                    column=self._patient_key,
                    scope="grouping",
                )
            if key not in groups:
                groups[key] = []
                order.append(key)
            elif self._spec.grouping.row_scope == "patient":
                raise MappingError(
                    f"learned mapping {self.name!r}: row_scope='patient' found 2 rows "
                    f"with the same patient key in column {self._patient_key!r}",
                    column=self._patient_key,
                    scope="grouping",
                )
            groups[key].append(row)
        return [(key, groups[key]) for key in order]

    def _value(self, row: Row, plan: _FieldPlan) -> object:
        raw = row.get(plan.source_path)
        table = self._translations.get(plan.source_path)
        if table is not None:
            cleaned = clean_cell(raw)
            if cleaned is not None and cleaned in table:
                raw = table[cleaned]
        try:
            return plan.transform(raw)
        except (ValueError, TypeError):
            # A transform that chokes on a cell must not crash with a traceback
            # that prints the value; re-raise value-free (`from None` drops it).
            raise MappingError(
                f"learned mapping {self.name!r}: a value in column {plan.source_path!r} "
                f"could not be read as {plan.transform_spec!r} for {plan.target_path!r}",
                column=plan.source_path,
                target=plan.target_path,
                transform=plan.transform_spec,
            ) from None

    def _row_extensions(self, rows: list[Row]) -> dict[str, object]:
        """Every non-consumed, non-empty column across ``rows`` (last value wins)."""
        extensions: dict[str, object] = {}
        for row in rows:
            for column, value in row.items():
                if column not in self._consumed and clean_cell(value) is not None:
                    extensions[f"learned:{self.name}:{column}"] = value
        return extensions

    def _patient_parts(
        self, base_row: Row, resolved_values: dict[str, object] | None
    ) -> tuple[dict[str, object], dict[str, str], list[ContactPoint], list[Identifier]]:
        """Route each mapped patient value to the model shape that holds
        it: an address (one object), phones/email (typed contact points),
        SSN/MRN/PRN (typed identifiers), or a plain field. With
        ``resolved_values``, reads the first-nonblank value agreed across
        an encounter-grained group instead of the one row."""
        scalars: dict[str, object] = {}
        address: dict[str, str] = {}
        telecom: list[ContactPoint] = []
        identifiers: list[Identifier] = []
        for plan in self._patient_plan:
            value = (
                self._value(base_row, plan)
                if resolved_values is None
                else resolved_values.get(plan.target_path)
            )
            if value is None:
                continue
            if plan.target_path in _ADDRESS_PARTS:
                address[_ADDRESS_PARTS[plan.target_path]] = str(value)
            elif plan.target_path in _PHONE_KINDS:
                telecom.append(ContactPoint(kind=_PHONE_KINDS[plan.target_path], value=str(value)))
            elif plan.target_path == _EMAIL_PATH:
                telecom.append(ContactPoint(kind=ContactKind.EMAIL, value=str(value)))
            elif plan.target_path in _IDENTIFIER_KINDS:
                identifiers.append(
                    Identifier(kind=_IDENTIFIER_KINDS[plan.target_path], value=str(value))
                )
            else:  # patient.<scalar>
                scalars[plan.target_path.split(".", 1)[1]] = value
        return scalars, address, telecom, identifiers

    def _build_patient(
        self,
        base_row: Row,
        group_key: str,
        extensions: dict[str, object],
        file_name: str,
        resolved_values: dict[str, object] | None = None,
    ) -> Patient:
        scalars, address, telecom, identifiers = self._patient_parts(base_row, resolved_values)
        patient_id = clean_cell(base_row.get(self._patient_key))
        if patient_id is not None:
            # Front of the list so a reader sees the identity this mapping
            # keyed on first; the destination picks by kind, not position.
            identifiers.insert(
                0, Identifier(kind=IdentifierKind.SOURCE_GUID, value=patient_id, system=self.name)
            )
        kwargs: dict[str, object] = dict(scalars)
        kwargs["id"] = patient_id or group_key
        if address:
            kwargs["addresses"] = [Address(**address)]
        if telecom:
            kwargs["telecom"] = telecom
        if identifiers:
            kwargs["identifiers"] = identifiers
        if extensions:
            kwargs["extensions"] = extensions
        kwargs["provenance"] = Provenance(
            source_system=self.name, source_file=file_name, source_id=patient_id
        )
        return self._construct(Patient, kwargs, "patient")

    def _encounter_parts(self, row: Row) -> tuple[dict[str, object], list[NoteSection]]:
        """Route each mapped encounter value: narrative sections, or plain
        fields — the mirror of :meth:`_patient_parts`. A SOAP body arrives
        as source markup, so it is sanitised for render and shadowed as
        text for search/QA; neither is empty-string where the model means
        absent."""
        scalars: dict[str, object] = {}
        sections: list[NoteSection] = []
        for plan in self._encounter_plan:
            value = self._value(row, plan)
            if value is None:
                continue
            if plan.target_path in _SECTION_KINDS:
                kind = _SECTION_KINDS[plan.target_path]
                text = str(value)
                sections.append(
                    NoteSection(
                        kind=kind,
                        title=_SECTION_TITLES[kind],
                        html=sanitize_soap_html(text) or None,
                        text=html_to_text(text) or text,
                    )
                )
            else:  # encounter.<scalar>
                scalars[plan.target_path.split(".", 1)[1]] = value
        return scalars, sections

    def _build_encounter(
        self, row: Row, patient_id: str, encounter_id: str, with_extensions: bool, file_name: str
    ) -> Encounter:
        scalars, sections = self._encounter_parts(row)
        kwargs: dict[str, object] = dict(scalars)
        kwargs["id"] = encounter_id
        kwargs["patient_id"] = patient_id
        if sections:
            kwargs["sections"] = sections
        if with_extensions and (extensions := self._row_extensions([row])):
            kwargs["extensions"] = extensions
        kwargs["provenance"] = Provenance(
            source_system=self.name, source_file=file_name, source_id=encounter_id
        )
        return self._construct(Encounter, kwargs, "encounter")

    def _build_encounters(
        self, patient_id: str, group_rows: list[Row], file_name: str
    ) -> list[Encounter]:
        per_encounter = self._spec.grouping.row_scope == "encounter"
        # In encounter-grained mode every row is its own encounter — even with
        # no encounter field mapped — so per-row columns never collapse.
        if not self._encounter_plan and not per_encounter:
            return []
        encounters: list[Encounter] = []
        seen: set[str] = set()
        for index, row in enumerate(group_rows):
            encounter_id = clean_cell(row.get(self._encounter_key)) if self._encounter_key else None
            if encounter_id is not None:
                if encounter_id in seen:
                    raise MappingError(
                        f"learned mapping {self.name!r}: found 2 rows with the same "
                        f"non-blank encounter key in column {self._encounter_key!r}",
                        column=self._encounter_key,
                        scope="grouping",
                    )
                seen.add(encounter_id)
            encounter_id = encounter_id or f"{self.name}:{patient_id}:{index}"
            encounters.append(
                self._build_encounter(row, patient_id, encounter_id, per_encounter, file_name)
            )
        return encounters

    def _resolve_encounter_grained_patient_values(self, group_rows: list[Row]) -> dict[str, object]:
        """First non-empty patient value per field, refusing conflicts
        across rows. Encounter exports commonly repeat demographics,
        sometimes leaving a repeat blank; incompatible non-empty values
        would make the "first" choice lossy, so they're rejected."""
        values_by_target: dict[str, object] = {}
        for row in group_rows:
            for plan in self._patient_plan:
                value = self._value(row, plan)
                if value is None or (isinstance(value, str) and clean_cell(value) is None):
                    continue
                if plan.target_path not in values_by_target:
                    values_by_target[plan.target_path] = value
                elif values_by_target[plan.target_path] != value:
                    raise MappingError(
                        f"learned mapping {self.name!r}: encounter-grained rows have "
                        f"conflicting non-empty values for patient field {plan.target_path!r} "
                        f"from column {plan.source_path!r}",
                        column=plan.source_path,
                        target=plan.target_path,
                        scope="grouping",
                    )
        return values_by_target

    def _assemble(self, group_key: str, group_rows: list[Row], file_name: str) -> PatientRecord:
        encounter_grained = self._spec.grouping.row_scope == "encounter"
        resolved_patient_values = (
            self._resolve_encounter_grained_patient_values(group_rows)
            if encounter_grained
            else None
        )
        # Un-mapped columns attach where the grain says: per encounter when
        # encounter-grained (via with_extensions), else merged onto the
        # patient (one row per patient, so lossless; round_trip proves it).
        patient_extensions = {} if encounter_grained else self._row_extensions(group_rows)
        patient = self._build_patient(
            group_rows[0],
            group_key,
            patient_extensions,
            file_name,
            resolved_patient_values,
        )
        encounters = self._build_encounters(patient.id, group_rows, file_name)
        return PatientRecord(
            patient=patient,
            encounters=encounters,
            provenance=Provenance(
                source_system=self.name, source_file=file_name, source_id=group_key
            ),
        )

    def _construct(self, model: type[_M], kwargs: dict[str, object], scope: str) -> _M:
        """Build a model, turning a validation failure into a PHI-safe
        error: a bad transform/target type surfaces here, and the message
        names the scope and the field count, never a value."""
        try:
            return model(**kwargs)
        except ValidationError as exc:
            raise MappingError(
                f"learned mapping {self.name!r} produced an invalid {scope} "
                f"({exc.error_count()} field error(s)) — check the transforms for those fields"
            ) from None
